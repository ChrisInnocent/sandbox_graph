"""Application-owned provider protocol; provider receipts are not lifecycle authority.

Authorization, owner fencing and finite lineage reservations belong to the caller.
This module preserves the original submission identity across crashes and unknown
acceptance. It never infers trust from model output or uses a host's private API.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator, model_validator

from sandbox_graph.persistence.repository import Conflict, Repository


Ref = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512, pattern=r'^\S+$')]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r'^[a-f0-9]{64}$')]
Positive = Annotated[int, Field(strict=True, gt=0, le=10**12)]
Count = Annotated[int, Field(strict=True, ge=0, le=10**12)]
JsonText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1048576)]
CAPABILITIES = frozenset({'idempotent_submit', 'lookup_by_request_id', 'durable_handles',
    'status', 'cancel', 'collect', 'scope_enforced', 'usage_limits', 'isolated_tools'})
REQUIRED_SUBMIT = ('idempotent_submit', 'lookup_by_request_id', 'durable_handles',
                   'status', 'cancel', 'collect', 'scope_enforced', 'usage_limits')
TERMINAL_STATES = frozenset({'succeeded', 'failed', 'cancelled'})


class WireModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, allow_inf_nan=False)
    schema_version: Literal[1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('integer schema_version=1 required')
        return value


def _json(value: str) -> str:
    def reject_constant(value):
        raise ValueError('nonfinite JSON value')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('valid bounded JSON required') from exc
    if len(value.encode('utf-8')) > 1048576:
        raise ValueError('JSON exceeds byte limit')
    return value


class ProviderScope(WireModel):
    kind: Literal['run', 'group', 'work', 'node', 'candidate']
    ref: Ref


class UsageCeiling(WireModel):
    input_tokens: Positive
    output_tokens: Positive
    cost_microunits: Positive


class Usage(WireModel):
    input_tokens: Count
    output_tokens: Count
    cost_microunits: Count


class SubmitEnvelope(WireModel):
    request_id: Ref
    lifecycle_id: Ref
    scope: ProviderScope
    input_hash: Digest
    input_json: JsonText
    model_id: Ref
    model_version: Ref
    usage_ceiling: UsageCeiling
    deadline: Annotated[float, Field(strict=True, gt=0)]

    @field_validator('input_json')
    @classmethod
    def valid_json(cls, value):
        return _json(value)

    @model_validator(mode='after')
    def bound_input(self):
        if hashlib.sha256(self.input_json.encode('utf-8')).hexdigest() != self.input_hash:
            raise ValueError('input_hash does not bind exact input_json bytes')
        return self


class ScopeQuery(WireModel):
    scope: ProviderScope


class LookupQuery(ScopeQuery):
    request_id: Ref


class HandleQuery(ScopeQuery):
    handle: Ref


class SubmitQuery(ScopeQuery):
    envelope: SubmitEnvelope


class ProviderCapabilities(WireModel):
    provider_id: Ref
    idempotent_submit: bool
    lookup_by_request_id: bool
    durable_handles: bool
    status: bool
    cancel: bool
    collect: bool
    scope_enforced: bool
    usage_limits: bool
    isolated_tools: bool
    scopes: Annotated[tuple[ProviderScope, ...], Field(max_length=1024)]


class ProviderReceipt(WireModel):
    request_id: Ref
    scope: ProviderScope
    input_hash: Digest
    model_id: Ref
    model_version: Ref
    usage_ceiling: UsageCeiling
    handle: Ref
    receipt_id: Ref
    state: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled']
    children_stopped: bool
    usage: Usage | None

    @property
    def terminal(self):
        return self.state in TERMINAL_STATES and self.children_stopped


class LookupReply(WireModel):
    # v1 has no irrevocable tombstone proof. A negative is only an observation,
    # and cannot establish nonexecution or justify releasing a reservation.
    acceptance: Literal['accepted', 'not_accepted', 'unknown']
    receipt: ProviderReceipt | None

    @model_validator(mode='after')
    def accepted_receipt(self):
        if (self.acceptance == 'accepted') != (self.receipt is not None):
            raise ValueError('accepted lookup requires exactly one receipt')
        return self


class CancelReply(WireModel):
    request_id: Ref
    scope: ProviderScope
    handle: Ref
    accepted: bool


class CollectReply(WireModel):
    receipt: ProviderReceipt
    output_json: JsonText

    @field_validator('output_json')
    @classmethod
    def valid_json(cls, value):
        return _json(value)


class RequestRecord(WireModel):
    provider_id: Ref
    envelope: SubmitEnvelope
    # not_accepted remains decodable for old ledgers, never emitted as new proof.
    state: Literal['submitted_unknown', 'submitted', 'not_accepted', 'terminal']
    receipt: ProviderReceipt | None = None
    cancel_accepted: bool = False
    last_error: Ref | None = None
    result: CollectReply | None = None

    @property
    def terminal(self):
        return self.state == 'terminal' and self.receipt is not None and self.receipt.terminal


class ProviderError(ValueError):
    """A stable, credential-free protocol or transport error code."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CapabilityMissing(ProviderError):
    def __init__(self, missing):
        self.missing = tuple(missing)
        super().__init__('capability_missing:' + ','.join(self.missing))


