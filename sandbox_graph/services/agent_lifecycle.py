"""Fenced lifecycle records and transactional diagnostic intent.

Only trusted services call this API. Models submit proposals, not guard values.
Legacy remedy records are deliberately not reinterpreted as lifecycle records.
"""
from __future__ import annotations

import json

from ..catalog.registry import NODE_GROUPS
from ..domain.agent_lifecycle import (
    LifecycleEvent, LifecycleKey, LifecycleRecord, RepairPolicy, TransitionGuards,
    transition_lifecycle,
)
from ..domain.models import NodeResult, canonical_hash
from ..persistence.repository import Conflict, LeaseToken, StaleOwner


def decode(model, body):
    """Repository JSON arrays are wire data, not strict Python tuple inputs."""
    return model.model_validate_json(json.dumps(body))


class LifecycleService:
    def __init__(self, repository):
        self.repository = repository

    def register_policy(self, ref: str, policy: RepairPolicy):
        if not ref or not isinstance(policy, RepairPolicy):
            raise ValueError('versioned policy reference and RepairPolicy required')
        with self.repository.transaction() as tx:
            tx.put('repair_policies', ref, policy.model_dump(mode='json'), immutable=True)

    @staticmethod
    def _scope_in(tx, identity):
        run = tx.get('runs', identity['run_ref'])
        if not run:
            raise Conflict('failure run missing')
        run = run['body']
        if identity['graph_ref'] != run['graph_ref']:
            raise Conflict('receipt graph differs from its run')
        group = next((g for g, nodes in NODE_GROUPS.items() if identity['node_id'] in nodes), None)
        if group is None:
            raise Conflict('unregistered lifecycle node')
        if group == 'work':
            row = tx.get('work_instances', identity['scope'])
            if not row:
                raise Conflict('failure work missing')
            work = row['body']
            if (work['run_id'], work['plan_ref'], work['graph_ref']) != (
                    identity['run_ref'], identity['plan_ref'], identity['graph_ref']):
                raise PermissionError('failure work identity mismatch')
            return 'work', run, work['contract_ref'], row['revision']
        if group == 'group':
            rows = [row for row in tx.scan('groups') if (
                row['body']['run_id'], row['body']['group_id'], row['body']['plan_ref']) == (
                    identity['run_ref'], identity['scope'], identity['plan_ref'])]
            if not rows:
                raise PermissionError('failure group identity mismatch')
            return 'group', run, run['contract_ref'], rows[0]['revision']
        if identity['scope'] != identity['run_ref'] or identity['plan_ref'] != run['active_plan_ref']:
            raise PermissionError('root lifecycle scope mismatch')
        return 'run', run, run['contract_ref'], 0

    @staticmethod
    def _assert_scope_token(tx, identity, token):
        tx.assert_fence(token)
        if token.thread_id == identity['thread_id']:
            return
        # A successor supervisor must have an actual live job in this scope.
        for row in tx.scan('jobs'):
            job = row['body']
            if (job['thread_id'] == token.thread_id
                    and job.get('run_id') == identity['run_ref']
                    and job['scope_ref'] == identity['scope']
                    and job['status'] in {'running', 'draining'}
                    and job.get('lease_until', 0) > tx.now
                    and job['deadline'] > tx.now):
                return
        raise PermissionError('thread lease does not own failure scope')

    @staticmethod
    def load_in(tx, lifecycle_id):
        row = tx.get('agent_lifecycles', lifecycle_id)
        if not row:
            raise KeyError(lifecycle_id)
        return decode(LifecycleRecord, row['body'])

    @staticmethod
    def assert_owner_in(tx, lifecycle_id, token):
        tx.assert_fence(token)
        owner = tx.get('lifecycle_owners', lifecycle_id)
        if not owner or tuple(owner['body'][k] for k in ('thread_id', 'owner_epoch', 'lease_id')) != (
                token.thread_id, token.owner_epoch, token.lease_id):
            raise StaleOwner('lifecycle owner differs from thread owner')

    @staticmethod
    def _owner_body(token):
        return {'thread_id': token.thread_id, 'owner_epoch': token.owner_epoch,
                'lease_id': token.lease_id}

    def observe_failure(self, receipt_ref, *, policy_ref, token):
        with self.repository.transaction() as tx:
            return self.observe_failure_in(tx, receipt_ref, policy_ref=policy_ref, token=token)

    @classmethod
    def observe_failure_in(cls, tx, receipt_ref, *, policy_ref, token):
        receipt = tx.get('receipts', receipt_ref)
        if not receipt:
            raise KeyError(receipt_ref)
        identity = receipt['body']['identity']
        cls._assert_scope_token(tx, identity, token)
        scope_kind, run, contract, work_revision = cls._scope_in(tx, identity)
        work = tx.get('work_instances',identity['scope'])
        if work and work['body'].get('source_lifecycle_ref'):
            raise Conflict('generation failure is linked to original lifecycle; explicit decision required')
        result = NodeResult.model_validate(receipt['body']['result'])
        review = tx.get('node_reviews', receipt_ref)
        if not (result.execution_status in {'failed', 'blocked'} or result.review_status == 'rejected'
                or (review and review['body']['decision'] == 'reject')):
            raise Conflict('receipt has no execution failure or quality rejection')
        policy_row = tx.get('repair_policies', policy_ref)
        if not policy_row:
            raise Conflict('registered repair policy missing')
        policy = decode(RepairPolicy, policy_row['body'])
        lifecycle_id = 'lifecycle:' + canonical_hash({'source_receipt': receipt_ref})
        old = tx.get('agent_lifecycles', lifecycle_id)
        if old:
            record = decode(LifecycleRecord, old['body'])
            if record.policy_ref != policy_ref:
                raise Conflict('failure policy cannot be replaced by observation replay')
            return record
        fingerprint = canonical_hash({'node': identity['node_id'],
            'execution': result.execution_status, 'review': result.review_status,
            'quality': review['body']['decision'] if review else None,
            'error_code': result.error.code if result.error else None,
            'input_refs': identity['input_refs']})
        record = LifecycleRecord(schema_version=1,
            key=LifecycleKey(schema_version=1, run_ref=identity['run_ref'],
                scope_kind=scope_kind, scope_ref=identity['scope'], node_id=identity['node_id'],
                lifecycle_id=lifecycle_id, lineage_budget_ref=run['lineage_id']),
            graph_ref=identity['graph_ref'], plan_ref=identity['plan_ref'], contract_ref=contract,
            policy_ref=policy_ref, phase='draining', revision=0, owner_epoch=token.owner_epoch,
            source_receipt=receipt_ref, issue_fingerprint=fingerprint,
            deadline=policy.deadline, node_result=result)
        tx.put('agent_lifecycles', lifecycle_id, record.model_dump(mode='json'), immutable=True)
        tx.put('lifecycle_owners', lifecycle_id, cls._owner_body(token))
        supervision = tx.get('agent_supervision', receipt_ref)
        tx.put('lifecycle_sources', lifecycle_id, {'source_receipt': receipt_ref,
            'identity': identity, 'work_revision': work_revision,
            'result_refs': supervision['body'].get('result_refs', {}) if supervision else {}}, immutable=True)
        tx.put('outbox', 'lifecycle-observed:' + lifecycle_id,
            {'kind': 'lifecycle_observed', 'lifecycle_id': lifecycle_id,
             'source_receipt': receipt_ref}, immutable=True)
        return record

    def claim(self, lifecycle_id, *, token):
        with self.repository.transaction() as tx:
            record = self.load_in(tx, lifecycle_id)
            identity = tx.get('lifecycle_sources', lifecycle_id)['body']['identity']
            self._assert_scope_token(tx, identity, token)
            owner = tx.get('lifecycle_owners', lifecycle_id)['body']
            if owner == self._owner_body(token):
                return record
            old_token = LeaseToken(owner['thread_id'], owner['owner_epoch'], owner['lease_id'], 0)
            try:
                tx.assert_fence(old_token)
            except StaleOwner:
                pass
            else:
                raise Conflict('previous lifecycle owner remains active')
            tx.put('lifecycle_owners', lifecycle_id, self._owner_body(token))
            # Owner takeover does not allocate an attempt or rewrite event history.
            updated = record.model_copy(update={'owner_epoch': token.owner_epoch})
            row = tx.get('agent_lifecycles', lifecycle_id)
            tx.put('agent_lifecycles', lifecycle_id, updated.model_dump(mode='json'),
                   expected_revision=row['revision'])
            return updated

    @staticmethod
    def evidence_binding_key(lifecycle_id, evidence_ref):
        """Same immutable work proof may be referenced by distinct lifecycles."""
        return 'lifecycle-evidence:' + canonical_hash({
            'lifecycle_id': lifecycle_id, 'evidence_ref': evidence_ref})

    @classmethod
    def bind_evidence_in(cls, tx, record, evidence_ref, *, kind):
        """Trusted producer binding; proof verification precedes this call.

        Do not reinterpret or overwrite the legacy globally keyed evidence row.
        """
        if not isinstance(evidence_ref, str) or not evidence_ref or not kind:
            raise ValueError('evidence reference and producer kind required')
        body = {'lifecycle_id': record.key.lifecycle_id,
                'evidence_ref': evidence_ref, 'kind': kind}
        key = cls.evidence_binding_key(record.key.lifecycle_id, evidence_ref)
        tx.put('lifecycle_evidence_bindings', key, body, immutable=True)
        return key

    @classmethod
    def _evidence_in(cls, tx, record, evidence_refs):
        if not evidence_refs or len(set(evidence_refs)) != len(evidence_refs):
            raise ValueError('nonempty distinct evidence references required')
        for ref in evidence_refs:
            receipt = tx.get('receipts', ref)
            if receipt:
                identity = receipt['body']['identity']
                if (identity['run_ref'], identity['scope']) == (record.key.run_ref, record.key.scope_ref):
                    continue
            # Event-specific facts remain authoritative service ledger records.
            pair = tx.get('lifecycle_evidence_bindings',
                          cls.evidence_binding_key(record.key.lifecycle_id, ref))
            if (pair and pair['body'].get('lifecycle_id') == record.key.lifecycle_id
                    and pair['body'].get('evidence_ref') == ref):
                continue
            # Legacy rows are read-only and authorize only their original life.
            evidence = tx.get('lifecycle_evidence', ref)
            if evidence and evidence['body'].get('lifecycle_id') == record.key.lifecycle_id:
                continue
            raise PermissionError('evidence is missing or belongs to another lifecycle scope')

    @staticmethod
    def guards_in(tx, record, event):
        """Fail closed until each producer has published identity-bound proof."""
        from .lifecycle_safety import LifecycleSafetyService
        policy = decode(RepairPolicy, tx.get('repair_policies', record.policy_ref)['body'])
        owner = tx.get('lifecycle_owners', record.key.lifecycle_id)['body']
        token = LeaseToken(owner['thread_id'], owner['owner_epoch'], owner['lease_id'], 0)
        try:
            LifecycleSafetyService.assert_work_in(tx, record.key.scope_ref, token)
        except (Conflict, KeyError):
            return TransitionGuards(schema_version=1, max_same_failure=policy.max_same_failure)
        closed = False
        proof = tx.get('lifecycle_dispositions', record.key.lifecycle_id)
        if proof:
            try:
                LifecycleSafetyService.verify_disposition_in(tx, proof['body']['receipt_ref'],
                                                            record.key.scope_ref, token)
                closed = True
            except Conflict:
                pass
        return TransitionGuards(schema_version=1, work_lease_valid=True,
            attempt_closed=closed, disposition_closed=closed, effect_known=closed,
            effects_in_flight=not closed, max_same_failure=policy.max_same_failure)

    def bind_disposition(self, lifecycle_id, receipt_ref, *, token):
        from .lifecycle_safety import LifecycleSafetyService
        with self.repository.transaction() as tx:
            self.assert_owner_in(tx, lifecycle_id, token)
            record = self.load_in(tx, lifecycle_id)
            receipt = LifecycleSafetyService.verify_disposition_in(
                tx, receipt_ref, record.key.scope_ref, token, require_closed=False)
            tx.put('lifecycle_dispositions', lifecycle_id, {'receipt_ref': receipt_ref})
            self.bind_evidence_in(tx, record, receipt_ref, kind='resource_disposition')
            return receipt

    @classmethod
    def advance_in(cls, tx, lifecycle_id, event, *, expected_revision, token):
        cls.assert_owner_in(tx, lifecycle_id, token)
        record = cls.load_in(tx, lifecycle_id)
        event = LifecycleEvent.model_validate(event)
        if event.expected_revision != expected_revision:
            raise Conflict('event revision differs from command revision')
        cls._evidence_in(tx, record, event.evidence_refs)
        existing = tx.get('lifecycle_events', event.event_id)
        encoded = {'lifecycle_id': lifecycle_id, 'event': event.model_dump(mode='json')}
        if existing:
            if existing['body'] != encoded:
                raise Conflict('event ID input conflict')
            return record
        if record.revision != expected_revision:
            raise Conflict('lifecycle revision conflict')
        if event.observed_at > tx.now:
            raise Conflict('future lifecycle event time')
        if tx.now >= record.deadline and event.kind in {
                'known_action', 'needs_code', 'claimed', 'timer_due', 'tests_fail'}:
            raise Conflict('lifecycle deadline prohibits new work')
        updated = transition_lifecycle(record, event, cls.guards_in(tx, record, event))
        return cls.persist_transition_in(tx, record, event, updated)

    @staticmethod
    def persist_transition_in(tx, record, event, updated):
        """Internal service commit primitive; callers already verified guards/fence."""
        lifecycle_id = record.key.lifecycle_id
        encoded = {'lifecycle_id': lifecycle_id, 'event': event.model_dump(mode='json')}
        row = tx.get('agent_lifecycles', lifecycle_id)
        tx.put('agent_lifecycles', lifecycle_id, updated.model_dump(mode='json'),
               expected_revision=row['revision'])
        tx.put('lifecycle_events', event.event_id, encoded, immutable=True)
        tx.put('outbox', 'lifecycle-event:' + event.event_id,
            {'kind': 'lifecycle_advanced', 'lifecycle_id': lifecycle_id,
             'event_ref': event.event_id, 'revision': updated.revision}, immutable=True)
        return updated

    def advance(self, lifecycle_id, event, *, expected_revision, token):
        with self.repository.transaction() as tx:
            return self.advance_in(tx, lifecycle_id, event, expected_revision=expected_revision, token=token)

    def record_decision(self, lifecycle_id, *, request_id, expected_revision,
                        route, evidence_refs, token, action_ref=None, proposal_ref=None, strategy_selection=None):
        with self.repository.transaction() as tx:
            decision = self.record_decision_in(tx, lifecycle_id, request_id=request_id,
                expected_revision=expected_revision, route=route, evidence_refs=evidence_refs,
                token=token, action_ref=action_ref, proposal_ref=proposal_ref)
            if strategy_selection is not None:
                if (route != 'known_action' or strategy_selection['action_ref'] != action_ref
                        or strategy_selection['evidence_refs'] != list(evidence_refs)):
                    raise Conflict('strategy selection differs from decision')
                tx.put('repair_strategy_selections', decision['decision_ref'], strategy_selection, immutable=True)
            return decision

    @classmethod
    def record_decision_in(cls, tx, lifecycle_id, *, request_id, expected_revision,
                           route, evidence_refs, token, action_ref=None, proposal_ref=None):
        """Same authority checks inside a caller's fenced atomic result commit."""
        if route not in {'known_action', 'needs_code', 'needs_input', 'no_safe_route'}:
            raise ValueError('unknown decision route')
        if not request_id:
            raise ValueError('decision request ID required')
        body = {'lifecycle_id': lifecycle_id, 'expected_revision': expected_revision,
                'route': route, 'evidence_refs': list(evidence_refs), 'action_ref': action_ref,
                'proposal_ref': proposal_ref}
        digest = canonical_hash(body)
        cls.assert_owner_in(tx, lifecycle_id, token)
        record = cls.load_in(tx, lifecycle_id)
        cls._evidence_in(tx, record, evidence_refs)
        previous = tx.get('lifecycle_decision_requests', request_id)
        if previous:
            if previous['body']['hash'] != digest:
                raise Conflict('decision request input conflict')
            return tx.get('lifecycle_decisions', previous['body']['decision_ref'])['body']
        if record.revision != expected_revision:
            raise Conflict('decision lifecycle revision conflict')
        if record.is_terminal or tx.now >= record.deadline:
            raise Conflict('decision lifecycle is terminal or expired')
        if route in {'known_action','needs_code'}:
            from .lifecycle_rollout import LifecycleRolloutService
            LifecycleRolloutService.assert_enabled_in(tx,record,'action' if route=='known_action' else 'candidate')
        decision_ref = 'decision:' + canonical_hash({'request_id': request_id, 'hash': digest})
        decision = body | {'decision_ref': decision_ref, 'source_receipt': record.source_receipt,
                           'producer': cls._owner_body(token)}
        tx.put('lifecycle_decisions', decision_ref, decision, immutable=True)
        tx.put('lifecycle_decision_requests', request_id,
               {'hash': digest, 'decision_ref': decision_ref}, immutable=True)
        tx.put('outbox', 'decision-ready:' + decision_ref,
               {'kind': 'decision_ready', 'lifecycle_id': lifecycle_id,
                'decision_ref': decision_ref}, immutable=True)
        return decision

    def consume_decision(self, decision_ref, *, token, dispatch_in):
        """Trusted in-process consumer executes dispatch under the SAME UoW.

        dispatch_in must perform only ledger operations; external calls are jobs.
        This hook is configured by the application, never by a model or payload.
        """
        with self.repository.transaction() as tx:
            row = tx.get('lifecycle_decisions', decision_ref)
            if not row:
                raise KeyError(decision_ref)
            decision = row['body']
            self.assert_owner_in(tx, decision['lifecycle_id'], token)
            previous = tx.get('lifecycle_decision_consumptions', decision_ref)
            if previous:
                return previous['body']['result']
            record = self.load_in(tx, decision['lifecycle_id'])
            if record.revision != decision['expected_revision'] or record.is_terminal:
                raise Conflict('decision is stale')
            if decision['route'] in {'known_action','needs_code'}:
                from .lifecycle_rollout import LifecycleRolloutService
                LifecycleRolloutService.assert_enabled_in(tx,record,'action' if decision['route']=='known_action' else 'candidate')
            result = dispatch_in(tx, decision)
            tx.put('lifecycle_decision_consumptions', decision_ref,
                   {'result': result, 'lifecycle_id': decision['lifecycle_id']}, immutable=True)
            return result
