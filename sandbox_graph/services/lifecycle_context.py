"""Trusted, node-bound context assembly and sealed evidence capabilities.

Registration is an operator/producer API, never a model tool. Public read inputs
are opaque handles and ranges; neither paths nor accessible-evidence lists grant
permission. Raw artifacts remain immutable and are never returned to a model.
"""
from __future__ import annotations

import json
import re

from ..domain.agent_lifecycle import BudgetUsage, ContextPacket
from ..domain.models import EvidenceRef, NodeResult, canonical_hash
from ..environment.evidence import SecretRedactor, assert_public_payload_safe
from ..persistence.repository import Conflict
from .agent_lifecycle import LifecycleService, decode
from .lifecycle_budgets import LifecycleBudgetService
from .lifecycle_safety import LifecycleSafetyService


_IDENTITY_FIELDS = ('run_ref', 'scope', 'node_id', 'graph_ref', 'plan_ref', 'contract_ref')
_SENSITIVE = re.compile(r'authorization|(?:^|[-_])(?:secret|token|password)(?:$|[-_])|'
                        r'api[-_]?key|private[-_]?key|manifest[-_]?key', re.I)
_SECRET_LINE = re.compile(r'(?im)^(\s*[^\n:=]*(?:authorization|token|secret|password|api[-_]?key|private[-_]?key)[^\n:=]*\s*[:=])[^\n]*$')


