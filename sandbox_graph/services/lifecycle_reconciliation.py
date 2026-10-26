"""原操作安全续办：独立授权、持久调用意图、原双层预算与追加关闭事实。"""
from __future__ import annotations
from ..runtime.tasks import business_job_kind

from copy import deepcopy
from pydantic import Field, ValidationError

from ..catalog.repair_actions import standard_repair_catalog
from ..domain.agent_lifecycle import LifecycleModel, LifecycleEvent, RepairPolicy, TransitionGuards, transition_lifecycle
from ..domain.models import NodeResult, canonical_hash
from ..persistence.repository import Conflict
from ..runtime.node_agent import NodeReview
from ..runtime.tasks import JobSpec, TaskBackend
from .agent_lifecycle import LifecycleService, decode
from .lifecycle_budgets import LifecycleBudgetService, SAFETY_OPERATIONS
from .lifecycle_safety import LifecycleSafetyService
from .repair_outcomes import RepairOutcomeService


class ReconciliationConfig(LifecycleModel):
    cost_unit: str = Field(min_length=1)
    query_cost: int = Field(strict=True, gt=0)
    stop_cost: int = Field(strict=True, gt=0)
    cleanup_cost: int = Field(strict=True, gt=0)
    backoff_seconds: float = Field(gt=0, allow_inf_nan=False)


