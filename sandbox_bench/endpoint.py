"""创建端点拓扑与就绪信号的只读解析（BCD-13 §13.2／§13.4，图侧 D17）。

同步/异步是入口与已审计产品行为的属性，禁止按接口名称推断。解析结果既可用于
脚本侧实现，也可被图侧（组 29）作为身份与可比性键使用。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Callable

from .scenarios import ScenarioBlocked
from .operation_sync import completion_plan

_CREATION_METHOD_PREFIXES = ('B00.', 'B01.', 'B02.')
# 异步入口（轮询）下，轮询间隔必须远小于被测创建耗时；创建是 ms 级，粗于此则测得的是轮询粒度。
MAX_ASYNC_POLL_INTERVAL_S = 0.005

_TOPOLOGIES = ('router', 'node_direct', 'co_located_cli')
_PRODUCTS = ('cubesandbox', 'kuasar', 'agentenv')
# Endpoints observable from outside the node without an injected descriptor.
_BLACKBOX_ENDPOINTS = ('api_complete', 'state_ready', 'server_terminal', 'exec_complete')


@dataclass(frozen=True)
class EndpointResolution:
    """Resolved creation-entry semantics (BCD-13 §13.4).

    status: 'resolved' | 'unverified' | 'blocked'. A blocked/unverified record must
    not be silently downgraded to another topology.
    """
    product: str
    topology: str
    endpoint_kind: str
    readiness_signal: str
    synchronous: bool
    management_plane_present: bool | None
    path_evidence: str
    status: str

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_endpoint(*, product: str, product_version: str, topology: str,
                     probe: Callable[[str], dict] | None, evidence_ref: str) -> EndpointResolution:
    """Resolve endpoint semantics from the declared topology and a read-only probe.

    `probe(topology)` must only observe; it may return management_plane_present as a
    bool or leave it unknown. Unsupported (product, topology) pairs raise
    ScenarioBlocked('endpoint_unresolved') rather than guessing.
    """
    if topology not in _TOPOLOGIES or product not in _PRODUCTS:
        raise ScenarioBlocked('endpoint_unresolved')
    if not product_version:
        # No audited version means the synchrony claim is unverified, not assumed.
        return EndpointResolution(product, topology, 'api_complete', 'none',
                                  synchronous=False, management_plane_present=None,
                                  path_evidence=evidence_ref or 'missing-product-version',
                                  status='unverified')

    management: bool | None = None
    if probe is not None:
        observed = probe(topology)
        if isinstance(observed, dict):
            declared = observed.get('management_plane_present')
            if declared is not None and not isinstance(declared, bool):
                raise ValueError('management_plane_present must be boolean or null')
            management = declared

    if topology == 'co_located_cli':
        # The ready-fd endpoint is only audited for the Kuasar node runner.
        if product != 'kuasar':
            raise ScenarioBlocked('endpoint_unresolved')
        return EndpointResolution(product, topology, 'guest_ready', 'ready_fd',
                                  synchronous=False, management_plane_present=False,
                                  path_evidence=evidence_ref, status='resolved')

    if topology == 'router':
        if management is False:
            return EndpointResolution(product, topology, 'api_complete', 'none',
                                      synchronous=True, management_plane_present=False,
                                      path_evidence=evidence_ref, status='blocked')
        if management is None:
            return EndpointResolution(product, topology, 'api_complete', 'none',
                                      synchronous=True, management_plane_present=None,
                                      path_evidence=evidence_ref, status='unverified')

    # Audited behaviour (BCD-13 §13.2): Kuasar router blocks to running; the data-plane
    # node API returns `starting`. Cube and AgentENV return ready on both entries.
    if product == 'kuasar':
        synchronous = topology == 'router'
        return EndpointResolution(product, topology, 'api_complete',
                                  'none' if synchronous else 'poll_state',
                                  synchronous=synchronous,
                                  management_plane_present=(topology == 'router'),
                                  path_evidence=evidence_ref, status='resolved')
    return EndpointResolution(product, topology, 'api_complete', 'none',
                              synchronous=True,
                              management_plane_present=(topology == 'router'),
                              path_evidence=evidence_ref, status='resolved')


def endpoint_requires_poll(resolution: EndpointResolution) -> bool:
    """True only when the create entry returns before the sandbox is ready."""
    return resolution.readiness_signal == 'poll_state'


def endpoint_comparable(left: EndpointResolution, right: EndpointResolution) -> bool:
    """Comparable only when both share the same black-box endpoint kind."""
    return (left.endpoint_kind == right.endpoint_kind
            and left.endpoint_kind in _BLACKBOX_ENDPOINTS
            and right.endpoint_kind in _BLACKBOX_ENDPOINTS)


def endpoint_declaration_consistent(product: str, topology, declared_endpoint: str):
    """声明的完成端点是否与实际创建的（已审核）同步性一致（issue #5）。

    - 同步创建（Cube/AgentENV、Kuasar router）必须声明 `API_COMPLETE`；否则
      会多一次 inspect 往返、抬高 latency；
    - 异步创建（Kuasar node_direct）不得声明 `API_COMPLETE`，否则把“受理”当“完成”；
    - 同步性未经审计（未知产品/拓扑）→ 不强制（返回 True 与原因）。
    返回 (ok, reason)，不抛异常，便于调用方选择 blocked/拒绝。
    """
    plan = completion_plan(product, 'create', topology=topology)
    if not plan['verified']:
        return (True, plan['reason'] or 'create endpoint unverified; declaration not enforced')
    if plan['synchronous']:
        if declared_endpoint != 'API_COMPLETE':
            return (False, f'{product}/{topology or "-"} create is synchronous; declare API_COMPLETE, '
                           f'not {declared_endpoint} (STATE_READY adds a redundant inspect round-trip '
                           f'and inflates latency)')
    elif declared_endpoint == 'API_COMPLETE':
        return (False, f'{product}/{topology} create is asynchronous; API_COMPLETE would record '
                       f'acceptance as completion')
    return (True, '')


def poll_interval_adequate(readiness_signal, poll_interval_s, *, max_interval_s=MAX_ASYNC_POLL_INTERVAL_S):
    """异步（需轮询）入口的轮询间隔是否足够密。

    同步入口无需轮询；异步入口若轮询太粗（相对 ms 级创建），测得的创建时延是轮询粒度
    而非真实耗时，必须拒绝。返回 (ok, reason)。
    """
    if readiness_signal != 'poll_state':
        return (True, '')
    if poll_interval_s is None or poll_interval_s <= 0:
        return (False, 'poll-required endpoint needs a positive poll_interval_s')
    if poll_interval_s > max_interval_s:
        return (False, f'poll interval {poll_interval_s}s is coarser than the ms-level creation '
                       f'granularity (<= {max_interval_s}s); measured latency would be polling '
                       f'quantization, not creation time')
    return (True, '')


def validate_endpoint_declaration(case, *, max_poll_interval_s=MAX_ASYNC_POLL_INTERVAL_S) -> None:
    """创建类用例的预检：端点声明与审计同步性不一致则 blocked（不静默规范化）。

    异步（轮询）入口还会校验轮询间隔是否足够密（ms 级创建）。
    """
    method = str(getattr(case, 'method_id', ''))
    if not method.startswith(_CREATION_METHOD_PREFIXES):
        return
    ok, reason = endpoint_declaration_consistent(case.product, getattr(case, 'topology', None),
                                                 case.completion.endpoint)
    if not ok:
        raise ScenarioBlocked('endpoint_declaration_mismatch:' + reason)
    plan = completion_plan(case.product, 'create', topology=getattr(case, 'topology', None))
    if plan['verified'] and plan['poll_required']:
        ok, reason = poll_interval_adequate('poll_state', case.completion.poll_interval_s,
                                            max_interval_s=max_poll_interval_s)
        if not ok:
            raise ScenarioBlocked('endpoint_poll_too_coarse:' + reason)