class AgentProvider(Protocol):
    async def capabilities(self) -> ProviderCapabilities: ...
    async def submit(self, envelope: SubmitEnvelope) -> ProviderReceipt: ...
    async def lookup(self, request_id: str) -> LookupReply: ...
    async def status(self, handle: str) -> ProviderReceipt: ...
    async def cancel(self, handle: str) -> CancelReply: ...
    async def collect(self, handle: str) -> CollectReply: ...


def _decode(model, body):
    # Repository is a JSON boundary: roundtrip preserves strict tuple JSON semantics.
    return model.model_validate_json(json.dumps(body, allow_nan=False))


class AgentBridge:
    """Durable client-side intent and receipts, independent of a host adapter.

    A request ID is globally unique in this repository, including across provider
    configurations. Existing unknown intents are queried, never blindly resent.
    Capability checks are not proof of caller authorization or budget reservation.
    """
    def __init__(self, repository: Repository, provider: AgentProvider, *,
                 provider_id: str, scope: ProviderScope, commit_guard=None):
        self.repository, self.provider = repository, provider
        self.provider_id, self.scope = provider_id, scope
        self.commit_guard=commit_guard

    async def capabilities(self, *required):
        caps = await self.provider.capabilities()
        if not isinstance(caps, ProviderCapabilities):
            raise ProviderError('invalid_protocol')
        if caps.provider_id != self.provider_id:
            raise ProviderError('provider_binding_mismatch')
        missing = [name for name in required if name not in CAPABILITIES or not getattr(caps, name)]
        if self.scope not in caps.scopes:
            missing.append('scope')
        if missing:
            raise CapabilityMissing(missing)
        return caps

    def _read(self, request_id):
        with self.repository.transaction() as tx:
            row = tx.get('agent_provider_requests', request_id)
            if row is None:
                raise ProviderError('request_not_found')
            record = _decode(RequestRecord, row['body'])
            if record.provider_id != self.provider_id or record.envelope.scope != self.scope:
                raise ProviderError('request_binding_mismatch')
            return record

    @staticmethod
    def _binding(record, receipt):
        if not isinstance(receipt, ProviderReceipt):
            raise ProviderError('invalid_protocol')
        env = record.envelope
        for field in ('request_id', 'scope', 'input_hash', 'model_id', 'model_version', 'usage_ceiling'):
            if getattr(receipt, field) != getattr(env, field):
                raise ProviderError('receipt_binding_mismatch')
        if record.receipt is not None and receipt.handle != record.receipt.handle:
            raise ProviderError('handle_binding_mismatch')
        if receipt.usage is not None:
            for field in ('input_tokens', 'output_tokens', 'cost_microunits'):
                if getattr(receipt.usage, field) > getattr(env.usage_ceiling, field):
                    raise ProviderError('usage_ceiling_exceeded')

    def _save_receipt(self, request_id, receipt, *, result=None):
        with self.repository.transaction() as tx:
            if self.commit_guard is not None: self.commit_guard(tx)
            row = tx.get('agent_provider_requests', request_id)
            record = _decode(RequestRecord, row['body'])
            self._binding(record, receipt)
            # Scope/provider identity is rechecked under the same transaction.
            if record.provider_id != self.provider_id or record.envelope.scope != self.scope:
                raise ProviderError('request_binding_mismatch')
            handle_key = hashlib.sha256(json.dumps([self.provider_id, self.scope.kind,
                self.scope.ref, receipt.handle], separators=(',', ':')).encode()).hexdigest()
            handle_binding = {'provider_id': self.provider_id,
                'scope': self.scope.model_dump(mode='json'), 'handle': receipt.handle,
                'request_id': request_id}
            saved_handle = tx.get('agent_provider_handles', handle_key)
            if saved_handle and saved_handle['body'] != handle_binding:
                raise ProviderError('handle_binding_conflict')
            receipt_key = hashlib.sha256((self.provider_id + '\0' + receipt.receipt_id).encode()).hexdigest()
            saved = tx.get('agent_provider_receipts', receipt_key)
            wire = receipt.model_dump(mode='json')
            if saved and saved['body'] != wire:
                raise ProviderError('receipt_conflict')
            # Late stale receipts cannot regress a terminal task or overwrite its result.
            if record.terminal and receipt != record.receipt:
                raise ProviderError('terminal_receipt_conflict')
            if result is not None and record.result is not None and result != record.result:
                raise ProviderError('collect_conflict')
            tx.put('agent_provider_handles', handle_key, handle_binding, immutable=True)
            tx.put('agent_provider_receipts', receipt_key, wire, immutable=True)
            updated = RequestRecord(**{**record.model_dump(), 'receipt': receipt,
                'state': 'terminal' if receipt.terminal else 'submitted', 'last_error': None,
                'result': result if result is not None else record.result})
            tx.put('agent_provider_requests', request_id, updated.model_dump(mode='json'),
                   expected_revision=row['revision'])
            return updated

    def _save_changes(self, request_id, **changes):
        with self.repository.transaction() as tx:
            if self.commit_guard is not None: self.commit_guard(tx)
            row = tx.get('agent_provider_requests', request_id)
            record = _decode(RequestRecord, row['body'])
            if record.provider_id != self.provider_id or record.envelope.scope != self.scope:
                raise ProviderError('request_binding_mismatch')
            if 'state' in changes and record.receipt is not None:
                # A lookup initiated before the acceptance reply may arrive later.
                # Absence from a stale snapshot cannot revoke known acceptance.
                changes = {key: value for key, value in changes.items() if key != 'state'}
            updated = RequestRecord(**{**record.model_dump(), **changes})
            tx.put('agent_provider_requests', request_id, updated.model_dump(mode='json'),
                   expected_revision=row['revision'])
            return updated

    async def submit(self, envelope: SubmitEnvelope, *, required_capabilities=()):
        if not isinstance(envelope, SubmitEnvelope) or envelope.scope != self.scope:
            raise ProviderError('envelope_scope_mismatch')
        # Recovery validates durable identity before requiring any live provider
        # capability. Terminal replay is a local read; other replay only needs the
        # lookup/status capability actually used, irrespective of submit options.
        with self.repository.transaction() as tx:
            row = tx.get('agent_provider_requests', envelope.request_id)
            if row:
                record = _decode(RequestRecord, row['body'])
                if record.provider_id != self.provider_id or record.envelope != envelope:
                    raise Conflict('request_id already binds different provider or envelope')
        if row:
            return await self.status(envelope.request_id)
        await self.capabilities(*REQUIRED_SUBMIT, *required_capabilities)
        with self.repository.transaction() as tx:
            # Another worker can win while capability discovery awaits I/O.
            # Only the transaction that creates the intent may send submit.
            row = tx.get('agent_provider_requests', envelope.request_id)
            if row:
                record = _decode(RequestRecord, row['body'])
                if record.provider_id != self.provider_id or record.envelope != envelope:
                    raise Conflict('request_id already binds different provider or envelope')
            else:
                if tx.now >= envelope.deadline:
                    raise ProviderError('submission_deadline_expired')
                record = RequestRecord(schema_version=1, provider_id=self.provider_id,
                    envelope=envelope, state='submitted_unknown')
                tx.put('agent_provider_requests', envelope.request_id,
                       record.model_dump(mode='json'), expected_revision=-1)
        if row:
            return await self.status(envelope.request_id)
        # This intent is committed before I/O. A crash here is deliberately unknown.
        try:
            receipt = await self.provider.submit(envelope)
            return self._save_receipt(envelope.request_id, receipt)
        except ProviderError as exc:
            return self._save_changes(envelope.request_id,
                                      state='submitted_unknown', last_error=exc.code)

    async def lookup(self, request_id: str):
        record = self._read(request_id)
        if record.receipt is not None:
            return await self.status(request_id)
        await self.capabilities('lookup_by_request_id')
        reply = await self.provider.lookup(request_id)
        if not isinstance(reply, LookupReply):
            raise ProviderError('invalid_protocol')
        if reply.receipt is not None:
            return self._save_receipt(request_id, reply.receipt)
        # A momentary miss can race delayed acceptance even after process death.
        # v1 carries no durable, irrevocable tombstone bound to this envelope.
        # Neither spelling of a negative is proof that execution cannot occur.
        return self._save_changes(request_id, state='submitted_unknown')

    async def status(self, request_id: str):
        record = self._read(request_id)
        if record.terminal:
            return record
        if record.receipt is None:
            return await self.lookup(request_id)
        await self.capabilities('status')
        return self._save_receipt(request_id, await self.provider.status(record.receipt.handle))

    async def cancel(self, request_id: str):
        record = self._read(request_id)
        if record.terminal or record.cancel_accepted:
            return record
        if record.receipt is None:
            record = await self.lookup(request_id)
        if record.receipt is None:
            raise ProviderError('acceptance_unknown')
        await self.capabilities('cancel')
        reply = await self.provider.cancel(record.receipt.handle)
        if not isinstance(reply, CancelReply):
            raise ProviderError('invalid_protocol')
        if (reply.request_id, reply.scope, reply.handle) != (request_id, self.scope, record.receipt.handle):
            raise ProviderError('cancel_binding_mismatch')
        # Accepted is only acknowledgement of the request, never terminal proof.
        return self._save_changes(request_id, cancel_accepted=reply.accepted)

    async def collect(self, request_id: str):
        record = self._read(request_id)
        if record.result is not None:
            return record.result
        if not record.terminal:
            raise ProviderError('not_terminal')
        await self.capabilities('collect')
        result = await self.provider.collect(record.receipt.handle)
        if not isinstance(result, CollectReply) or not result.receipt.terminal:
            raise ProviderError('invalid_collect_protocol')
        self._save_receipt(request_id, result.receipt, result=result)
        return result


