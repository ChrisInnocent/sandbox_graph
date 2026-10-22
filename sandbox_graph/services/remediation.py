"""有界失败循环：归属 agent 修复**自己**工作节点（WF-04 循环工程）。

约束（与"可复核"红线一致）：
- agent 只能**发起**该节点已注册的修复动作；未注册/越界一律拒绝；
- 修复 = **新 attempt + 新 activation** 重执行该节点；**旧 receipt/证据不可变**，
  绝不在原地改测量结果或证据；
- 每个 `(work_ref, node_id)` 有 `max_attempts` 上界；耗尽后显式终态阻塞（不无限循环）。
"""
from uuid import uuid4

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
    def __init__(self, repository, *, max_attempts=2):
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError('max_attempts must be a positive integer')
        self.repository = repository
        self.max_attempts = max_attempts

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
        attempt = self.attempts(work_ref, node_id) + 1
        if attempt > self.max_attempts:
            return self._record(key + ':exhausted', {'status': 'blocked', 'reason': 'remediation_exhausted',
                                                     'owner_agent_id': owner_agent_id, 'node_id': node_id,
                                                     'attempts': self.max_attempts})
        order = {'owner_agent_id': owner_agent_id, 'work_ref': work_ref, 'node_id': node_id,
                 'action_ref': action, 'attempt': attempt, 'run_ref': run_ref, 'plan_ref': plan_ref,
                 'graph_ref': graph_ref, 'input_refs': dict(input_refs or {})}
        identity = canonical_hash(order)
        with self.repository.transaction() as tx:
            tx.put('node_attempts', f'{work_ref}:{node_id}', {'attempts': attempt})
            tx.put('remedy_orders', identity, order, immutable=True)
            TaskBackend.enqueue_in(tx, JobSpec(
                job_id=str(uuid4()), job_kind='remedy', scope_ref=work_ref,
                thread_id=f'remedy:{identity}', activation_id=str(uuid4()),
                dedupe_key=f'remedy:{identity}', due_at=tx.now, deadline=deadline,
                run_id=run_ref, dispatch_generation=1,
                payload={'remedy_ref': identity, 'owner_agent_id': owner_agent_id,
                         'work_ref': work_ref, 'node_id': node_id, 'attempt': attempt}))
        return self._record(key + f':attempt{attempt}',
                            {'status': 'ordered', 'reason': 'registered_remediation', 'owner_agent_id': owner_agent_id,
                             'node_id': node_id, 'action_ref': action, 'attempt': attempt, 'remedy_ref': identity})
