"""Application protocol qualification; controlled providers are not host adapter proof."""
import asyncio
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
from pydantic import ValidationError

from tests.orchestration.conftest import FakeClock, MemoryRepository
from sandbox_graph.runtime.agent_bridge import (
    AgentBridge, BridgeConfig, CancelReply, CapabilityMissing, CollectReply,
    HttpAgentProvider, LookupReply, ProviderCapabilities, ProviderError,
    ProviderReceipt, ProviderScope, SubmitEnvelope, UsageCeiling, Usage,
)


def scope(ref='work:a'):
    return ProviderScope(schema_version=1, kind='work', ref=ref)


def envelope(**changes):
    values = dict(schema_version=1, request_id='request:1', lifecycle_id='life:1',
        scope=scope(), input_json='{"facts":[]}',
        input_hash='14d4159d74f8cfe160ef4a3ca2062efb2918b522ae76c77ac6286b7029ba894a',
        model_id='model', model_version='v1', deadline=1100.0,
        usage_ceiling=UsageCeiling(schema_version=1, input_tokens=100, output_tokens=50,
                                  cost_microunits=1000))
    # Digest literal independently checked with SHA-256 CLI / hashlib.
    values['input_hash'] = hashlib.sha256(values['input_json'].encode()).hexdigest()
    return SubmitEnvelope(**{**values, **changes})


def capabilities(**changes):
    return ProviderCapabilities(**{**dict(schema_version=1, provider_id='local',
        idempotent_submit=True, lookup_by_request_id=True, durable_handles=True,
        status=True, cancel=True, collect=True, scope_enforced=True,
        usage_limits=True, isolated_tools=False, scopes=(scope(),)), **changes})


def receipt(env=None, **changes):
    env = env or envelope()
    return ProviderReceipt(**{**dict(schema_version=1, request_id=env.request_id,
        scope=env.scope, input_hash=env.input_hash, model_id=env.model_id,
        model_version=env.model_version, usage_ceiling=env.usage_ceiling,
        handle='task:1', state='running', receipt_id='receipt:running',
        children_stopped=False, usage=None), **changes})


class ControlledProvider:
    """Controlled remote boundary, including response loss after acceptance."""
    def __init__(self, caps=None):
        self.caps = caps or capabilities()
        self.submissions = []
        self.lookups = []
        self.saved = None
        self.lose_submit = False
        self.unknown_lookup = False
        self.failure = None
        self.current = None

    async def capabilities(self):
        return self.caps

    async def submit(self, env):
        self.submissions.append(env)
        self.saved = receipt(env)
        if self.lose_submit:
            raise ProviderError('timeout')
        return self.saved

    async def lookup(self, request_id):
        self.lookups.append(request_id)
        if self.failure:
            raise self.failure
        return LookupReply(schema_version=1, acceptance='unknown' if self.unknown_lookup else 'accepted',
                           receipt=None if self.unknown_lookup else self.saved)

    async def status(self, handle):
        if self.failure:
            raise self.failure
        return self.current or self.saved

    async def cancel(self, handle):
        return CancelReply(schema_version=1, request_id='request:1', scope=scope(),
                           handle=handle, accepted=True)

    async def collect(self, handle):
        return CollectReply(schema_version=1, receipt=self.current, output_json='{"answer":"ok"}')


def bridge(repo=None, provider=None, **kwargs):
    return AgentBridge(repo or MemoryRepository(FakeClock()), provider or ControlledProvider(),
                       provider_id='local', scope=scope(), **kwargs)


@pytest.mark.asyncio
async def test_response_loss_and_process_restart_query_original_id_without_resubmit():
    repo = MemoryRepository(FakeClock())
    provider = ControlledProvider()
    provider.lose_submit = True
    first = await bridge(repo, provider).submit(envelope())
    assert first.state == 'submitted_unknown' and first.receipt is None
    restored = await bridge(repo, provider).submit(envelope())
    assert restored.receipt.handle == 'task:1'
    assert len(provider.submissions) == 1 and provider.lookups == ['request:1']
    with repo.transaction() as tx:
        assert tx.get('agent_provider_requests', 'request:1')['body']['envelope']['model_version'] == 'v1'