def _positive(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{label} must be a positive integer')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


class LifecycleContextService:
    """Trusted producer facade; limits and secret registration come from wiring.

    Source preparation is bounded separately from model reads. A source larger
    than max_source_bytes must be prepared/redacted by a trusted producer first;
    it is never read whole and then silently truncated to fit a packet.
    """
    def __init__(self, repository, artifact_store, *, artifact_scope, secret_values=(),
                 max_read_bytes=65536, total_evidence_bytes=262144,
                 max_packet_bytes=32768, max_source_bytes=1048576):
        self.repository = repository
        self.store = artifact_store
        self.artifact_scope = artifact_scope
        self.derived_scope = artifact_scope + ':sanitized'
        self.max_read_bytes = _positive(max_read_bytes, 'per-read byte limit')
        self.total_evidence_bytes = _positive(total_evidence_bytes, 'total evidence byte limit')
        self.max_packet_bytes = _positive(max_packet_bytes, 'packet byte limit')
        self.max_source_bytes = _positive(max_source_bytes, 'source preparation byte limit')
        self._secrets = tuple(sorted({v for v in secret_values if isinstance(v, str) and v}, key=len, reverse=True))
        self._redactor = SecretRedactor(self._secrets)
        # Only this private service ledger gets the fingerprint, never a packet.
        self._sanitizer = canonical_hash({'secrets': self._secrets, 'version': 1})

    def _sanitize(self, value):
        def visit(item, key=None):
            if key and _SENSITIVE.search(key):
                return '[REDACTED]'
            if isinstance(item, dict):
                if any(secret in str(k) for k in item for secret in self._secrets):
                    raise ValueError('unsafe secret metadata key; trusted sanitization required')
                return {str(k): visit(v, str(k)) for k, v in item.items()}
            if isinstance(item, (list, tuple)):
                return [visit(v) for v in item]
            if isinstance(item, str):
                item = self._redactor.redact_text(item)
                for secret in self._secrets:
                    item = item.replace(secret, '[REDACTED]')
                return _SECRET_LINE.sub(r'\1[REDACTED]', item)
            return item
        result = visit(self._redactor.redact(value))
        try:
            assert_public_payload_safe(result, secret_values=self._secrets)
        except ValueError:
            raise ValueError('unsafe secret material; trusted sanitization required') from None
        return result

    @staticmethod
    def _record_in(tx, lifecycle_id, expected_revision=None, token=None):
        record = LifecycleService.load_in(tx, lifecycle_id)
        if token is not None:
            LifecycleService.assert_owner_in(tx, lifecycle_id, token)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != record.revision):
            raise Conflict('context lifecycle revision changed')
        source = tx.get('lifecycle_sources', lifecycle_id)
        receipt = tx.get('receipts', record.source_receipt)
        if not source or not receipt or source['body'].get('source_receipt') != record.source_receipt:
            raise Conflict('original lifecycle failure source missing')
        identity = source['body']['identity']
        expected = dict(run_ref=record.key.run_ref, scope=record.key.scope_ref,
            node_id=record.key.node_id, graph_ref=record.graph_ref, plan_ref=record.plan_ref)
        if (any(identity.get(k) != v for k, v in expected.items())
                or receipt['body'].get('identity') != identity
                or NodeResult.model_validate(receipt['body']['result']) != record.node_result):
            raise PermissionError('original failure identity or content mismatch')
        return record, receipt['body']

    @staticmethod
    def _identity(record):
        return dict(run_ref=record.key.run_ref, scope=record.key.scope_ref,
            node_id=record.key.node_id, graph_ref=record.graph_ref,
            plan_ref=record.plan_ref, contract_ref=record.contract_ref)

    def grant_evidence(self, lifecycle_id, *, artifact, producer_identity, visibility,
                       offline_sealed=False):
        """Trusted producer explicitly grants one original artifact to one node.

        Cross-node sharing must be explicit and still match run/scope/versions.
        Parent/private conversations are not supported evidence classes. The
        producer must bind this identity to its authoritative artifact record.
        """
        if visibility not in {'private', 'shared'} or type(offline_sealed) is not bool:
            raise PermissionError('only explicitly private or shared evidence is permitted')
        if not isinstance(artifact, EvidenceRef) or artifact.scope != self.artifact_scope:
            raise PermissionError('artifact must belong to the configured evidence store scope')
        # Pydantic model_copy can bypass validation. Restore the wire boundary
        # before even integrity hashing can open a filename from metadata.
        try:
            artifact = EvidenceRef.model_validate(artifact.model_dump())
        except ValueError:
            raise ValueError('invalid artifact metadata') from None
        with self.repository.transaction() as tx:
            record, _ = self._record_in(tx, lifecycle_id)
            target = self._identity(record)
            if (set(producer_identity) != set(_IDENTITY_FIELDS)
                    or any(producer_identity[k] != target[k] for k in _IDENTITY_FIELDS if k != 'node_id')
                    or (visibility == 'private' and producer_identity['node_id'] != target['node_id'])
                    or not isinstance(producer_identity['node_id'], str) or not producer_identity['node_id']):
                raise PermissionError('evidence grant expands run/scope/node/version')
        # A bounded complete sanitization happens before any public range exists.
        if artifact.size > self.max_source_bytes:
            raise ValueError('source exceeds preparation limit; request narrower evidence artifacts')
        self.store.verify(artifact)
        raw = self.store.open_range(artifact, scope=self.artifact_scope, offset=0,
                                    length=artifact.size, max_bytes=self.max_source_bytes)
        if len(raw) != artifact.size:
            raise ValueError('source evidence length changed')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('text evidence requires trusted UTF-8 conversion') from None
        try:
            value = json.loads(text)
        except ValueError:
            sanitized = self._sanitize(text).encode('utf-8')
            media_type = 'text/plain'
        else:
            sanitized = _json(self._sanitize(value)).encode('utf-8')
            media_type = 'application/json'
        if any(secret.encode('utf-8') in sanitized for secret in self._secrets):
            raise ValueError('secret material remains after serialization; trusted sanitization required')
        derived = self.store.commit([sanitized], scope=self.derived_scope,
                                    media_type=media_type, producer='lifecycle_context/v1')
        body = {'schema_version': 1, 'lifecycle_id': lifecycle_id, 'identity': target,
            'producer_identity': dict(producer_identity), 'visibility': visibility,
            'original': artifact.model_dump(mode='json'), 'derived': derived.model_dump(mode='json'),
            'offline_sealed': offline_sealed, 'sanitizer': self._sanitizer}
        handle = 'evidence-cap:' + canonical_hash(body)
        with self.repository.transaction() as tx:
            current, _ = self._record_in(tx, lifecycle_id)
            if self._identity(current) != target:
                raise Conflict('evidence identity changed during preparation')
            tx.put('lifecycle_context_grants', handle, body, immutable=True)
        return handle

    def _authorize_in(self, tx, lifecycle_id, *, token, evidence_mode, expected_revision=None):
        if evidence_mode not in {'live', 'offline_sealed'}:
            raise ValueError('explicit known evidence mode required')
        if token is None:
            raise PermissionError('current lifecycle owner required')
        record, receipt = self._record_in(tx, lifecycle_id, expected_revision, token)
        disposition = None
        if evidence_mode == 'live':
            row = tx.get('lifecycle_dispositions', lifecycle_id)
            if not row:
                raise Conflict('current resource disposition required')
            disposition = row['body']['receipt_ref']
            LifecycleSafetyService.verify_disposition_in(tx, disposition, record.key.scope_ref, token)
            proof_identity = tx.get('resource_disposition_receipts', disposition)['body']['identity']
            if record.key.scope_kind!='work':proof_identity=proof_identity|{'scope':proof_identity.get('scope_ref')}
            if any(proof_identity.get(k) != v for k, v in self._identity(record).items() if k != 'node_id'):
                raise PermissionError('live disposition differs from failure run/scope/version')
        return record, receipt, disposition

    def _grants_in(self, tx, record, evidence_mode):
        grants = []
        for row in tx.scan('lifecycle_context_grants'):
            body = row['body']
            if body.get('lifecycle_id') != record.key.lifecycle_id:
                continue
            if (body.get('identity') != self._identity(record)
                    or body.get('sanitizer') != self._sanitizer
                    or row['key'] != 'evidence-cap:' + canonical_hash(body)):
                raise PermissionError('evidence capability identity or sanitizer changed')
            if evidence_mode == 'offline_sealed' and body.get('offline_sealed') is not True:
                continue
            grants.append(row)
        # Publication order is stable across persistent repositories, which scan
        # by key; packet ordering must not depend on transaction scan order.
        return sorted(grants, key=lambda row: (row['body']['visibility'] == 'shared', row['key']))

    def _usage_in(self, tx, lifecycle_id):
        policy = dict(max_read_bytes=self.max_read_bytes, total_evidence_bytes=self.total_evidence_bytes,
                      max_packet_bytes=self.max_packet_bytes, sanitizer=self._sanitizer)
        row = tx.get('lifecycle_context_usage', lifecycle_id)
        if row:
            if row['body']['policy'] != policy:
                raise Conflict('context read policy changed; explicit trusted adjustment required')
            return row['body']
        body = {'bytes_read': 0, 'policy': policy}
        tx.put('lifecycle_context_usage', lifecycle_id, body, expected_revision=-1)
        return body

    def build(self, lifecycle_id, *, expected_revision, token, task_role='diagnosis', evidence_mode='live'):
        """Build only from authoritative lifecycle, source, grants and budgets."""
        from ..runtime.agents import node_supervisor_for
        with self.repository.transaction() as tx:
            record, receipt, disposition = self._authorize_in(tx, lifecycle_id, token=token,
                evidence_mode=evidence_mode, expected_revision=expected_revision)
            grants = self._grants_in(tx, record, evidence_mode)
            if evidence_mode == 'offline_sealed' and not grants:
                raise Conflict('offline diagnosis requires explicitly sealed evidence')
            self._usage_in(tx, lifecycle_id)
            snapshot = LifecycleBudgetService.snapshot_in(tx, lifecycle_id)['lifecycle']
            if snapshot['lineage_budget_ref'] != record.key.lineage_budget_ref:
                raise PermissionError('context lineage budget differs from lifecycle')
            totals = {k: snapshot['spent'][k] + snapshot['reserved'][k] for k in snapshot['spent']}
            safety = {k: snapshot['safety_spent'][k] + snapshot['safety_reserved'][k]
                      for k in snapshot['safety_spent']}
            usage = BudgetUsage(schema_version=1, **{k: totals[k] for k in
                ('repair_attempts', 'diagnosis_calls', 'candidate_revisions', 'child_jobs', 'observations')},
                tokens_reserved=totals['tokens'], stop_calls=safety['stop_calls'], cleanup_calls=safety['cleanup_calls'])
            error = receipt['result'].get('error')
            summary = f"{error['code']}: {error['message']}" if error else 'node execution or quality review failed'
            facts = [f'source_receipt={record.source_receipt}',
                     f'execution_status={record.node_result.execution_status}',
                     f'review_status={record.node_result.review_status}',
                     f'data_status={record.node_result.data_status}',
                     f'cleanup_status={record.node_result.cleanup_status}',
                     f'coverage_status={record.node_result.coverage_status}']
            if record.node_result.error:
                facts.extend((f'error_effect_certainty={record.node_result.error.effect_certainty}',
                              f'error_retry_class={record.node_result.error.retry_class}'))
            review = tx.get('node_reviews', record.source_receipt)
            if review:
                facts.append('source_review=' + _json(self._sanitize({
                    k: v for k, v in review['body'].items() if k in {'decision', 'findings'}})))
            packet = ContextPacket(schema_version=1, key=record.key,
                node_supervisor_id=node_supervisor_for(record.key.node_id, record.key.scope_ref, record.key.run_ref),
                task_role=task_role, graph_ref=record.graph_ref, plan_ref=record.plan_ref,
                contract_ref=record.contract_ref, failure_summary=self._sanitize(summary),
                facts=tuple(self._sanitize(fact) for fact in facts), hypotheses=(),
                evidence_refs=tuple(row['key'] for row in grants),
                evidence_scope_refs=(record.key.scope_ref,), evidence_byte_limit=self.total_evidence_bytes,
                budget_usage=usage, tool_capabilities=(), evidence_mode=evidence_mode,
                disposition_receipt_ref=disposition)
            try:
                assert_public_payload_safe(packet.model_dump(mode='json'), secret_values=self._secrets)
            except ValueError:
                raise ValueError('unsafe context metadata; trusted sanitization required') from None
            packet_bytes = packet.model_dump_json().encode('utf-8')
            if any(secret.encode('utf-8') in packet_bytes for secret in self._secrets):
                raise ValueError('secret material remains in context metadata')
            if len(packet_bytes) > self.max_packet_bytes:
                raise ValueError('context exceeds byte budget; request narrower evidence ranges')
            return packet

    def read_evidence(self, lifecycle_id, handle, *, offset, length, token,
                      evidence_mode='live', expected_revision=None):
        """Revalidate identity/disposition and atomically charge bytes before return."""
        if (type(offset) is not int or type(length) is not int or offset < 0
                or length <= 0 or length > self.max_read_bytes):
            raise ValueError('range exceeds evidence byte limit; request narrower evidence ranges')
        with self.repository.transaction() as tx:
            record, _receipt, _disposition = self._authorize_in(tx, lifecycle_id, token=token,
                evidence_mode=evidence_mode, expected_revision=expected_revision)
            grant = next((row['body'] for row in self._grants_in(tx, record, evidence_mode)
                          if row['key'] == handle), None)
            if grant is None:
                raise PermissionError('evidence handle not granted to this lifecycle node')
            ref = decode(EvidenceRef, grant['derived'])
            if offset > ref.size or offset + length > ref.size:
                raise ValueError('range exceeds sealed evidence size')
            usage = self._usage_in(tx, lifecycle_id)
            if usage['bytes_read'] + length > self.total_evidence_bytes:
                raise Conflict('cumulative evidence byte budget exhausted; request narrower evidence ranges')
            # Integrity can stream the whole original without loading it into a
            # context; model data comes only from the sanitized derived range.
            self.store.verify(decode(EvidenceRef, grant['original']))
            payload = self.store.open_range(ref, scope=self.derived_scope, offset=offset,
                                          length=length, max_bytes=self.max_read_bytes)
            if len(payload) != length:
                raise ValueError('sealed evidence range length changed')
            tx.put('lifecycle_context_usage', lifecycle_id,
                   usage | {'bytes_read': usage['bytes_read'] + len(payload)})
            return payload
