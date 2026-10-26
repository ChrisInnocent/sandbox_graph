"""Trusted, fenced repair execution and independent action-specific acceptance.

The worker supplies only a receipt reference. Results, review, operations,
collections, authority, budgets and resource closure are read from current
service ledgers in the committing transaction. This service never runs actions.
"""
from __future__ import annotations

import math

from ..catalog.repair_actions import standard_repair_catalog
from ..domain.agent_lifecycle import (LifecycleEvent, RepairOutcome, RepairPostcheckEvidence, RepairPolicy,
                                      TransitionGuards, transition_lifecycle)
from ..domain.models import NodeResult, canonical_hash
from ..persistence.repository import Conflict, StaleOwner
from ..runtime.node_agent import NodeReview
from ..runtime.runner import RunnerCollection, RunnerStatus
from ..runtime.tasks import TaskBackend
from .agent_lifecycle import LifecycleService, decode
from .lifecycle_budgets import LifecycleBudgetService
from .lifecycle_safety import LifecycleSafetyService


class RepairOutcomeService:
    @staticmethod
    def load_order_in(tx, job):
        TaskBackend.assert_claim_in(tx, job)
        ref = job['payload']['repair_order_ref']
        row = tx.get('repair_orders', ref)
        if not row:
            raise Conflict('modern repair order missing')
        order = row['body']
        request = tx.get('repair_requests', order['request_id'])
        if (order['order_ref'] != ref or job['job_kind'] != 'remedy'
                or order['lifecycle_id'] != job['payload'].get('lifecycle_id')
                or order['work_ref'] != job['scope_ref'] or order['run_ref'] != job['run_id']
                or not request or request['body']['result']['order_ref'] != ref
                or request['body']['result']['job_ref'] != job['job_id']):
            raise Conflict('repair order does not bind claimed job')
        return order

    @classmethod
    def assert_current_in(cls, tx, job, order, token, *, catalog=None):
        """Recheck frozen authority and every real owner at each publish boundary."""
        cls.load_order_in(tx, job)
        LifecycleService.assert_owner_in(tx, order['lifecycle_id'], token)
        LifecycleSafetyService.assert_work_in(tx, order['work_ref'], token)
        record = LifecycleService.load_in(tx, order['lifecycle_id'])
        from .lifecycle_stop import LifecycleStopService
        LifecycleStopService.assert_admission_in(tx,record)
        expected = (order['run_ref'], order['work_ref'], order['node_id'], order['source_receipt'],
                    order['plan_ref'], order['graph_ref'], order['contract_ref'], order['policy_ref'])
        if ((record.key.run_ref, record.key.scope_ref, record.key.node_id, record.source_receipt,
             record.plan_ref, record.graph_ref, record.contract_ref, record.policy_ref) != expected
                or record.key.scope_kind != 'work' or record.is_terminal
                or record.terminal_intent is not None or tx.now >= min(record.deadline, order['deadline'])):
            raise Conflict('repair lifecycle identity, stop intent or deadline changed')
        work = tx.get('work_instances', order['work_ref'])
        if (not work or work['revision'] != order['work_revision']
                or any(work['body'].get(k) != order[v] for k, v in (
                    ('run_id', 'run_ref'), ('plan_ref', 'plan_ref'), ('graph_ref', 'graph_ref'),
                    ('contract_ref', 'contract_ref')))):
            raise Conflict('frozen repair work revision or versions changed')
        gate, run = tx.get('dispatch_gates', order['run_ref']), tx.get('runs', order['run_ref'])
        if (not gate or not run or gate['body'].get('open') is not True
                or type(gate['body'].get('generation')) is not int
                or gate['body']['generation'] != order['dispatch_generation']
                or job['dispatch_generation'] != order['dispatch_generation']
                or run['body'].get('dispatch_generation') != order['dispatch_generation']
                or run['body'].get('graph_ref') != order['graph_ref']):
            raise Conflict('repair dispatch gate or generation changed')
        policy_row = tx.get('repair_policies', order['policy_ref'])
        if not policy_row:
            raise Conflict('repair policy missing')
        policy = decode(RepairPolicy, policy_row['body'])
        action_id, sep, version = order['action_ref'].rpartition('@')
        if not sep or order['action_ref'] not in policy.actions:
            raise PermissionError('repair action outside frozen policy')
        action = (catalog or standard_repair_catalog()).resolve(action_id, version, order['node_id']).require_available()
        authorization = tx.get('repair_authorizations', order['authorization_ref'])
        if not authorization or authorization['revision'] != order['authorization_revision']:
            raise PermissionError('repair authorization revision changed')
        auth = authorization['body']
        expiry = auth.get('expires_at')
        auth_identity = {k: order[k] for k in ('principal', 'run_ref', 'plan_ref', 'graph_ref',
                                             'contract_ref', 'work_revision')}
        auth_identity['scope_ref'] = order['work_ref']
        if (any(auth.get(k) != v for k, v in auth_identity.items())
                or auth.get('revoked') is not False or type(expiry) not in (int, float)
                or not math.isfinite(expiry) or expiry <= tx.now
                or order['action_ref'] not in auth.get('actions', ())
                or not set(action.permissions).issubset(auth.get('permissions', ()))):
            raise PermissionError('repair authorization no longer permits this action')
        source = tx.get('lifecycle_sources', order['lifecycle_id'])
        manifest = tx.get('repair_inputs', order['input_manifest_ref'])
        if not source or not manifest:
            raise Conflict('frozen repair provenance missing')
        inputs = decode(action.input_schema, manifest['body']['manifest'])
        expected_hash = canonical_hash({k: manifest['body'][k] for k in ('lifecycle_id', 'manifest')})
        if (manifest['body']['lifecycle_id'] != order['lifecycle_id']
                or manifest['body']['hash'] != expected_hash or order['input_hash'] != expected_hash
                or inputs.source_receipt != order['source_receipt']
                or inputs.input_refs != order['input_refs']
                or inputs.input_refs != source['body']['identity']['input_refs']
                or order['result_refs'] != source['body']['result_refs']):
            raise Conflict('frozen repair inputs or precursor results changed')
        LifecycleService._evidence_in(tx, record, inputs.evidence_refs)
        return record, action, policy

    @staticmethod
    def assert_interrupted_owner_in(tx, job, order, token, *, catalog=None):
        """Permit only a fenced fact handoff after the enclosing worker times out.

        Worker.run_once records its TimeoutError before cancelling the handler.
        That terminal task cannot authorize another action or an accepted
        outcome. Its still-live, unreplaced owners may record uncertainty.
        """
        row = tx.get('jobs', job['job_id'])
        actual = row['body'] if row else {}
        timeout = (actual.get('status') == 'failed'
                   and actual.get('result', {}).get('error') == {
                       'code': 'EXECUTION_FAILED', 'type': 'TimeoutError'})
        if (not row or (actual.get('status') not in {'running', 'draining'} and not timeout)
                or any(actual.get(key) != job.get(key) for key in (
                    'owner_epoch', 'owner_id', 'thread_id', 'activation_id',
                    'scope_ref', 'run_id', 'payload', 'dispatch_generation'))
                or actual.get('lease_until', 0) <= tx.now or actual.get('deadline', 0) <= tx.now):
            raise StaleOwner('interrupted repair task no longer owns factual handoff')
        LifecycleService.assert_owner_in(tx, order['lifecycle_id'], token)
        LifecycleSafetyService.assert_work_in(tx, order['work_ref'], token)
        frozen = tx.get('repair_orders', job['payload']['repair_order_ref'])
        request = tx.get('repair_requests', order['request_id'])
        if (not frozen or frozen['body'] != order or not request
                or request['body']['result']['job_ref'] != job['job_id']
                or request['body']['result']['order_ref'] != order['order_ref']
                or job['payload'].get('lifecycle_id') != order['lifecycle_id']
                or (job['scope_ref'], job['run_id']) != (order['work_ref'], order['run_ref'])):
            raise Conflict('interrupted repair order differs from its fenced job')
        record = LifecycleService.load_in(tx, order['lifecycle_id'])
        if (record.phase != 'remediating' or order['order_ref'] not in record.attempt_refs
                or (record.key.scope_ref, record.key.run_ref, record.key.node_id) != (
                    order['work_ref'], order['run_ref'], order['node_id'])):
            raise Conflict('interrupted repair lifecycle no longer owns this attempt')
        policy = decode(RepairPolicy, tx.get('repair_policies', order['policy_ref'])['body'])
        action_id, _, version = order['action_ref'].rpartition('@')
        action = (catalog or standard_repair_catalog()).resolve(action_id, version, order['node_id']).require_available()
        return record, action, policy

    @staticmethod
    def refresh_disposition_in(tx, order, token, *, ttl):
        """A predecessor owner's receipt is never a successor owner's permit."""
        ref = LifecycleSafetyService.produce_disposition_in(tx, order['work_ref'], token, ttl=ttl)
        disposition = LifecycleSafetyService.verify_disposition_in(
            tx, ref, order['work_ref'], token, require_closed=False)
        tx.put('lifecycle_dispositions', order['lifecycle_id'], {'receipt_ref': ref})
        tx.put('lifecycle_evidence', ref,
               {'lifecycle_id': order['lifecycle_id'], 'kind': 'resource_disposition'}, immutable=True)
        return ref, disposition

    @staticmethod
    def state(order, job):
        return {'thread_id': job['thread_id'], 'scope': order['work_ref'],
                'run_ref': order['run_ref'], 'plan_ref': order['plan_ref'],
                'graph_ref': order['graph_ref'], 'activation_id': job['activation_id'],
                'input_refs': dict(order['input_refs']), 'result_refs': dict(order['result_refs']),
                'node_visits': {order['node_id']: order['attempt']}}

    @classmethod
    def receipt_in(cls, tx, order, job):
        """Find only the exact immutable NodeExecutor visit, including its input hash."""
        state = cls.state(order, job)
        identity = {k: state[k] for k in ('thread_id', 'scope', 'run_ref', 'plan_ref',
                                          'graph_ref', 'activation_id', 'input_refs')}
        identity.update(node_id=order['node_id'], visit_number=order['attempt'])
        visit_key = f'{job["thread_id"]}:{job["activation_id"]}:{order["node_id"]}:{order["attempt"]}'
        visit = tx.get('visits', visit_key)
        if not visit:
            return None
        digest = canonical_hash(identity)
        if visit['body']['input_hash'] != digest:
            raise Conflict('repair executor visit input changed')
        ref = f'{job["thread_id"]}:{order["node_id"]}:{visit["body"]["visit_id"]}:{digest}'
        receipt = tx.get('receipts', ref)
        if receipt and (receipt['body']['identity'] != identity
                        or receipt['body']['visit_id'] != visit['body']['visit_id']):
            raise Conflict('repair receipt identity mismatch')
        return (ref, receipt['body']) if receipt else None

    @classmethod
    def start_in(cls, tx, job, order, token, *, ttl, catalog=None):
        record, action, policy = cls.assert_current_in(tx, job, order, token, catalog=catalog)
        old = tx.get('repair_executions', order['order_ref'])
        if old:
            if record.phase != 'remediating' or old['body']['job_ref'] != job['job_id']:
                raise Conflict('repair execution phase or job changed')
            return False
        if record.phase != 'remediation_pending':
            raise Conflict('repair order is not pending')
        from .lifecycle_rollout import LifecycleRolloutService
        LifecycleRolloutService.assert_enabled_in(tx,record,'action')
        if LifecycleSafetyService._repair_isolations_in(tx,order['work_ref']):
            raise Conflict('unresolved repair isolation prohibits another live action')
        failures = cls.failure_count_in(tx, order, record)
        if failures >= policy.max_same_failure:
            raise Conflict('repair strategy has made no progress since ordering')
        bounds = LifecycleBudgetService.snapshot_in(tx, order['lifecycle_id'])
        if any(tx.now >= bound['deadline'] for bound in bounds.values()):
            raise Conflict('reserved repair business deadline elapsed before execution')
        proof, disposition = cls.refresh_disposition_in(tx, order, token, ttl=ttl)
        LifecycleSafetyService.verify_disposition_in(tx, proof, order['work_ref'], token)
        reservation = tx.get('budget_reservations', order['budget_reservation_ref'])
        if (not reservation or reservation['body']['lifecycle_id'] != order['lifecycle_id']
                or reservation['body']['amounts'] != {'repair_attempts': 1, 'child_jobs': 1}
                or reservation['body']['input_ref'] != order['input_manifest_ref']
                or reservation['body']['metadata'] != {'action_ref': order['action_ref'], 'input_hash': order['input_hash']}
                or tx.get('budget_settlements', order['budget_reservation_ref'])):
            raise Conflict('repair does not hold its original unsettled allowance')
        event = LifecycleEvent(schema_version=1, event_id='repair-claimed:' + order['order_ref'],
            kind='claimed', source='lifecycle_repair_worker', expected_revision=record.revision,
            evidence_refs=(proof,), observed_at=tx.now)
        guards = TransitionGuards(schema_version=1, work_lease_valid=True,
            attempt_closed=disposition.stopped, disposition_closed=True, effect_known=True,
            effects_in_flight=False, authority_valid=True, action_registered=True,
            budget_available=True, same_failure_count=failures, max_same_failure=policy.max_same_failure)
        updated = transition_lifecycle(record, event, guards)
        LifecycleService.persist_transition_in(tx, record, event, updated)
        tx.put('repair_executions', order['order_ref'], {'order_ref': order['order_ref'],
            'lifecycle_id': order['lifecycle_id'], 'job_ref': job['job_id'],
            'thread_id': token.thread_id, 'owner_epoch': token.owner_epoch, 'lease_id': token.lease_id,
            'state': 'submission_started', 'started_at': tx.now,
            'operation_refs': [r['key'] for r in LifecycleSafetyService._rows_in(tx, 'operations', order['work_ref'])]}, immutable=True)
        return True

    @staticmethod
    def operation_statuses_in(tx, order, refs):
        statuses = {}
        for ref in refs:
            row = tx.get('operations', ref)
            if (not row or row['body'].get('scope') != order['work_ref']
                    or row['body'].get('run_id') != order['run_ref']):
                return None
            # Intent and submission_started are legitimate durable rows with
            # no remote status yet. Their absence is uncertainty, never a
            # schema failure that prevents the safety outcome from committing.
            if row['body'].get('phase') != 'terminal':
                return None
            try:
                status = RunnerStatus.model_validate(row['body'].get('status', {}))
            except ValueError:
                return None
            statuses[ref] = status
        return statuses

    @staticmethod
    def collection_in(tx, order, result):
        if not result.operation_refs:
            return None, []
        artifacts, refs = set(), []
        for operation in result.operation_refs:
            owned = tx.get('operations', operation)
            if (not owned or owned['body'].get('scope') != order['work_ref']
                    or owned['body'].get('run_id') != order['run_ref']):
                return None, []
            rows = [r for r in tx.scan('collections') if r['key'].startswith(operation + ':')]
            if not rows:
                return None, []
            # A collection from another operation with a shared string prefix is
            # excluded by requiring the exact OperationService key shape.
            rows = [r for r in rows if r['key'] == f'{operation}:{r["body"].get("last_seq")}']
            if not rows:
                return None, []
            row = max(rows, key=lambda r: r['body']['last_seq'])
            collection = RunnerCollection.model_validate(row['body'])
            if collection.interrupted or collection.missing_sequences:
                return None, []
            for seq in range(1, collection.last_seq + 1):
                event = tx.get('runner_events', f'{operation}:{seq:020}')
                if not event or event['body'].get('seq') != seq:
                    return None, []
            artifacts.update(collection.artifact_refs)
            refs.append(row['key'])
        # Composition of verified collections, not a proof supplied by the node.
        return RunnerCollection(artifact_refs=tuple(sorted(artifacts))), refs

    @staticmethod
    def failure_count_in(tx, order, record):
        from .repair_strategy import failure_count_in, strategy_input_hash
        return failure_count_in(tx, record, order['action_ref'], order['input_hash'],
            order.get('strategy_input_hash') or strategy_input_hash(record, order['input_refs']))

    @staticmethod
    def next_allowance_in(tx, record, policy, *, failures):
        bounds = LifecycleBudgetService.snapshot_in(tx, record.key.lifecycle_id)
        if tx.now >= min(record.deadline, policy.deadline) or failures >= policy.max_same_failure:
            return False
        for body in bounds.values():
            if tx.now >= body['deadline']:
                return False
            for counter in ('repair_attempts', 'child_jobs'):
                if body['spent'][counter] + body['reserved'][counter] >= body['limits'][counter]:
                    return False
        local = bounds['lifecycle']
        return local['spent']['repair_attempts'] + local['reserved']['repair_attempts'] < policy.max_repair_attempts

    @classmethod
    def record_in(cls, tx, job, order, token, *, ttl, interrupted=False, catalog=None):
        record, action, policy = (cls.assert_interrupted_owner_in(tx, job, order, token, catalog=catalog) if interrupted
                                  else cls.assert_current_in(tx, job, order, token, catalog=catalog))
        existing = tx.get('repair_outcomes', order['order_ref'])
        if existing:
            return existing['body']['return_result']
        if record.phase != 'remediating' or not tx.get('repair_executions', order['order_ref']):
            raise Conflict('repair outcome requires a started execution')
        receipt = cls.receipt_in(tx, order, job)
        proof, disposition = cls.refresh_disposition_in(tx, order, token, ttl=ttl)
        closed = (disposition.stopped and disposition.admission_status == 'closed'
                  and disposition.sampler_status in {'stopped', 'not_started'}
                  and not disposition.residual_resources and not disposition.isolation_domains)
        raw_refs, collection_refs = [proof], []
        result, review = None, None
        accepted = False
        if receipt:
            receipt_ref, body = receipt
            raw_refs.append(receipt_ref)
            result = NodeResult.model_validate(body['result'])
            review_row = tx.get('node_reviews', receipt_ref)
            if review_row:
                review = NodeReview.model_validate(review_row['body'])
                if review.owner_agent_id != f'agent:work:{order["work_ref"]}' or review.node_id != order['node_id']:
                    raise Conflict('repair review owner or node differs from receipt')
            statuses = cls.operation_statuses_in(tx, order, result.operation_refs)
            collection, collection_refs = (cls.collection_in(tx, order, result)
                                           if order['node_id'] == 'N07' else (None, []))
            accepted = closed and action.postcheck(result, review, disposition=disposition,
                checked_at=tx.now, operation_statuses=statuses, collection=collection)
            if order['node_id'] == 'N01':
                started = tx.get('repair_executions', order['order_ref'])['body']
                accepted = accepted and set(result.operation_refs).issubset(started['operation_refs'])
        operations = tuple(r['key'] for r in LifecycleSafetyService._rows_in(tx, 'operations', order['work_ref']))
        # Neither an absent remote reply nor exit=0 proves a closed effect.
        unknown = (interrupted or receipt is None or not closed
                   or (result.error is not None and result.error.effect_certainty == 'unknown'))
        if interrupted:
            accepted = False
        effect = 'unknown' if unknown else 'confirmed' if operations else 'none'
        postcheck = 'unknown' if unknown else 'accepted' if accepted else 'rejected'
        outcome = RepairOutcome(schema_version=1, order_ref=order['order_ref'],
            operation_refs=operations, effect_certainty=effect,
            execution_status=result.execution_status if result else 'blocked',
            data_status=result.data_status if result else 'unverified',
            cleanup_status=result.cleanup_status if result else 'unknown',
            postcheck_ref=action.postcheck_ref, raw_refs=tuple(raw_refs))
        evidence_ref = 'repair-outcome:' + order['order_ref']
        tx.put('lifecycle_evidence', evidence_ref,
               {'lifecycle_id': order['lifecycle_id'], 'kind': 'repair_outcome'}, immutable=True)
        postcheck_evidence_ref = None
        if receipt:
            check = RepairPostcheckEvidence(schema_version=1,
                lifecycle_id=order['lifecycle_id'], order_ref=order['order_ref'],
                action_ref=order['action_ref'], postcheck_ref=action.postcheck_ref,
                receipt_ref=receipt[0], receipt_hash=canonical_hash(receipt[1]),
                disposition_ref=proof, effect_certainty=effect, postcheck_status=postcheck,
                producer='repair_outcome_service', producer_version='1').model_dump(mode='json')
            postcheck_evidence_ref = 'repair-postcheck:' + canonical_hash(check)
            tx.put('repair_postchecks', postcheck_evidence_ref, check, immutable=True)
            LifecycleService.bind_evidence_in(tx, record, postcheck_evidence_ref, kind='repair_postcheck')
        settlement = LifecycleBudgetService.settle_in(tx, request_id=order['budget_reservation_ref'])
        from .repair_strategy import select_strategy_in, strategy_input_hash
        failures = 0
        if not accepted and not unknown:
            tx.put('repair_failure_fingerprints', order['order_ref'], {
                'lineage_budget_ref': record.key.lineage_budget_ref, 'action_ref': order['action_ref'],
                'input_hash': order['input_hash'],
                'strategy_input_hash': order.get('strategy_input_hash') or strategy_input_hash(record, order['input_refs']),
                'failure_fingerprint': record.issue_fingerprint,
                'receipt_ref': receipt[0], 'postcheck_evidence_ref': postcheck_evidence_ref}, immutable=True)
            failures = cls.failure_count_in(tx, order, record)
        alternate = None
        if not accepted and not unknown and failures >= policy.max_same_failure:
            alternate, _ = select_strategy_in(tx, record, policy, catalog or standard_repair_catalog())
        next_budget = cls.next_allowance_in(tx, record, policy,
            failures=0 if alternate else failures) if not accepted and not unknown else False
        next_due = None
        reason = None
        if unknown:
            LifecycleSafetyService._quarantine_in(tx, order['work_ref'], 'repair effect requires reconciliation', force=True)
            isolation = 'repair-isolation:' + order['order_ref']
            tx.put('repair_isolations', isolation, {'order_ref': order['order_ref'], 'work_ref': order['work_ref'],
                   'operation_refs': list(operations), 'reason': 'execution_or_disposition_unknown'}, immutable=True)
            # A missing operation binding cannot be invented to satisfy the
            # reconciling transition. Keep the started phase durably held.
            kind = 'unknown' if operations else None
            reason = 'effect_unknown' if operations else 'operation_binding_missing'
        elif accepted:
            kind = 'postcheck_pass'
        elif next_budget:
            kind = 'postcheck_fail_with_budget'
            if result.error and result.error.retry_class == 'bounded':
                due = tx.now + policy.backoff.seconds
                if due < record.deadline:
                    next_due = due
                else:
                    kind, next_budget, reason = 'postcheck_fail_exhausted', False, 'backoff_exceeds_deadline'
        else:
            kind = 'postcheck_fail_exhausted'
            reason = 'no_progress' if failures >= policy.max_same_failure else 'budget_exhausted'
        event_args = {}
        if unknown and operations:
            event_args.update(operation_kind='site', operation_refs=operations, isolation_refs=(isolation,))
        updated = record
        if kind is not None:
            event = LifecycleEvent(schema_version=1, event_id=evidence_ref, kind=kind,
                source='repair_outcome_service', expected_revision=record.revision,
                evidence_refs=(evidence_ref, *raw_refs), observed_at=tx.now,
                next_due_at=next_due, **event_args)
            guards = TransitionGuards(schema_version=1, work_lease_valid=True,
                attempt_closed=closed, disposition_closed=closed, effect_known=not unknown,
                effects_in_flight=unknown, postcheck_verified=not unknown,
                budget_available=next_budget, same_failure_count=failures,
                max_same_failure=policy.max_same_failure, new_evidence=bool(alternate))
            updated = transition_lifecycle(record, event, guards)
            LifecycleService.persist_transition_in(tx, record, event, updated)
        returned = {'execution_status': 'succeeded' if accepted and not unknown else 'blocked',
            'output': {'repair_order_ref': order['order_ref'], 'lifecycle_id': order['lifecycle_id'],
                       'receipt_ref': receipt[0] if receipt else None, 'attempt': order['attempt'],
                       'phase': updated.phase, 'postcheck_status': postcheck, 'reason': reason}}
        tx.put('repair_outcomes', order['order_ref'], {'outcome': outcome.model_dump(mode='json'),
            'postcheck_status': postcheck, 'postcheck_evidence_ref': postcheck_evidence_ref,
            'collection_refs': collection_refs,
            'interrupted': interrupted,
            'settlement_ref': order['budget_reservation_ref'], 'charged': settlement['charged'],
            'return_result': returned}, immutable=True)
        kind = ('repair_reconciliation_required' if unknown else
                'repair_recovery_required' if accepted else 'repair_retry_required' if next_budget else 'repair_blocked')
        tx.put('outbox', 'repair-outcome:' + order['order_ref'], {'kind': kind,
            'lifecycle_id': order['lifecycle_id'], 'order_ref': order['order_ref'],
            'outcome_ref': order['order_ref'], 'phase': updated.phase,
            'receipt_ref': receipt[0] if receipt else None, 'operation_refs': list(operations),
            'next_due_at': next_due, 'reason': reason}, immutable=True)
        return returned