class LifecycleReconciliationService:
    KINDS = {'repair_reconciliation_required', 'provider_reconciliation_required', 'lifecycle_safety_continue'}

    def __init__(self, repository, *, config: ReconciliationConfig, authorizations, repair_catalog=None):
        if not isinstance(config,ReconciliationConfig):
            raise ValueError('明确 ReconciliationConfig 必需')
        self.repository, self.config = repository, ReconciliationConfig.model_validate_json(config.model_dump_json())
        self.authorizations = dict(authorizations)
        self.repair_catalog = repair_catalog or standard_repair_catalog()

    @staticmethod
    def target_in(tx, operation_kind, target_ref):
        if operation_kind == 'provider':
            from .agent_team import AgentTeamService
            task, contract, record, remote = AgentTeamService.safety_target_in(tx,target_ref)
            identity = {'operation_kind':'provider', 'target_ref':target_ref,
                'lifecycle_id':contract.lifecycle_id,'lineage_budget_ref':contract.lineage_budget_ref,
                'work_ref':contract.scope_ref,'run_ref':contract.run_ref,'source_receipt':contract.source_receipt,
                'plan_ref':contract.plan_ref,'graph_ref':contract.graph_ref,'contract_ref':contract.contract_ref,
                'work_revision':tx.get('work_instances',contract.scope_ref)['revision'],
                'contract_hash':canonical_hash(task['contract']), 'envelope_hash':canonical_hash(task['envelope']),
                'operation_refs':[target_ref]}
            return identity, record
        if operation_kind != 'site':
            raise ValueError('明确 site/provider 分域必需')
        row = tx.get('repair_orders',target_ref)
        outcome = tx.get('repair_outcomes',target_ref)
        execution = tx.get('repair_executions',target_ref)
        isolation = tx.get('repair_isolations','repair-isolation:' + target_ref)
        if not row or not outcome or not execution or not isolation:
            raise Conflict('original_order_outcome_execution_isolation_missing')
        order = row['body']
        record = LifecycleService.load_in(tx,order['lifecycle_id'])
        request = tx.get('repair_requests',order['request_id'])
        source = tx.get('lifecycle_sources',order['lifecycle_id'])
        original_job = tx.get('jobs',execution['body']['job_ref'])
        operations = outcome['body']['outcome']['operation_refs']
        if (not request or not source or not original_job
                or request['body']['result']['order_ref'] != target_ref
                or request['body']['result']['job_ref'] != original_job['key']
                or original_job['body']['payload'].get('repair_order_ref') != target_ref
                or original_job['body']['payload'].get('lifecycle_id') != order['lifecycle_id']
                or original_job['body']['scope_ref'] != order['work_ref']
                or original_job['body']['run_id'] != order['run_ref']
                or execution['body']['order_ref'] != target_ref
                or order['order_ref'] != target_ref or target_ref not in record.attempt_refs
                or outcome['body']['postcheck_status'] != 'unknown'
                or isolation['body']['order_ref'] != target_ref
                or isolation['body']['work_ref'] != order['work_ref']
                or isolation['body']['operation_refs'] != operations):
            raise Conflict('original_order_inventory_binding_changed')
        identity = {key:order[key] for key in ('lifecycle_id','work_ref','run_ref','source_receipt',
            'plan_ref','graph_ref','contract_ref','work_revision')}
        identity.update(operation_kind='site',target_ref=target_ref,operation_refs=operations,
            lineage_budget_ref=record.key.lineage_budget_ref,order_hash=canonical_hash(order),
            outcome_hash=canonical_hash(outcome['body']),isolation_hash=canonical_hash(isolation['body']),
            source_hash=canonical_hash(source['body']))
        raw_hashes={}
        for ref in (order['source_receipt'],*outcome['body']['outcome']['raw_refs']):
            raw=tx.get('receipts',ref) or tx.get('resource_disposition_receipts',ref)
            if not raw:
                raise Conflict('frozen_original_raw_evidence_missing')
            raw_hashes[ref]=canonical_hash(raw['body'])
        identity['raw_hashes']=raw_hashes
        if (record.key.scope_ref,record.key.run_ref,record.source_receipt,record.plan_ref,
                record.graph_ref,record.contract_ref) != tuple(identity[k] for k in (
                'work_ref','run_ref','source_receipt','plan_ref','graph_ref','contract_ref')):
            raise Conflict('original_lifecycle_identity_changed')
        return identity, record

    def authorize(self, authorization_ref, *, operation_kind, target_ref, principal, expires_at, permissions):
        """受信部署/操作员入口；不从旧 repair 权限推导安全权限或增加预算。"""
        if not principal or not permissions or not set(permissions) <= set(SAFETY_OPERATIONS):
            raise ValueError('明确安全 principal 与 query/stop/cleanup 权限必需')
        if operation_kind=='provider' and 'cleanup' in permissions:
            raise ValueError('provider 安全授权只允许 query/stop，不授予现场 cleanup')
        with self.repository.transaction() as tx:
            identity, record = self.target_in(tx,operation_kind,target_ref)
            snapshot = LifecycleBudgetService.snapshot_in(tx,record.key.lifecycle_id)
            deadline = min(bound['safety_deadline'] for bound in snapshot.values())
            if not tx.now < expires_at <= deadline:
                raise ValueError('安全授权必须在原绝对 safety deadline 内')
            body = {'identity':identity,'principal':principal,'expires_at':expires_at,
                    'permissions':sorted(permissions),'revoked':False,
                    'config':self.config.model_dump(mode='json'),
                    'limits':{key:bound['safety_limits'] for key,bound in snapshot.items()}}
            tx.put('reconciliation_authorizations',authorization_ref,body,immutable=True)
        return authorization_ref

    def assert_target_in(self, tx, session):
        identity, record = self.target_in(tx,session['identity']['operation_kind'],session['identity']['target_ref'])
        if identity != session['identity']:
            raise Conflict('original_target_identity_changed')
        auth = tx.get('reconciliation_authorizations',session['authorization_ref'])
        body = auth['body'] if auth else {}
        if (body.get('identity') != identity or body.get('revoked') is not False
                or body.get('expires_at',0) <= tx.now
                or body.get('config') != self.config.model_dump(mode='json')):
            raise PermissionError('independent_original_ID_safety_authorization_required')
        current = LifecycleSafetyService._identity_in(tx,identity['work_ref'])
        if any(current[k] != identity[v] for k,v in (
                ('scope','work_ref'),('run_ref','run_ref'),('plan_ref','plan_ref'),
                ('graph_ref','graph_ref'),('contract_ref','contract_ref'),('work_revision','work_revision'))):
            raise Conflict('frozen_safety_work_revision_or_versions_changed')
        if identity['operation_kind'] == 'site':
            operations = LifecycleSafetyService._rows_in(tx,'operations',identity['work_ref'])
            if sorted(r['key'] for r in operations) != sorted(identity['operation_refs']):
                raise Conflict('original_operation_inventory_changed')
            if any(r['body'].get('run_id') != identity['run_ref'] for r in operations):
                raise Conflict('original_operation_scope_or_run_changed')
        return record, body

    def assert_owner_in(self, tx, job, token):
        TaskBackend.assert_claim_in(tx,job)
        if token.thread_id != job['thread_id'] or business_job_kind(job['job_kind']) != 'reconcile':
            raise PermissionError('实际安全 job/thread 必需')
        row = tx.get('lifecycle_reconciliations',job['payload']['reconciliation_ref'])
        if not row or row['body'].get('job_ref') != job['job_id']:
            raise Conflict('safety_session_job_binding_changed')
        session = row['body']
        identity = session['identity']
        if (job['scope_ref'],job['run_id'],job['payload'].get('lifecycle_id')) != (
                identity['work_ref'],identity['run_ref'],identity['lifecycle_id']):
            raise Conflict('safety_job_scope_or_lifecycle_changed')
        LifecycleService.assert_owner_in(tx,identity['lifecycle_id'],token)
        LifecycleSafetyService.assert_work_in(tx,identity['work_ref'],token)
        self.assert_target_in(tx,session)
        return session

    def block_in(self, tx, session, reason, *, status='blocked'):
        identity = session['identity']
        blocker = {'status':status,'reason':reason,'lifecycle_id':identity['lifecycle_id'],
            'operation_kind':identity['operation_kind'],'operation_refs':identity['operation_refs'],
            'target_ref':identity['target_ref'],'intent_ref':session['intent_ref'],
            'authorization_ref':session['authorization_ref'],
            'recovery_entry':'LifecycleReconciliationService.resume; 原身份/独立授权/原双预算；未知cleanup需可信原清理查询合同'}
        tx.put('lifecycle_blockers',identity['lifecycle_id'],blocker)
        tx.put('outbox_errors',session['intent_ref'],blocker)
        return {'execution_status':'blocked','output':blocker}

    def consume_in(self, tx, intent_ref, body):
        """单项目标验证失败留在原 outbox，不中断共享 pump 或接管活跃原 owner。"""
        from .agent_team import ProviderHandoffPending
        try:
            return self._consume_in(tx,intent_ref,body)
        except (ValueError,PermissionError,KeyError) as exc:
            tx.put('outbox_errors',intent_ref,{
                'status':'waiting_dependency' if isinstance(exc,ProviderHandoffPending) else 'blocked',
                'reason':'reconciliation_target_schema_invalid' if isinstance(exc,ValidationError) else str(exc),
                'kind':body.get('kind'),
                'operation_refs':body.get('operation_refs',()),
                'recovery_entry':'原 owner 完成后重放原 outbox；独立安全授权与原身份仍须核验'})
            return False

    def _consume_in(self, tx, intent_ref, body):
        if body.get('kind') not in self.KINDS:
            return None
        if body['kind'] == 'lifecycle_safety_continue':
            ref = body['reconciliation_ref']
            row=tx.get('lifecycle_reconciliations',ref)
            if not row:
                raise Conflict('original_reconciliation_session_missing')
            session=row['body']
            if body.get('lifecycle_id')!=session['identity']['lifecycle_id']:
                raise Conflict('original_reconciliation_lifecycle_changed')
            if session['state'] != 'waiting' or session['round'] != body['round']:
                return True
        else:
            operation_kind = 'site' if body['kind']=='repair_reconciliation_required' else 'provider'
            target = body.get('order_ref') if operation_kind=='site' else body.get('task_ref')
            identity, _ = self.target_in(tx,operation_kind,target)
            if (body.get('lifecycle_id') != identity['lifecycle_id']
                    or body.get('operation_refs') != identity['operation_refs']):
                raise Conflict('outbox_original_target_binding_changed')
            ref = 'safety:' + canonical_hash({'operation_kind':operation_kind,'target_ref':target})
            old = tx.get('lifecycle_reconciliations',ref)
            if old:
                if old['body']['identity']!=identity:
                    raise Conflict('original_reconciliation_identity_changed')
                return bool(old['body'].get('job_ref')) or old['body']['state']=='closed'
            auth_ref = self.authorizations.get(target,self.authorizations.get(identity['lifecycle_id']))
            session = {'identity':identity,'authorization_ref':auth_ref,'intent_ref':intent_ref,
                'state':'waiting','round':0,'next_due_at':tx.now,'ref':ref}
        try:
            record,auth=self.assert_target_in(tx,session)
            if not session['identity']['operation_refs']:
                raise Conflict('operation_binding_missing; 需原操作可信归属证据')
            snapshot = LifecycleBudgetService.snapshot_in(tx,session['identity']['lifecycle_id'])
            policy=decode(RepairPolicy,tx.get('repair_policies',record.policy_ref)['body'])
            deadline = min(auth['expires_at'],policy.safety_budget.deadline,
                           *(b['safety_deadline'] for b in snapshot.values()))
            if session['next_due_at'] >= deadline or tx.now >= deadline:
                raise Conflict('safety_deadline_exhausted')
            job_ref = ref + ':' + str(session['round'])
            TaskBackend.enqueue_in(tx,JobSpec(job_id=job_ref,job_kind='reconcile',
                scope_ref=session['identity']['work_ref'],run_id=session['identity']['run_ref'],
                thread_id=ref,activation_id=job_ref,dedupe_key=job_ref,
                due_at=session['next_due_at'],deadline=deadline,max_attempts=2,
                payload={'reconciliation_ref':ref,'lifecycle_id':session['identity']['lifecycle_id']}))
            session = session | {'job_ref':job_ref,'state':'queued'}
            tx.put('lifecycle_reconciliations',ref,session)
            return True
        except (Conflict,PermissionError) as exc:
            tx.put('lifecycle_reconciliations',ref,session | {'state':'blocked'})
            self.block_in(tx,session,str(exc))
            return False

    def resume(self, reconciliation_ref, *, authorization_ref=None):
        """显式重读原会话；不增加预算、重置轮次或重发未知清理。"""
        with self.repository.transaction() as tx:
            row = tx.get('lifecycle_reconciliations',reconciliation_ref)
            session = row['body']
            if session['state'] == 'closed':
                return session
            if session['state'] != 'blocked':
                raise Conflict('safety_session_not_blocked')
            if authorization_ref is not None:
                session=session | {'authorization_ref':authorization_ref}
            self.assert_target_in(tx,session)
            session = session | {'state':'waiting','round':session['round']+1,'next_due_at':tx.now}
            tx.put('reconciliation_resumptions',reconciliation_ref + ':' + str(session['round']),{
                'session_ref':reconciliation_ref,'previous_authorization_ref':row['body']['authorization_ref'],
                'authorization_ref':session['authorization_ref'],'round':session['round'],
                'operation_refs':session['identity']['operation_refs'],'resumed_at':tx.now},immutable=True)
            tx.put('lifecycle_reconciliations',reconciliation_ref,session)
            self.continue_in(tx,session)
            return session

    @staticmethod
    def continue_in(tx, session):
        tx.put('outbox',session['ref'] + ':continue:' + str(session['round']),{
            'kind':'lifecycle_safety_continue','lifecycle_id':session['identity']['lifecycle_id'],
            'reconciliation_ref':session['ref'],'round':session['round']},immutable=True)

    @staticmethod
    def resolve_blocker_in(tx,session):
        identity=session['identity']
        row=tx.get('lifecycle_blockers',identity['lifecycle_id'])
        body=row['body'] if row else {}
        if row and (body.get('target_ref')==identity['target_ref']
                or body.get('intent_ref')==session['intent_ref']
                or identity['operation_kind']=='provider' and body.get('task_ref')==identity['target_ref']):
            tx.put('reconciliation_resolved_blockers',session['ref'] + ':' + str(row['revision']),{
                'session_ref':session['ref'],'blocker':row,'closed_at':tx.now},immutable=True)
            tx.delete('lifecycle_blockers',identity['lifecycle_id'])
        error=tx.get('outbox_errors',session['intent_ref'])
        if error:
            tx.put('reconciliation_resolved_errors',session['ref'] + ':' + str(error['revision']),{
                'session_ref':session['ref'],'error':error,'closed_at':tx.now},immutable=True)
            tx.delete('outbox_errors',session['intent_ref'])

    @staticmethod
    def closed_result_in(tx,job):
        """只读已提交关闭；兼容旧关闭事务未保存外层 job result 的崩溃窗口。"""
        TaskBackend.assert_claim_in(tx,job)
        row=tx.get('lifecycle_reconciliations',job['payload']['reconciliation_ref'])
        if not row or row['body'].get('state')!='closed':
            return None
        session=row['body']; identity=session['identity']
        if (session.get('job_ref')!=job['job_id'] or business_job_kind(job['job_kind'])!='reconcile'
                or (job['scope_ref'],job['run_id'],job['payload'].get('lifecycle_id'))!=(
                    identity['work_ref'],identity['run_ref'],identity['lifecycle_id'])):
            raise Conflict('closed_reconciliation_job_binding_changed')
        if identity['operation_kind']=='site':
            isolation=tx.get('repair_isolations','repair-isolation:' + identity['target_ref'])
            outcome=tx.get('repair_reconciliation_outcomes',identity['target_ref'])
            if not isolation or not outcome or not LifecycleSafetyService.isolation_resolved_in(tx,isolation):
                raise Conflict('closed_reconciliation_resolution_invalid')
            result=outcome['body']
        else:
            from .agent_team import AgentTeamService
            task,contract,record,remote=AgentTeamService.safety_target_in(tx,identity['target_ref'])
            fact=tx.get('agent_team_reconciliations',identity['target_ref'])
            result=fact['body'] if fact else {}
            if (result.get('session_ref')!=session['ref'] or result.get('job_ref')!=job['job_id']
                    or result.get('task_ref')!=identity['target_ref']
                    or result.get('lifecycle_id')!=identity['lifecycle_id']
                    or result.get('lineage_budget_ref')!=identity['lineage_budget_ref']
                    or result.get('provider_id')!=contract.provider_id
                    or canonical_hash(task['contract'])!=identity['contract_hash']
                    or canonical_hash(task['envelope'])!=identity['envelope_hash']
                    or result.get('children_stopped') is not True or not remote.terminal
                    or result.get('receipt')!=remote.receipt.model_dump(mode='json')):
                raise Conflict('closed_provider_reconciliation_fact_invalid')
        return {'execution_status':'succeeded','output':result}

    def prepare_call_in(self, tx, job, token, kind, target):
        session = self.assert_owner_in(tx,job,token)
        _, auth = self.assert_target_in(tx,session)
        if kind not in auth['permissions']:
            raise PermissionError('independent_safety_permission_missing:' + kind)
        identity=session['identity']
        if (kind in {'query','stop'} and target not in identity['operation_refs']) or (
                identity['operation_kind']=='provider' and kind=='cleanup'):
            raise PermissionError('安全目标必须是原分域 operation ID')
        if kind=='cleanup':
            if not isinstance(target,list) or not target or len(set(target))!=len(target):
                raise Conflict('明确原资源全集必需')
            for ref in target:
                resource=tx.get('resources',ref)
                if (not resource or resource['body'].get('scope')!=identity['work_ref']
                        or resource['body'].get('operation_id') not in identity['operation_refs']):
                    raise PermissionError('原资源/operation 精确归属必需')
        # stop/cleanup are one-shot per original target, independent of job/owner.
        ordinal = session['round'] if kind=='query' else 0
        ref = session['ref'] + ':call:' + canonical_hash({'kind':kind,
            'target':'original_resource_set' if kind=='cleanup' else target,'ordinal':ordinal})
        old = tx.get('reconciliation_calls',ref)
        if old:
            if old['body']['target']!=target:
                raise Conflict('cleanup_original_resource_inventory_changed; 不重发未知清理')
            if not tx.get('budget_settlements',ref):
                LifecycleBudgetService.settle_in(tx,request_id=ref)
            return ref, False
        self.assert_drain_action_in(tx,session,kind,target)
        amounts = {SAFETY_OPERATIONS[kind]:1,'cost':getattr(self.config,kind + '_cost')}
        identity = session['identity']
        snapshot = LifecycleBudgetService.snapshot_in(tx,identity['lifecycle_id'])
        policy = decode(RepairPolicy,tx.get('repair_policies',LifecycleService.load_in(tx,identity['lifecycle_id']).policy_ref)['body'])
        maxima = {'query_calls':policy.safety_budget.max_query_calls,
                  'stop_calls':policy.safety_budget.max_stop_calls,
                  'cleanup_calls':policy.safety_budget.max_cleanup_calls,'cost':policy.safety_budget.cost_limit}
        for key,bound in snapshot.items():
            if (bound['cost_unit'] != self.config.cost_unit or policy.safety_budget.cost_unit != self.config.cost_unit
                    or tx.now >= policy.safety_budget.deadline
                    or any(bound['safety_spent'][k]+bound['safety_reserved'][k]+v > min(maxima[k],auth['limits'][key][k])
                           for k,v in amounts.items())):
                raise Conflict('safety_budget_or_policy_exhausted')
        LifecycleBudgetService.reserve_in(tx,request_id=ref,lifecycle_id=identity['lifecycle_id'],
            input_ref=session['ref'],amounts=amounts,safety=True,operation_kind=kind,
            metadata={'identity':identity,'kind':kind,'target':target,'ordinal':ordinal})
        tx.put('reconciliation_calls',ref,{'session_ref':session['ref'],'kind':kind,'target':target,
            'state':'unknown','network_started':False,'job_ref':job['job_id']},immutable=True)
        return ref, True

    @staticmethod
    def assert_drain_action_in(tx, session, kind, target):
        from .lifecycle_stop import LifecycleStopService
        identity = session['identity']
        record = LifecycleService.load_in(tx,identity['lifecycle_id'])
        targets = identity['operation_refs'] if kind=='cleanup' else [target]
        if kind in {'stop','cleanup'} and any(not LifecycleStopService.stop_allowed_in(
                tx,record,operation_kind=identity['operation_kind'],target=ref) for ref in targets):
            raise Conflict('drain_waits_for_original_deadline')

    def network_in(self, tx, job, token, call_ref, *, start=False):
        session = self.assert_owner_in(tx,job,token)
        call = tx.get('reconciliation_calls',call_ref)
        reservation = tx.get('budget_reservations',call_ref)
        if (not call or call['body']['session_ref'] != session['ref'] or not reservation
                or reservation['body']['lifecycle_id'] != session['identity']['lifecycle_id']
                or reservation['body']['lineage_budget_ref'] != session['identity']['lineage_budget_ref']
                or reservation['body']['safety'] is not True
                or tx.get('budget_settlements',call_ref)):
            raise PermissionError('original_safety_call_reservation_required')
        record,auth=self.assert_target_in(tx,session)
        kind=call['body']['kind']
        # A drain can arrive after reservation. Check again at the I/O boundary;
        # never reject persistence of a response to an already-started call.
        if start or not call['body']['network_started']:
            self.assert_drain_action_in(tx,session,kind,call['body']['target'])
        amounts={SAFETY_OPERATIONS[kind]:1,'cost':getattr(self.config,kind+'_cost')}
        policy=decode(RepairPolicy,tx.get('repair_policies',record.policy_ref)['body'])
        maxima={'query_calls':policy.safety_budget.max_query_calls,'stop_calls':policy.safety_budget.max_stop_calls,
                'cleanup_calls':policy.safety_budget.max_cleanup_calls,'cost':policy.safety_budget.cost_limit}
        if (kind not in auth['permissions'] or reservation['body']['operation_kind']!=kind
                or reservation['body']['amounts']!=amounts or reservation['body']['input_ref']!=session['ref']
                or reservation['body']['metadata'].get('identity')!=session['identity']
                or reservation['body']['metadata'].get('target')!=call['body']['target']):
            raise PermissionError('original_safety_call_contract_changed')
        for key,bound in LifecycleBudgetService.snapshot_in(tx,record.key.lifecycle_id).items():
            if (bound['cost_unit']!=self.config.cost_unit or policy.safety_budget.cost_unit!=self.config.cost_unit
                    or tx.now>=min(bound['safety_deadline'],policy.safety_budget.deadline)
                    or any(bound['safety_reserved'][counter]<amount
                        or bound['safety_reserved'][counter]+bound['safety_spent'][counter]>min(maxima[counter],auth['limits'][key][counter])
                        for counter,amount in amounts.items())):
                raise PermissionError('original_safety_allowance_or_deadline_changed')
        if start:
            if call['body']['network_started']:
                raise Conflict('safety_call_already_started')
            tx.put('reconciliation_calls',call_ref,call['body'] | {'network_started':True})
        return session

    def finish_call_in(self, tx, job, token, call_ref, *, completed):
        self.assert_owner_in(tx,job,token)
        row = tx.get('reconciliation_calls',call_ref)
        tx.put('reconciliation_calls',call_ref,row['body'] | {'state':'completed' if completed else 'unknown'})
        LifecycleBudgetService.settle_in(tx,request_id=call_ref)

    @staticmethod
    def job_ended_in(tx, job):
        ref=job.get('payload',{}).get('reconciliation_ref')
        if business_job_kind(job['job_kind'])!='reconcile' or not ref:
            return
        row=tx.get('lifecycle_reconciliations',ref)
        if not row or row['body'].get('job_ref')!=job['job_id']:
            return
        session=row['body']
        for call in tx.scan('reconciliation_calls'):
            if call['body'].get('job_ref')==job['job_id'] and not tx.get('budget_settlements',call['key']):
                LifecycleBudgetService.settle_in(tx,request_id=call['key'])
        if session['state']!='queued':
            return
        tx.put('lifecycle_reconciliations',ref,session | {'state':'blocked'})
        identity=session['identity']
        blocker={'status':'blocked','reason':'safety_job_stopped_unknown; 原调用不重发，需原身份安全续办',
            'lifecycle_id':identity['lifecycle_id'],'operation_refs':identity['operation_refs'],
            'operation_kind':identity['operation_kind'],'intent_ref':session['intent_ref'],
            'recovery_entry':'LifecycleReconciliationService.resume','reconciliation_ref':ref,
            'authorization_ref':session['authorization_ref']}
        tx.put('lifecycle_blockers',identity['lifecycle_id'],blocker)
        tx.put('outbox_errors',session['intent_ref'],blocker)

    def wait_in(self, tx, job, token, reason):
        session = self.assert_owner_in(tx,job,token)
        session = session | {'state':'waiting','round':session['round']+1,
                             'next_due_at':tx.now+self.config.backoff_seconds}
        tx.put('lifecycle_reconciliations',session['ref'],session)
        self.continue_in(tx,session)
        return self.block_in(tx,session,reason,status='waiting_dependency')

    def close_site_in(self, tx, job, token, *, ttl):
        session = self.assert_owner_in(tx,job,token)
        identity = session['identity']; scope = identity['work_ref']; order_ref = identity['target_ref']
        snapshot = LifecycleSafetyService._snapshot_in(tx,scope)
        ops,resources,uncertain,owned,residual = LifecycleSafetyService._resource_state_in(tx,scope)
        observations = snapshot['observations']
        from ..runtime.observation import ObservationService
        if any(not ObservationService.closed_in(tx,r) for r in observations):
            raise Conflict('sampler_stop_adapter_receipt_missing; 账本stop不证明采样任务停止')
        if (not ops or uncertain or residual or not LifecycleSafetyService._closure_valid(snapshot)
                or any(r['body'].get('status')!='stopped' or r['body'].get('run_id',identity['run_ref']) != identity['run_ref']
                       or r['body'].get('operation_id') not in {None,*identity['operation_refs']} for r in observations)):
            raise Conflict('mandatory_closure_or_sampler_or_resources_missing')
        order = tx.get('repair_orders',order_ref)['body']
        collection,collection_refs=RepairOutcomeService.collection_in(tx,order,
            NodeResult(operation_refs=tuple(identity['operation_refs'])))
        if collection is None:
            raise Conflict('original_operation_collection_or_event_chain_missing; 需原ID可信采集证据')
        for operation in ops:
            declared=set((operation['body'].get('status') or {}).get('artifact_refs',()))
            selected=[tx.get('collections',ref)['body'] for ref in collection_refs
                      if ref.rsplit(':',1)[0]==operation['key']]
            if len(selected)!=1 or set(selected[0].get('artifact_refs',()))!=declared:
                raise Conflict('original_operation_collection_artifact_inventory_changed')
        isolation_ref = 'repair-isolation:' + order_ref
        if any(r['key'] != isolation_ref for r in snapshot['repair_isolations']):
            raise Conflict('other_unresolved_isolation_requires_own_reconciliation')
        for lease in snapshot['resource_leases']:
            if lease['body'].get('run_id') != identity['run_ref']:
                raise Conflict('resource_lease_run_ownership_missing')
        material=LifecycleSafetyService.reconciliation_closure_material_in(tx,scope)
        resolution = {'status':'closed','producer':'lifecycle_reconciliation','version':1,
            'identity':identity,'isolation_ref':isolation_ref,
            'isolation_hash':canonical_hash(tx.get('repair_isolations',isolation_ref)['body']),
            'session_ref':session['ref'],'job_ref':job['job_id'],
            'owner':LifecycleSafetyService._owner(token),'closed_at':tx.now,
            'closure_hash':canonical_hash(material),'closure_material':material,
            'closure_identity':snapshot['identity'],
            'collection_refs':collection_refs,
            'operation_refs':identity['operation_refs']}
        resolution_ref = 'repair-resolution:' + order_ref
        tx.put('repair_isolation_resolutions',resolution_ref,resolution,immutable=True)
        # The immutable resolution is verified by every isolation reader.
        for lease in snapshot['resource_leases']:
            tx.delete('resource_leases',lease['key'])
        if snapshot['resource_leases']:
            from ..runtime.leases import LeaseService
            LeaseService.announce_release(tx,[r['key'] for r in snapshot['resource_leases']],scope,token.owner_epoch)
        proof = LifecycleSafetyService.produce_disposition_in(tx,scope,token,ttl=ttl)
        disposition = LifecycleSafetyService.verify_disposition_in(tx,proof,scope,token)
        order = tx.get('repair_orders',order_ref)['body']
        original = tx.get('repair_outcomes',order_ref)['body']
        execution = tx.get('repair_executions',order_ref)['body']
        old_job = tx.get('jobs',execution['job_ref'])['body']
        receipt = RepairOutcomeService.receipt_in(tx,order,old_job)
        result = NodeResult.model_validate(receipt[1]['result']) if receipt else None
        review_row = tx.get('node_reviews',receipt[0]) if receipt else None
        review = NodeReview.model_validate(review_row['body']) if review_row else None
        action_id,_,version = order['action_ref'].rpartition('@')
        action = self.repair_catalog.resolve(action_id,version,order['node_id']).require_available()
        statuses = RepairOutcomeService.operation_statuses_in(tx,order,result.operation_refs) if result else None
        collection, _ = RepairOutcomeService.collection_in(tx,order,result) if result and order['node_id']=='N07' else (None,[])
        accepted = bool(result and review and review.owner_agent_id==f'agent:work:{scope}'
            and action.postcheck(result,review,disposition=disposition,checked_at=tx.now,
                                 operation_statuses=statuses,collection=collection))
        if order['node_id']=='N01':
            accepted = accepted and set(result.operation_refs) <= set(execution['operation_refs'])
        record = LifecycleService.load_in(tx,identity['lifecycle_id'])
        evidence_ref = 'repair-reconciled:' + order_ref
        LifecycleService.bind_evidence_in(tx,record,evidence_ref,kind='repair_reconciliation')
        guards = TransitionGuards(schema_version=1,work_lease_valid=True,attempt_closed=True,
            disposition_closed=True,effect_known=True,effects_in_flight=False,postcheck_verified=True)
        def advance(kind):
            nonlocal record
            event = LifecycleEvent(schema_version=1,event_id=evidence_ref+':'+kind,kind=kind,
                source='lifecycle_reconciliation',expected_revision=record.revision,
                evidence_refs=(evidence_ref,proof,resolution_ref),observed_at=tx.now)
            updated = transition_lifecycle(record,event,guards)
            LifecycleService.persist_transition_in(tx,record,event,updated); record=updated
        if record.phase=='reconciling':
            advance('effect_known')
        from .lifecycle_stop import LifecycleStopService
        stopped = LifecycleStopService.policy_in(tx,record)
        business_live = tx.now < record.deadline and record.terminal_intent is None
        if stopped == 'drain':
            # Keep the original outcome/resolution as evidence. The stop service
            # closes the old lifecycle only after all jobs and effects close and
            # it verifies the version successor; no old-version recovery/retry.
            next_kind='repair_blocked'
        elif not business_live or stopped:
            if record.terminal_intent is None:
                advance('cancel' if stopped=='cancel' else 'deadline')
            if record.phase=='cancelling':
                advance('disposition_closed')
            next_kind='repair_blocked'
        elif accepted:
            advance('postcheck_pass'); next_kind='repair_recovery_required'
        else:
            policy=decode(RepairPolicy,tx.get('repair_policies',record.policy_ref)['body'])
            tx.put('repair_failure_fingerprints',order_ref,{
                'lineage_budget_ref':record.key.lineage_budget_ref,'action_ref':order['action_ref'],
                'input_hash':order['input_hash'],'failure_fingerprint':record.issue_fingerprint,
                'receipt_ref':receipt[0] if receipt else None},immutable=True)
            failures=RepairOutcomeService.failure_count_in(tx,order,record)
            available=RepairOutcomeService.next_allowance_in(tx,record,policy,failures=failures)
            guards=guards.model_copy(update={'budget_available':available,'same_failure_count':failures,
                                            'max_same_failure':policy.max_same_failure})
            advance('postcheck_fail_with_budget' if available else 'postcheck_fail_exhausted')
            next_kind='repair_retry_required' if available else 'repair_blocked'
        settled={'order_ref':order_ref,'original_outcome_ref':order_ref,'original_outcome_hash':canonical_hash(original),
            'effect_certainty':'confirmed','postcheck_status':'accepted' if accepted else 'rejected',
            'resolution_ref':resolution_ref,'postcheck_disposition_ref':proof,'operation_refs':identity['operation_refs'],
            'raw_refs':original['outcome']['raw_refs'],'phase':record.phase}
        tx.put('repair_reconciliation_outcomes',order_ref,settled,immutable=True)
        tx.put('outbox',evidence_ref,{'kind':next_kind,'lifecycle_id':identity['lifecycle_id'],
            'order_ref':order_ref,'outcome_ref':order_ref,'settled_outcome_ref':order_ref,
            'phase':record.phase,'reason':None if accepted else 'reconciled_postcheck_rejected'},immutable=True)
        tx.put('lifecycle_reconciliations',session['ref'],session | {'state':'closed','resolution_ref':resolution_ref})
        self.resolve_blocker_in(tx,session)
        result={'execution_status':'succeeded','output':settled}
        tx.put('reconciliation_job_results',job['job_id'],result,immutable=True)
        return result

    @staticmethod
    def accepted_outcome_in(tx, order, original, *, token, catalog=None):
        """恢复编译窄读取：验证追加事实并重跑原动作后验，绝不改写 unknown 原件。"""
        ref=order['order_ref']
        settled_row=tx.get('repair_reconciliation_outcomes',ref)
        isolation=tx.get('repair_isolations','repair-isolation:' + ref)
        settled=settled_row['body'] if settled_row else {}
        if (original.get('postcheck_status')!='unknown' or settled.get('postcheck_status')!='accepted'
                or settled.get('original_outcome_hash')!=canonical_hash(original)
                or not isolation or not LifecycleSafetyService.isolation_resolved_in(tx,isolation)):
            raise Conflict('trusted_accepted_reconciliation_resolution_required')
        resolution=tx.get('repair_isolation_resolutions',settled['resolution_ref'])
        identity=resolution['body']['identity']
        for raw_ref,digest in identity['raw_hashes'].items():
            raw=tx.get('receipts',raw_ref) or tx.get('resource_disposition_receipts',raw_ref)
            if not raw or canonical_hash(raw['body'])!=digest:
                raise Conflict('accepted_original_raw_evidence_changed')
        proof_row=tx.get('resource_disposition_receipts',settled['postcheck_disposition_ref'])
        if not proof_row or proof_row['body']['receipt']['expires_at']<=tx.now:
            raise Conflict('accepted_reconciliation_disposition_expired')
        ttl=proof_row['body']['receipt']['expires_at']-tx.now
        proof=LifecycleSafetyService.produce_disposition_in(tx,order['work_ref'],token,ttl=ttl)
        disposition=LifecycleSafetyService.verify_disposition_in(tx,proof,order['work_ref'],token)
        execution=tx.get('repair_executions',ref)['body']
        receipt=RepairOutcomeService.receipt_in(tx,order,tx.get('jobs',execution['job_ref'])['body'])
        result=NodeResult.model_validate(receipt[1]['result']) if receipt else None
        review_row=tx.get('node_reviews',receipt[0]) if receipt else None
        review=NodeReview.model_validate(review_row['body']) if review_row else None
        action_id,_,version=order['action_ref'].rpartition('@')
        action=(catalog or standard_repair_catalog()).resolve(action_id,version,order['node_id']).require_available()
        statuses=RepairOutcomeService.operation_statuses_in(tx,order,result.operation_refs) if result else None
        collection,_=RepairOutcomeService.collection_in(tx,order,result) if result and order['node_id']=='N07' else (None,[])
        accepted=bool(result and review and review.owner_agent_id==f'agent:work:{order["work_ref"]}'
            and action.postcheck(result,review,disposition=disposition,checked_at=tx.now,
                                 operation_statuses=statuses,collection=collection))
        if order['node_id']=='N01':
            accepted=accepted and set(result.operation_refs)<=set(execution['operation_refs'])
        if not accepted:
            raise Conflict('accepted_reconciliation_action_postcheck_changed')
        effective=deepcopy(original)
        effective.update(postcheck_status='accepted',interrupted=False,
                         reconciliation_disposition_ref=settled['postcheck_disposition_ref'])
        effective['outcome']['effect_certainty']='confirmed'
        return effective