@pytest.mark.asyncio
async def test_process_death_after_durable_intent_and_unknown_lookup_never_resubmits():
    repo = MemoryRepository(FakeClock())
    provider = ControlledProvider()
    provider.lose_submit = True
    await bridge(repo, provider).submit(envelope())
    provider.unknown_lookup = True
    for _ in range(2):
        assert (await bridge(repo, provider).submit(envelope())).state == 'submitted_unknown'
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [dict(model_version='v2'), dict(lifecycle_id='life:2'),
    dict(usage_ceiling=UsageCeiling(schema_version=1,input_tokens=101,output_tokens=50,cost_microunits=1000))])
async def test_request_id_conflict_cannot_rebind_model_lifecycle_or_ceiling(changed):
    b = bridge()
    await b.submit(envelope())
    with pytest.raises(ValueError, match='request_id'):
        await b.submit(envelope(**changed))


@pytest.mark.asyncio
async def test_missing_candidate_capability_does_not_block_diagnosis_path():
    provider = ControlledProvider()
    b = bridge(provider=provider)
    with pytest.raises(CapabilityMissing, match='isolated_tools'):
        await b.submit(envelope(), required_capabilities=('isolated_tools',))
    assert not provider.submissions
    assert (await b.submit(envelope())).receipt.state == 'running'


@pytest.mark.asyncio
async def test_missing_required_lookup_or_scope_is_precisely_blocked_before_submission():
    for caps, expected in [(capabilities(lookup_by_request_id=False), 'lookup_by_request_id'),
                            (capabilities(scopes=(scope('other'),)), 'scope')]:
        provider = ControlledProvider(caps)
        with pytest.raises(CapabilityMissing, match=expected):
            await bridge(provider=provider).submit(envelope())
        assert not provider.submissions


@pytest.mark.asyncio
async def test_cancel_accepted_requires_later_terminal_receipt_and_stopped_children():
    provider = ControlledProvider()
    b = bridge(provider=provider)
    await b.submit(envelope())
    cancelling = await b.cancel('request:1')
    assert cancelling.cancel_accepted and not cancelling.terminal
    with pytest.raises(ProviderError, match='not_terminal'):
        await b.collect('request:1')
    provider.current = receipt(state='cancelled', children_stopped=True,
        receipt_id='receipt:cancelled', usage=Usage(schema_version=1,input_tokens=5,output_tokens=0,cost_microunits=10))
    final = await b.status('request:1')
    assert final.terminal and final.receipt.state == 'cancelled'
    result = await b.collect('request:1')
    assert result.output_json == '{"answer":"ok"}'
    assert (await b.collect('request:1')) == result


@pytest.mark.asyncio
async def test_unstopped_children_are_not_terminal_and_changed_duplicate_receipt_is_rejected():
    provider = ControlledProvider()
    b = bridge(provider=provider)
    await b.submit(envelope())
    provider.current = receipt(state='succeeded', children_stopped=False,receipt_id='receipt:done')
    assert not (await b.status('request:1')).terminal
    provider.current = receipt(state='succeeded', children_stopped=True,receipt_id='receipt:done')
    with pytest.raises(ProviderError, match='receipt_conflict'):
        await b.status('request:1')


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [dict(scope=scope('other')),dict(input_hash='f'*64),
    dict(model_version='v2'),dict(handle='different')])
async def test_receipt_binding_rejects_cross_scope_or_changed_task(changes):
    provider = ControlledProvider()
    b = bridge(provider=provider)
    await b.submit(envelope())
    provider.current = receipt(**changes)
    with pytest.raises(ProviderError, match='binding'):
        await b.status('request:1')


@pytest.mark.asyncio
async def test_usage_over_ceiling_is_protocol_error_and_missing_usage_remains_unknown():
    provider = ControlledProvider()
    b = bridge(provider=provider)
    first = await b.submit(envelope())
    assert first.receipt.usage is None
    provider.current = receipt(receipt_id='receipt:over',usage=Usage(schema_version=1,input_tokens=101,output_tokens=0,cost_microunits=0))
    with pytest.raises(ProviderError, match='usage_ceiling'):
        await b.status('request:1')


@pytest.mark.parametrize('changes',[dict(input_hash='0'*64),dict(request_id=''),dict(deadline=True),
    dict(extra='bad'),dict(input_json='{"bad":NaN}')])
def test_envelope_rejects_invalid_or_non_strict_input(changes):
    with pytest.raises(ValidationError):
        envelope(**changes)


