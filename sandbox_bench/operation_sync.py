"""Operation sync/async endpoint mapping and synchronous latency (RT-46, BCD-17).

Only a synchronous completion is recorded as the operation latency; an asynchronous entry
returns acceptance and must be polled to its ready state (`state_ready_s`), distinct from
`api_complete_s`. Unconfirmed combinations are `unverified`, never assumed. See
docs/source-audit/alignment.md §11/§12.
"""
from __future__ import annotations

import asyncio
import time

# (product, operation) -> (synchronous, endpoint, ready_state); None trio = unverified/pending.
_TABLE = {
    ('kuasar', 'pause'): (True, 'API_COMPLETE', 'paused'),
    ('kuasar', 'resume'): (False, 'STATE_READY', 'running'),
    ('kuasar', 'delete'): (False, 'STATE_READY', 'gone'),
    ('kuasar', 'clone'): (None, None, None),        # no native fork (B05.NATIVE unsupported)
    ('kuasar', 'snapshot'): (None, None, None),      # export (pause-first) pending
    ('cubesandbox', 'create'): (True, 'API_COMPLETE', 'running'),
    ('cubesandbox', 'pause'): (True, 'API_COMPLETE', 'paused'),
    ('cubesandbox', 'resume'): (True, 'API_COMPLETE', 'running'),
    ('cubesandbox', 'clone'): (True, 'API_COMPLETE', 'running'),
    ('cubesandbox', 'delete'): (True, 'API_COMPLETE', 'gone'),
    ('cubesandbox', 'snapshot'): (None, None, None),  # pending
    ('agentenv', 'create'): (True, 'API_COMPLETE', 'running'),
    ('agentenv', 'pause'): (True, 'API_COMPLETE', 'paused'),
    ('agentenv', 'resume'): (True, 'API_COMPLETE', 'running'),
    ('agentenv', 'clone'): (None, None, None),        # fork pending
    ('agentenv', 'delete'): (True, 'API_COMPLETE', 'gone'),
    ('agentenv', 'snapshot'): (True, 'API_COMPLETE', 'published'),
}

_GONE_STATES = ('gone', 'dead', 'not_found', 'deleted')


def _plan(synchronous, endpoint, ready_state):
    return {'synchronous': synchronous, 'endpoint': endpoint, 'ready_state': ready_state,
            'poll_required': not synchronous, 'verified': True, 'reason': None}


def _unverified(reason):
    return {'synchronous': None, 'endpoint': None, 'ready_state': None,
            'poll_required': False, 'verified': False, 'reason': reason}


def completion_plan(product, operation, *, topology=None):
    """Per (product, operation) completion plan; unknown/entry-ambiguous -> unverified."""
    if product == 'kuasar' and operation == 'create':
        if topology == 'router':
            return _plan(True, 'API_COMPLETE', 'running')
        if topology == 'node_direct':
            return _plan(False, 'STATE_READY', 'running')
        return _unverified('kuasar create requires the entry topology (router/node_direct)')
    entry = _TABLE.get((product, operation))
    if entry is None or entry[0] is None:
        return _unverified('operation endpoint not confirmed for this product')
    return _plan(*entry)


def physical_release_status(product, delete_status, *, observed_state=None):
    """删除后能否声称物理释放（issue #3 根因）。

    - 非 `api_deleted` → `unverified`；
    - 同步删除（Cube/AgentENV）→ `verified`（调用返回即已删除）；
    - 异步删除（Kuasar）→ 必须**观察**到 gone 才 `verified`，否则 `unverified`；
    - 端点未确认 → `unverified`。绝不把“已接受删除”当“已删除”。
    """
    if delete_status != 'api_deleted':
        return 'unverified'
    plan = completion_plan(product, 'delete')
    if not plan['verified']:
        return 'unverified'
    if plan['synchronous']:
        return 'verified'
    return 'verified' if observed_state in _GONE_STATES else 'unverified'


def _reached(state, ready_state):
    if ready_state == 'gone':
        return state in _GONE_STATES
    return state == ready_state


async def await_operation(adapter, handle, plan, *, api_complete_s=0.0, timeout,
                          poll_interval=0.0, sleep=asyncio.sleep, clock=time.monotonic):
    """Stop at the synchronous endpoint; poll asynchronous entries to the ready state."""
    if not plan['verified']:
        return {'status': 'unverified', 'reason': plan['reason'], 'api_complete_s': api_complete_s,
                'state_ready_s': None, 'polls': 0}
    if not plan['poll_required']:
        return {'status': 'success', 'api_complete_s': api_complete_s, 'state_ready_s': api_complete_s,
                'polls': 0, 'state': plan['ready_state']}
    deadline = clock() + timeout
    polls = 0
    while clock() < deadline:
        state = adapter.info(handle, max(0.001, deadline - clock())).get('state')
        polls += 1
        if _reached(state, plan['ready_state']):
            return {'status': 'success', 'api_complete_s': api_complete_s, 'state_ready_s': clock(),
                    'polls': polls, 'state': state}
        await sleep(poll_interval)
    return {'status': 'timeout', 'api_complete_s': api_complete_s, 'state_ready_s': None, 'polls': polls}
