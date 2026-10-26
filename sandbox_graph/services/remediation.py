"""有界失败循环：归属 agent 修复**自己**工作节点（WF-04 循环工程）。

约束（与"可复核"红线一致）：
- agent 只能**发起**该节点已注册的修复动作；未注册/越界一律拒绝；
- 修复 = **新 attempt + 新 activation** 重执行该节点；**旧 receipt/证据不可变**，
  绝不在原地改测量结果或证据；
- 每个 `(work_ref, node_id)` 有 `max_attempts` 上界；耗尽后显式终态阻塞（不无限循环）。
"""
import math

from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from ..runtime.tasks import JobSpec, TaskBackend

# 节点 → 已注册的修复动作（逻辑能力名）。未列出的节点没有可发起动作。
REGISTERED_NODE_ACTIONS = {
    'N01': 'case.reconcile',
    'N07': 'evidence.seal',
    'N09': 'resource.cleanup',
}


class NodeRemediationService:
    def __init__(self, repository, *, max_attempts=2, repair_catalog=None):
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError('max_attempts must be a positive integer')
        self.repository = repository
        self.max_attempts = max_attempts
        self.repair_catalog = repair_catalog

    def register_input(self, lifecycle_id, ref, manifest, *, token):
        """Freeze schema-checked inputs; arbitrary model fields cannot become argv."""
        from ..catalog.repair_actions import RepairActionInput
        from .agent_lifecycle import LifecycleService, decode
        if not isinstance(ref, str) or not ref:
            raise ValueError('input manifest reference required')
        value = decode(RepairActionInput, manifest)
        with self.repository.transaction() as tx:
            LifecycleService.assert_owner_in(tx, lifecycle_id, token)
            record = LifecycleService.load_in(tx, lifecycle_id)
            if value.source_receipt != record.source_receipt:
                raise PermissionError('input manifest source differs from failure')
            LifecycleService._evidence_in(tx, record, value.evidence_refs)
            source = tx.get('lifecycle_sources', lifecycle_id)['body']
            if value.input_refs != source['identity']['input_refs']:
                raise Conflict('repair inputs changed; new authorized contract required')
            body = {'lifecycle_id': lifecycle_id, 'manifest': value.model_dump(mode='json')}
            tx.put('repair_inputs', ref, body | {'hash': canonical_hash(body)}, immutable=True)
            return ref

    def request_remediation(self, request, *, principal, token):
        with self.repository.transaction() as tx:
            return self.request_remediation_in(tx, request, principal=principal, token=token, catalog=self.repair_catalog)

    @staticmethod
    def request_remediation_in(tx, request, *, principal, token, catalog=None):
        """Authorized request → allowance, attempt, order, job and event atomically.

        principal is supplied by the application authentication boundary. The
        caller cannot provide attempts, generations, budgets or guard booleans.
        """
        from ..catalog.repair_actions import standard_repair_catalog
        from ..domain.agent_lifecycle import (RepairRequest, RepairPolicy,
            LifecycleEvent, TransitionGuards, transition_lifecycle)
        from .agent_lifecycle import LifecycleService, decode
        from .lifecycle_budgets import LifecycleBudgetService
        from .lifecycle_safety import LifecycleSafetyService
        request = RepairRequest.model_validate(request)
        LifecycleService.assert_owner_in(tx, request.lifecycle_id, token)
        record = LifecycleService.load_in(tx, request.lifecycle_id)
        digest = canonical_hash({'request': request.model_dump(mode='json'), 'principal': principal})
        previous = tx.get('repair_requests', request.request_id)
        if previous:
            if previous['body']['hash'] != digest:
                raise Conflict('repair request ID input conflict')
            return previous['body']['result']
        if record.revision != request.expected_revision or record.phase != 'diagnosing':
            raise Conflict('repair request lifecycle revision or phase changed')
        from .lifecycle_rollout import LifecycleRolloutService
        LifecycleRolloutService.assert_enabled_in(tx,record,'action')
        from .lifecycle_stop import LifecycleStopService
        LifecycleStopService.assert_admission_in(tx,record)
        if record.source_receipt != request.source_receipt:
            raise Conflict('repair request source receipt changed')
        if tx.now >= record.deadline:
            raise Conflict('repair lifecycle expired')
        policy = decode(RepairPolicy, tx.get('repair_policies', record.policy_ref)['body'])
        if request.action_ref not in policy.actions:
            raise PermissionError('action outside lifecycle policy')
        action_id, separator, version = request.action_ref.rpartition('@')
        if not separator:
            raise ValueError('versioned action reference required')
        action = (catalog or standard_repair_catalog()).resolve(action_id, version, record.key.node_id).require_available()
        work_row = tx.get('work_instances', record.key.scope_ref)
        if record.key.scope_kind != 'work' or not work_row:
            raise Conflict('registered repair currently requires a work scope')
        source = tx.get('lifecycle_sources', request.lifecycle_id)['body']
        if work_row['revision'] != source['work_revision']:
            raise Conflict('work revision changed; observe/revalidate current work')
        work = work_row['body']
        if (work['run_id'], work['plan_ref'], work['graph_ref'], work['contract_ref']) != (
                record.key.run_ref, record.plan_ref, record.graph_ref, record.contract_ref):
            raise Conflict('repair work versions changed')
        gate = tx.get('dispatch_gates', record.key.run_ref)
        run = tx.get('runs', record.key.run_ref)
        if (not gate or gate['body'].get('open') is not True or not run
                or type(gate['body'].get('generation')) is not int
                or gate['body']['generation'] < 1
                or gate['body']['generation'] != run['body']['dispatch_generation']):
            raise Conflict('repair dispatch gate closed or generation changed')
        authorization = tx.get('repair_authorizations', request.authorization_ref)
        if not authorization:
            raise PermissionError('repair authorization missing')
        auth = authorization['body']
        expiry = auth.get('expires_at')
        expected = {'principal': principal, 'run_ref': record.key.run_ref,
            'scope_ref': record.key.scope_ref, 'plan_ref': record.plan_ref,
            'graph_ref': record.graph_ref, 'contract_ref': record.contract_ref,
            'work_revision': work_row['revision']}
        if (any(auth.get(k) != v for k, v in expected.items())
                or auth.get('revoked') is not False
                or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= tx.now
                or request.action_ref not in auth.get('actions', ())
                or not set(action.permissions).issubset(auth.get('permissions', ()))):
            raise PermissionError('repair authorization scope/permission/validity mismatch')
        decision = tx.get('lifecycle_decisions', request.proposal_ref)
        if not decision or any(decision['body'].get(k) != v for k, v in {
                'lifecycle_id': request.lifecycle_id, 'source_receipt': record.source_receipt,
                'expected_revision': record.revision, 'route': 'known_action',
                'action_ref': request.action_ref}.items()):
            raise Conflict('decision does not request this repair at this revision')
        manifest = tx.get('repair_inputs', request.input_manifest_ref)
        if not manifest or manifest['body']['lifecycle_id'] != request.lifecycle_id:
            raise Conflict('repair input manifest missing or wrong scope')
        inputs = decode(action.input_schema, manifest['body']['manifest'])
        if inputs.source_receipt != record.source_receipt or inputs.input_refs != source['identity']['input_refs']:
            raise Conflict('repair input provenance changed')
        LifecycleService._evidence_in(tx, record, inputs.evidence_refs)
        LifecycleSafetyService.assert_work_in(tx, record.key.scope_ref, token)
        disposition = tx.get('lifecycle_dispositions', request.lifecycle_id)
        if not disposition:
            raise Conflict('resource disposition proof required')
        LifecycleSafetyService.verify_disposition_in(tx, disposition['body']['receipt_ref'],
                                                    record.key.scope_ref, token)
        from .repair_strategy import failure_count_in, strategy_input_hash
        stable_hash = strategy_input_hash(record, inputs.input_refs)
        failures = failure_count_in(tx, record, request.action_ref, manifest['body']['hash'], stable_hash)
        if failures >= policy.max_same_failure:
            raise Conflict('same repair strategy has made no progress')
        reservation = 'repair-budget:' + request.request_id
        LifecycleBudgetService.reserve_in(tx, request_id=reservation,
            lifecycle_id=request.lifecycle_id, amounts={'repair_attempts': 1, 'child_jobs': 1},
            input_ref=request.input_manifest_ref,
            metadata={'action_ref': request.action_ref, 'input_hash': manifest['body']['hash']})
        count_key = canonical_hash({'run': record.key.run_ref, 'scope': record.key.scope_ref,
                                    'node': record.key.node_id})
        counter = tx.get('repair_attempt_counters', count_key)
        attempt = counter['body']['attempt'] + 1 if counter else 1
        order_ref = 'repair:' + digest
        order = {'schema_version': 1, 'order_ref': order_ref, 'request_id': request.request_id,
            'lifecycle_id': request.lifecycle_id, 'source_receipt': record.source_receipt,
            'run_ref': record.key.run_ref, 'work_ref': record.key.scope_ref,
            'work_revision': work_row['revision'], 'node_id': record.key.node_id,
            'plan_ref': record.plan_ref, 'graph_ref': record.graph_ref, 'contract_ref': record.contract_ref,
            'policy_ref': record.policy_ref, 'action_ref': request.action_ref, 'attempt': attempt,
            'input_manifest_ref': request.input_manifest_ref, 'input_hash': manifest['body']['hash'],
            'strategy_input_hash': stable_hash,
            'input_refs': dict(inputs.input_refs), 'result_refs': dict(source['result_refs']),
            'authorization_ref': request.authorization_ref, 'authorization_revision': authorization['revision'],
            'principal': principal, 'budget_reservation_ref': reservation,
            'disposition_ref': disposition['body']['receipt_ref'],
            'dispatch_generation': gate['body']['generation'], 'deadline': record.deadline}
        tx.put('repair_attempt_counters', count_key, {'attempt': attempt})
        tx.put('repair_orders', order_ref, order, immutable=True)
        job_id = 'job:' + digest
        TaskBackend.enqueue_in(tx, JobSpec(job_id=job_id, job_kind='remedy',
            scope_ref=record.key.scope_ref, thread_id='remedy:' + digest, activation_id='activation:' + digest,
            dedupe_key='repair:' + request.request_id, due_at=tx.now, deadline=record.deadline,
            run_id=record.key.run_ref, dispatch_generation=gate['body']['generation'],
            payload={'repair_order_ref': order_ref, 'lifecycle_id': request.lifecycle_id}))
        event = LifecycleEvent(schema_version=1, event_id='repair-request:' + digest,
            kind='known_action', source='remediation_service', expected_revision=record.revision,
            evidence_refs=(request.source_receipt,), observed_at=tx.now)
        guards = TransitionGuards(schema_version=1, work_lease_valid=True, attempt_closed=True,
            disposition_closed=True, effect_known=True, effects_in_flight=False,
            authority_valid=True, action_registered=True, budget_available=True,
            max_same_failure=policy.max_same_failure, same_failure_count=failures)
        updated = transition_lifecycle(record, event, guards).model_copy(update={
            'attempt_refs': record.attempt_refs + (order_ref,), 'decision_ref': request.proposal_ref})
        LifecycleService.persist_transition_in(tx, record, event, updated)
        result = {'status': 'ordered', 'order_ref': order_ref, 'job_ref': job_id, 'attempt': attempt}
        tx.put('repair_requests', request.request_id, {'hash': digest, 'result': result}, immutable=True)
        return result

    def attempts(self, work_ref, node_id):
        with self.repository.transaction() as tx:
            row = tx.get('node_attempts', f'{work_ref}:{node_id}')
            return int(row['body']['attempts']) if row else 0

    def _record(self, key, body):
        with self.repository.transaction() as tx:
            tx.put('agent_remedies', key, body, immutable=True)
        return body

    def order(self, *, owner_agent_id, work_ref, node_id, run_ref, plan_ref, graph_ref,
              decision, input_refs=None, deadline):
        """据 agent 的（已校验）提案，发起有界修复；否则显式阻塞。"""
        action = REGISTERED_NODE_ACTIONS.get(node_id)
        key = f'{owner_agent_id}:{work_ref}:{node_id}'
        if action is None:
            return self._record(key + ':no_action', {'status': 'blocked', 'reason': 'no_registered_action_for_node',
                                                     'owner_agent_id': owner_agent_id, 'node_id': node_id})
        if not decision or decision.get('status') != 'proposed':
            return self._record(key + ':no_model', {'status': 'blocked', 'reason': 'model_unavailable',
                                                    'owner_agent_id': owner_agent_id, 'node_id': node_id})
        proposal = decision.get('proposal') or {}
        suggested = [a for a in (proposal.get('suggested_actions') or []) if a == action]
        if not suggested:
            return self._record(key + ':not_suggested', {'status': 'blocked', 'reason': 'agent_did_not_request_registered_action',
                                                         'owner_agent_id': owner_agent_id, 'node_id': node_id})
        # Legacy model proposals carry no failure identity, owner fence, frozen
        # authorization or transactional reservation. Keep the read API/history,
        # but never mint new side effects from this deprecated input contract.
        return self._record(key + ':requires_lifecycle', {
            'status': 'blocked', 'reason': 'lifecycle_contract_required',
            'owner_agent_id': owner_agent_id, 'node_id': node_id,
            'recovery_entry': 'request_remediation'})