@contextmanager
def protocol_server():
    state = {'calls': [], 'mode': 'ok'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            payload = json.loads(raw)
            state['calls'].append((self.path, self.headers.get('Authorization'), payload))
            if self.headers.get('Authorization') != 'Bearer secret':
                self.send_response(401); self.end_headers(); return
            if state['mode'] == 'auth':
                self.send_response(403); self.end_headers(); return
            if state['mode'] == 'drop':
                self.close_connection = True; return
            if state['mode'] == 'timeout':
                import time
                time.sleep(.15)
            method = self.path.rsplit('/',1)[-1]
            result = {
                'capabilities': capabilities().model_dump(mode='json'),
                'submit': receipt().model_dump(mode='json'),
                'lookup': LookupReply(schema_version=1,acceptance='accepted',receipt=receipt()).model_dump(mode='json'),
                'status': receipt().model_dump(mode='json'),
                'cancel': CancelReply(schema_version=1,request_id='request:1',scope=scope(),handle='task:1',accepted=True).model_dump(mode='json'),
                'collect': CollectReply(schema_version=1,receipt=receipt(state='succeeded',children_stopped=True,receipt_id='done'),output_json='{}').model_dump(mode='json'),
            }[method]
            if state['mode'] == 'malformed': result['unexpected'] = True
            body = b'x' * 20000 if state['mode'] == 'oversize' else json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/bridge', state
    finally: server.shutdown(); server.server_close(); thread.join()


def config(url, **changes):
    return BridgeConfig(**{**dict(schema_version=1,endpoint=url,provider_id='local',
        bearer_token='secret',scope=scope(),timeout_seconds=1.0,max_request_bytes=16384,
        max_response_bytes=16384), **changes})


@pytest.mark.asyncio
async def test_real_loopback_protocol_exercises_every_operation_and_scope_auth_envelope():
    with protocol_server() as (url, state):
        provider = HttpAgentProvider(config(url))
        assert (await provider.capabilities()).provider_id == 'local'
        assert (await provider.submit(envelope())).handle == 'task:1'
        assert (await provider.lookup('request:1')).acceptance == 'accepted'
        assert (await provider.status('task:1')).state == 'running'
        assert (await provider.cancel('task:1')).accepted
        assert (await provider.collect('task:1')).output_json == '{}'
        assert len(state['calls']) == 6
        for path, auth, payload in state['calls']:
            assert path.startswith('/bridge/v1/') and auth == 'Bearer secret'
            assert payload['schema_version'] == 1 and payload['scope'] == scope().model_dump(mode='json')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code',[('auth','authentication_failed'),('drop','transport_unknown'),
    ('malformed','invalid_protocol'),('oversize','response_too_large'),('timeout','transport_unknown')])
async def test_loopback_failures_have_precise_codes_without_leaking_credentials(mode,code):
    with protocol_server() as (url, state):
        state['mode'] = mode
        with pytest.raises(ProviderError,match=code) as error:
            await HttpAgentProvider(config(url,timeout_seconds=.05)).capabilities()
        assert 'secret' not in str(error.value)


@pytest.mark.parametrize('endpoint',['http://example.com/x','ftp://127.0.0.1/x',
    'https://user:pass@example.com/x','https://example.com/x?q=secret','https://example.com/x#fragment'])
def test_config_requires_tls_except_loopback_and_forbids_embedded_credentials(endpoint):
    with pytest.raises(ValidationError): config(endpoint)


@pytest.mark.parametrize('version', [True, 1.0, '1'])
def test_protocol_version_rejects_numeric_coercion(version):
    with pytest.raises(ValidationError):
        ProviderScope(schema_version=version,kind='work',ref='work:a')


@pytest.mark.asyncio
async def test_abrupt_worker_death_leaves_committed_intent_for_lookup_only():
    class Crash(BaseException): pass
    class CrashingProvider(ControlledProvider):
        async def submit(self, env):
            raise Crash()
    repo = MemoryRepository(FakeClock())
    with pytest.raises(Crash):
        await bridge(repo,CrashingProvider()).submit(envelope())
    recovery_provider = ControlledProvider()
    recovery_provider.unknown_lookup = True
    recovered = await bridge(repo,recovery_provider).submit(envelope())
    assert recovered.state == 'submitted_unknown' and not recovery_provider.submissions
    assert recovery_provider.lookups == ['request:1']