class BridgeConfig(WireModel):
    endpoint: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]
    provider_id: Ref
    bearer_token: SecretStr = Field(repr=False)
    scope: ProviderScope
    timeout_seconds: Annotated[float, Field(strict=True, gt=0, le=300)]
    max_request_bytes: Annotated[int, Field(strict=True, gt=0, le=1048576)]
    max_response_bytes: Annotated[int, Field(strict=True, gt=0, le=1048576)]

    @field_validator('endpoint')
    @classmethod
    def safe_endpoint(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(ord(c) <= 32 for c in value)):
            raise ValueError('explicit HTTP(S) endpoint without embedded credentials/query required')
        if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', '::1', 'localhost'):
            raise ValueError('TLS required outside controlled loopback')
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError('invalid endpoint port') from exc
        return value.rstrip('/')

    @field_validator('bearer_token')
    @classmethod
    def safe_token(cls, value):
        token = value.get_secret_value()
        if not token or any(ord(c) <= 32 or ord(c) > 126 for c in token):
            raise ValueError('nonempty printable bearer token required')
        return value


class HttpAgentProvider:
    """Version 1 JSON RPC over HTTP POST, with explicit bearer authentication.

    One connection per call, no redirects/proxies/cookies. Deadline bounds the
    entire connect/write/read operation; response header and body sizes are capped.
    """
    def __init__(self, config: BridgeConfig):
        self.config = config

    async def _request(self, method, payload, model):
        query_type = (SubmitQuery if method == 'submit' else LookupQuery if method == 'lookup'
                      else ScopeQuery if method == 'capabilities' else HandleQuery)
        query = query_type(schema_version=1, scope=self.config.scope, **payload)
        body = json.dumps(query.model_dump(mode='json'), allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
        if len(body) > self.config.max_request_bytes:
            raise ProviderError('request_too_large')
        parsed = urlsplit(self.config.endpoint)

        async def exchange():
            tls = ssl.create_default_context() if parsed.scheme == 'https' else None
            reader, writer = await asyncio.open_connection(parsed.hostname,
                parsed.port or (443 if tls else 80), ssl=tls, limit=16384)
            try:
                path = (parsed.path.rstrip('/') or '') + '/v1/' + method
                host = parsed.netloc
                header = (f'POST {path} HTTP/1.1\r\nHost: {host}\r\n'
                    f'Authorization: Bearer {self.config.bearer_token.get_secret_value()}\r\n'
                    f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n'
                    'Connection: close\r\n\r\n').encode('ascii')
                writer.write(header + body)
                await writer.drain()
                raw_headers = await reader.readuntil(b'\r\n\r\n')
                lines = raw_headers.decode('iso-8859-1').split('\r\n')
                try:
                    protocol, code, *_ = lines[0].split(' ')
                    if protocol not in ('HTTP/1.0', 'HTTP/1.1'):
                        raise ValueError('invalid HTTP version')
                    status = int(code)
                    headers = {}
                    for line in lines[1:]:
                        if not line: continue
                        key, value = line.split(':', 1)
                        if key.lower() in headers:
                            raise ValueError('duplicate HTTP header')
                        headers[key.lower()] = value.strip()
                except ValueError as exc:
                    raise ProviderError('invalid_protocol') from exc
                if status in (401, 403):
                    raise ProviderError('authentication_failed')
                if status != 200:
                    raise ProviderError('http_status_' + str(status))
                if (headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json'
                        or 'transfer-encoding' in headers):
                    raise ProviderError('invalid_protocol')
                limit = self.config.max_response_bytes
                if 'content-length' in headers:
                    try:
                        length = int(headers['content-length'])
                    except ValueError as exc:
                        raise ProviderError('invalid_protocol') from exc
                    if length < 0:
                        raise ProviderError('invalid_protocol')
                    if length > limit:
                        raise ProviderError('response_too_large')
                    result = await reader.readexactly(length)
                else:
                    chunks, size = [], 0
                    while True:
                        chunk = await reader.read(min(65536, limit + 1 - size))
                        if not chunk: break
                        chunks.append(chunk); size += len(chunk)
                        if size > limit:
                            raise ProviderError('response_too_large')
                    result = b''.join(chunks)
                try:
                    _json(result.decode('utf-8'))
                    return model.model_validate_json(result)
                except (ValueError, UnicodeError) as exc:
                    raise ProviderError('invalid_protocol') from exc
            finally:
                writer.close()
                # close() suffices for cancellation; waiting on TLS close may block.

        try:
            return await asyncio.wait_for(exchange(), timeout=self.config.timeout_seconds)
        except ProviderError:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, UnicodeError) as exc:
            raise ProviderError('transport_unknown') from exc

    async def capabilities(self):
        result = await self._request('capabilities', {}, ProviderCapabilities)
        if result.provider_id != self.config.provider_id:
            raise ProviderError('provider_binding_mismatch')
        return result

    async def submit(self, envelope):
        if not isinstance(envelope, SubmitEnvelope) or envelope.scope != self.config.scope:
            raise ProviderError('envelope_scope_mismatch')
        result = await self._request('submit', {'envelope': envelope}, ProviderReceipt)
        AgentBridge._binding(RequestRecord(schema_version=1,provider_id=self.config.provider_id,
            envelope=envelope,state='submitted_unknown'), result)
        return result

    async def lookup(self, request_id):
        result = await self._request('lookup', {'request_id': request_id}, LookupReply)
        if result.receipt is not None and (result.receipt.scope != self.config.scope
                                          or result.receipt.request_id != request_id):
            raise ProviderError('receipt_binding_mismatch')
        return result

    async def status(self, handle):
        result = await self._request('status', {'handle': handle}, ProviderReceipt)
        if result.scope != self.config.scope or result.handle != handle:
            raise ProviderError('receipt_binding_mismatch')
        return result

    async def cancel(self, handle):
        result = await self._request('cancel', {'handle': handle}, CancelReply)
        if result.scope != self.config.scope or result.handle != handle:
            raise ProviderError('cancel_binding_mismatch')
        return result

    async def collect(self, handle):
        result = await self._request('collect', {'handle': handle}, CollectReply)
        if result.receipt.scope != self.config.scope or result.receipt.handle != handle:
            raise ProviderError('receipt_binding_mismatch')
        return result