@pytest.mark.asyncio
async def test_distinct_request_ids_must_not_share_same_provider_handle():
    class ReusedHandleProvider(ControlledProvider):
        async def submit(self, env):
            return receipt(env,receipt_id='receipt:' + env.request_id)
    b = bridge(provider=ReusedHandleProvider())
    await b.submit(envelope())
    second = await b.submit(envelope(request_id='request:2'))
    assert second.state == 'submitted_unknown' and second.last_error == 'handle_binding_conflict'


@pytest.mark.asyncio
async def test_known_handle_and_terminal_state_survive_late_unknown_lookup():
    class RacingProvider(ControlledProvider):
        def __init__(self):
            super().__init__()
            self.submit_started, self.submit_release = asyncio.Event(), asyncio.Event()
            self.lookup_started, self.lookup_release = asyncio.Event(), asyncio.Event()
        async def submit(self, env):
            self.submit_started.set()
            await self.submit_release.wait()
            return receipt(state='succeeded',children_stopped=True,receipt_id='done')
        async def lookup(self, request_id):
            self.lookup_started.set()
            await self.lookup_release.wait()
            return LookupReply(schema_version=1,acceptance='unknown',receipt=None)
    provider = RacingProvider()
    b = bridge(provider=provider)
    submission = asyncio.create_task(b.submit(envelope()))
    await provider.submit_started.wait()
    late_lookup = asyncio.create_task(b.lookup('request:1'))
    await provider.lookup_started.wait()
    provider.submit_release.set()
    assert (await submission).terminal
    provider.lookup_release.set()
    assert (await late_lookup).terminal


@pytest.mark.asyncio
async def test_request_id_conflicts_across_provider_and_scope_before_any_second_submission():
    repo = MemoryRepository(FakeClock())
    await bridge(repo).submit(envelope())
    provider = ControlledProvider(capabilities(provider_id='other'))
    other = AgentBridge(repo,provider,provider_id='other',scope=scope())
    with pytest.raises(ValueError,match='request_id'):
        await other.submit(envelope())
    assert not provider.submissions


@pytest.mark.asyncio
async def test_identical_receipt_replay_is_idempotent_and_keeps_usage_unknown():
    repo = MemoryRepository(FakeClock())
    provider = ControlledProvider()
    b = bridge(repo,provider)
    first = await b.submit(envelope())
    assert await b.status('request:1') == first
    assert await b.status('request:1') == first
    with repo.transaction() as tx:
        assert len(tx.scan('agent_provider_receipts')) == 1
        assert len(tx.scan('agent_provider_handles')) == 1
    assert first.receipt.usage is None


@pytest.mark.asyncio
async def test_expired_first_submission_does_not_create_intent_or_network_effect():
    provider = ControlledProvider()
    repo = MemoryRepository(FakeClock())
    with pytest.raises(ProviderError,match='deadline_expired'):
        await bridge(repo,provider).submit(envelope(deadline=999.0))
    with repo.transaction() as tx:
        assert tx.scan('agent_provider_requests') == []
    assert not provider.submissions


@pytest.mark.asyncio
async def test_rpc_request_size_and_scope_are_rejected_before_http_dispatch():
    with protocol_server() as (url,state):
        provider = HttpAgentProvider(config(url,max_request_bytes=32))
        with pytest.raises(ProviderError,match='request_too_large'):
            await provider.submit(envelope())
        with pytest.raises(ProviderError,match='scope'):
            await provider.submit(envelope(scope=scope('other')))
        assert state['calls'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['lookup','status','cancel','collect'])
@pytest.mark.parametrize('identifier', [True,1,'','white space'])
async def test_rpc_query_identifiers_are_strict_before_network(method,identifier):
    with protocol_server() as (url,state):
        with pytest.raises(ValidationError):
            await getattr(HttpAgentProvider(config(url)),method)(identifier)
        assert state['calls'] == []


@pytest.mark.asyncio
async def test_negative_lookup_before_delayed_acceptance_and_lost_reply_remains_unknown():
    class DelayedProvider(ControlledProvider):
        def __init__(self):
            super().__init__()
            self.started, self.release = asyncio.Event(), asyncio.Event()
        async def submit(self, env):
            self.submissions.append(env)
            self.started.set()
            await self.release.wait()
            self.saved = receipt(env)
            raise ProviderError('transport_unknown')
        async def lookup(self, request_id):
            self.lookups.append(request_id)
            return LookupReply(schema_version=1,
                acceptance='accepted' if self.saved is not None else 'not_accepted',
                receipt=self.saved)
    repo, provider = MemoryRepository(FakeClock()), DelayedProvider()
    pending = asyncio.create_task(bridge(repo,provider).submit(envelope()))
    await provider.started.wait()
    try:
        negative = await bridge(repo,provider).lookup('request:1')
    finally:
        provider.release.set()
        after_loss = await pending
    assert negative.state == 'submitted_unknown' and negative.receipt is None
    assert after_loss.state == 'submitted_unknown' and after_loss.last_error == 'transport_unknown'
    assert provider.saved is not None
    recovered = await bridge(repo,provider).submit(envelope())
    assert recovered.state == 'submitted' and recovered.receipt.handle == 'task:1'
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
async def test_unknown_replay_ignores_unrelated_new_submission_capability_losses():
    provider = ControlledProvider()
    provider.lose_submit = True
    b = bridge(provider=provider)
    await b.submit(envelope())
    provider.caps = capabilities(cancel=False,isolated_tools=False,usage_limits=False)
    recovered = await b.submit(envelope(),required_capabilities=('isolated_tools',))
    assert recovered.state == 'submitted' and provider.lookups == ['request:1']
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
async def test_known_handle_replay_only_requires_status_capability():
    provider = ControlledProvider()
    b = bridge(provider=provider)
    await b.submit(envelope())
    provider.caps = capabilities(lookup_by_request_id=False,cancel=False,isolated_tools=False)
    recovered = await b.submit(envelope(),required_capabilities=('isolated_tools',))
    assert recovered.receipt.handle == 'task:1' and provider.lookups == []
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('known_handle,missing',[(False,'lookup_by_request_id'),(True,'status')])
async def test_replay_blocks_only_missing_recovery_capability(known_handle,missing):
    provider = ControlledProvider()
    provider.lose_submit = not known_handle
    b = bridge(provider=provider)
    await b.submit(envelope())
    provider.caps = capabilities(**{missing:False},cancel=False,isolated_tools=False)
    with pytest.raises(CapabilityMissing) as error:
        await b.submit(envelope(),required_capabilities=('isolated_tools',))
    assert error.value.missing == (missing,)
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
async def test_terminal_replay_and_binding_conflict_work_without_any_provider_rpc():
    class UnavailableProvider(ControlledProvider):
        async def capabilities(self):
            if self.failure:
                raise self.failure
            return await super().capabilities()
    provider = UnavailableProvider()
    b = bridge(provider=provider)
    await b.submit(envelope())
    provider.current = receipt(state='succeeded',children_stopped=True,receipt_id='done')
    final = await b.status('request:1')
    provider.failure = ProviderError('provider_offline')
    assert await b.submit(envelope(),required_capabilities=('isolated_tools',)) == final
    with pytest.raises(ValueError,match='request_id'):
        await b.submit(envelope(model_version='changed'))
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
async def test_new_submit_rechecks_binding_after_capability_io_and_never_double_sends():
    class WaitingCapabilities(ControlledProvider):
        def __init__(self):
            super().__init__()
            self.waiting, self.release = asyncio.Event(), asyncio.Event()
            self.calls = 0
        async def capabilities(self):
            self.calls += 1
            if self.calls == 1:
                self.waiting.set()
                await self.release.wait()
            return self.caps
    provider = WaitingCapabilities()
    b = bridge(provider=provider)
    pending = asyncio.create_task(b.submit(envelope()))
    await provider.waiting.wait()
    winner = await b.submit(envelope())
    provider.release.set()
    replay = await pending
    assert winner == replay
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
async def test_concurrent_different_envelope_winner_prevents_late_first_send():
    class WaitingCapabilities(ControlledProvider):
        def __init__(self):
            super().__init__()
            self.waiting, self.release = asyncio.Event(), asyncio.Event()
            self.calls = 0
        async def capabilities(self):
            self.calls += 1
            if self.calls == 1:
                self.waiting.set()
                await self.release.wait()
            return self.caps
    provider = WaitingCapabilities()
    b = bridge(provider=provider)
    pending = asyncio.create_task(b.submit(envelope()))
    await provider.waiting.wait()
    winner = await b.submit(envelope(model_version='v2'))
    provider.release.set()
    with pytest.raises(ValueError,match='request_id'):
        await pending
    assert winner.envelope.model_version == 'v2'
    assert len(provider.submissions) == 1 and provider.submissions[0].model_version == 'v2'
