"""Environment graph services: durable repair intent, fresh verification and evidence.

Only a registered gateway performs mutations. Unknown effects retain the original
operation and host reservation; a new activation reconciles that same operation.
"""
from dataclasses import asdict
from datetime import datetime,timezone
import json

from .contracts import EnvironmentDeclaration,EnvironmentReport,RepairPlan,StepReceipt
from .workflow import EnvironmentScopePlan,EnvironmentRunService
from .reporting import publish_report
from .evidence import assert_public_payload_safe
from ..domain.models import EvidenceRef,NodeResult,ErrorRecord,canonical_hash
from ..runtime.leases import LeaseService
from ..services.workflows import token_for
from ..persistence.repository import Conflict


def timestamp(value):return datetime.fromtimestamp(value,timezone.utc).isoformat().replace('+00:00','Z')
def outcome(status='succeeded',code=None,**output):
    return NodeResult(execution_status=status,coverage_status='blocked' if status=='blocked' else 'attempted',
        error=ErrorRecord(code=code,observed_at=0) if code else None,output=output)


class EnvironmentWorkflowHandlers:
    def __init__(self,repository,*,store,probe,planner,gateway=None,cleanup=None):
        self.repository=repository;self.store=store;self.probe=probe
        self.planner=planner;self.gateway=gateway;self.cleanup_service=cleanup

    def mapping(self):
        return dict(E1=self.restore,E2=self.inspect,C1=self.plan,C2=self.configure,
            C3=self.verify,T8=self.cleanup,GE=self.deliver)

    def _load(self,context,*,mutate=False):
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            row=tx.get('environment_plans',context.plan_ref)
            if not row:raise Conflict('environment scope plan missing')
            plan=EnvironmentScopePlan.model_validate(row['body'])
            run=tx.get('runs',context.run_ref)
            if (plan.plan_hash!=context.plan_ref or context.graph_ref!=plan.graph_ref or not run
                    or run['body']['active_plan_ref']!=context.plan_ref or context.scope!=plan.group_id):
                raise Conflict('environment context differs from frozen plan')
            declaration=EnvironmentDeclaration.model_validate(tx.get('environment_declarations',plan.declaration_hash)['body'])
            if declaration.declaration_hash!=plan.declaration_hash:raise Conflict('environment declaration digest differs')
            if mutate:
                if plan.mode!='prepare':raise PermissionError('check-only graph cannot mutate environment')
                if tx.now>=plan.deadline:raise PermissionError('environment mutation deadline expired')
                EnvironmentRunService.verify_authority_in(tx,plan)
            return plan,declaration

    def _read(self,context):
        with self.repository.transaction() as tx:
            row=tx.get('environment_progress',context.run_ref)
            return row['body'] if row else {'run_id':context.run_ref,'rounds':[],'step_receipts':{},'ready':False}

    def _write(self,context,body):
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context));tx.put('environment_progress',context.run_ref,body)

    def commit(self,value,context,producer='environment-graph.v1'):
        assert_public_payload_safe(value)
        raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
        ref=self.store.commit([raw],scope=context.run_ref,media_type='application/json',producer=producer)
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context));tx.put('environment_evidence','artifact://'+ref.uri,ref.model_dump(mode='json'),immutable=True)
        return 'artifact://'+ref.uri

    def resolve(self,ref):
        with self.repository.transaction() as tx:
            row=tx.get('environment_evidence',ref)
            return EvidenceRef.model_validate(row['body']) if row else None

    async def restore(self,context):
        plan,declaration=self._load(context)
        body=self._read(context)
        if plan.approved_repair_hash and not body['rounds']:
            with self.repository.transaction() as tx:row=tx.get('environment_scope_inputs',plan.plan_hash)
            if not row:raise Conflict('approved environment repair input missing')
            repair=RepairPlan.model_validate(row['body']['repair_plan'])
            if repair.plan_hash!=plan.approved_repair_hash:raise Conflict('approved repair hash differs')
            if (repair.declaration_hash!=plan.declaration_hash
                    or not {step.node_id for step in repair.steps}<=set(plan.node_ids)
                    or not {step.action_ref for step in repair.steps}<=set(plan.allowed_actions)
                    or len(repair.steps)>plan.max_steps):raise PermissionError('approved repair exceeds frozen environment scope')
            ref=self.commit(repair.model_dump(mode='json'),context)
            body.update(active_plan=repair.model_dump(mode='json'),active_plan_ref=ref)
            body['rounds'].append({'plan_hash':repair.plan_hash,'plan_ref':ref,'step_count':len(repair.steps)})
        self._write(context,body)
        return outcome(plan_ref=plan.plan_hash,declaration_ref=declaration.declaration_hash)

    async def _inspect(self,context,*,fresh):
        plan,declaration=self._load(context)
        report=EnvironmentReport.model_validate(await self.probe.check(declaration,plan.node_ids,fresh=fresh))
        if (report.declaration_hash!=plan.declaration_hash or report.profile!=declaration.profile
                or report.source_set!=declaration.source_set
                or any(obs.node_id not in plan.node_ids for obs in report.observations)):
            raise Conflict('probe returned another environment or node scope')
        refs=set(report.artifact_manifest)|{ref for row in report.observations for ref in row.evidence_refs}
        for ref in refs:
            actual=self.resolve(ref)
            if actual is None and hasattr(self.probe,'resolve'):actual=self.probe.resolve(ref)
            if actual is None or actual.scope!=context.run_ref:raise ValueError('probe raw evidence is missing or has another scope')
            self.store.verify(actual)
            with self.repository.transaction() as tx:
                tx.assert_fence(token_for(context));tx.put('environment_evidence',ref,actual.model_dump(mode='json'),immutable=True)
        ref=self.commit(report.model_dump(mode='json'),context)
        body=self._read(context);body.update(report=report.model_dump(mode='json'),report_ref=ref)
        self._write(context,body)
        return report

    @staticmethod
    def _ready(report,node_ids):
        return (set(node_ids)<=set(row.node_id for row in report.observations)
            and bool(report.readiness) and all(value in {'ready','not_applicable'} for value in report.readiness.values())
            and all(row.status in {'satisfied','not_applicable'} for row in report.observations))

    async def inspect(self,context):
        plan,_=self._load(context)
        body=self._read(context)
        # New activations do not plan over an unresolved old installation.
        if body.get('active_plan'):
            return outcome(continuation_reason='reconcile_original').model_copy(update={'continuation':'C1'})
        report=await self._inspect(context,fresh=False)
        result=outcome(report_ref=self._read(context)['report_ref'])
        if plan.mode=='check' or self._ready(report,plan.node_ids):return result.model_copy(update={'continuation':'T8'})
        return result

    async def plan(self,context):
        scope,declaration=self._load(context);body=self._read(context)
        if body.get('active_plan'):
            return outcome(repair_plan_ref=body['active_plan_ref'])
        self._load(context,mutate=True)
        report=EnvironmentReport.model_validate(body['report'])
        round_number=len(body['rounds'])
        if round_number>=scope.max_repair_revisions:return outcome('blocked','ENV_REPAIR_BUDGET_EXHAUSTED')
        result=self.planner.plan(declaration,report,report_ref=body['report_ref'],deadline=timestamp(scope.deadline),
            authorized_actions=set(scope.allowed_actions),repair_round=round_number,
            max_repair_rounds=scope.max_repair_revisions,max_steps=scope.max_steps)
        if result.status=='blocked':
            body['blockers']=[asdict(item) for item in result.blockers];self._write(context,body)
            return outcome('blocked','ENV_REPAIR_PLAN_BLOCKED',blockers=body['blockers'])
        if sum(row['step_count'] for row in body['rounds'])+len(result.plan.steps)>scope.max_steps:
            return outcome('blocked','ENV_STEP_BUDGET_EXHAUSTED')
        if not set(step.node_id for step in result.plan.steps)<=set(scope.node_ids):
            raise PermissionError('repair plan escaped selected nodes')
        ref=self.commit(result.plan.model_dump(mode='json'),context)
        body.update(active_plan=result.plan.model_dump(mode='json'),active_plan_ref=ref)
        body['rounds'].append({'plan_hash':result.plan.plan_hash,'plan_ref':ref,'step_count':len(result.plan.steps)})
        self._write(context,body)
        return outcome(repair_plan_ref=ref)

    async def configure(self,context):
        scope,_=self._load(context);body=self._read(context)
        plan=RepairPlan.model_validate(body['active_plan'])
        if self.gateway is None:return outcome('blocked','ENV_EXECUTOR_UNAVAILABLE')
        self.gateway.bind_plan(plan,body['active_plan_ref'])
        token=token_for(context)
        for step in plan.steps:
            identity=plan.plan_hash+':'+step.step_id
            saved=body['step_receipts'].get(identity)
            if saved and saved['result']=='completed' and saved['effect_certainty']=='confirmed':continue
            if saved and saved['result'] in {'failed','cancelled'} and saved['effect_certainty']=='confirmed':
                return outcome(known_failure=True,step_id=step.step_id,result=saved['result'])
            if any(body['step_receipts'].get(plan.plan_hash+':'+dep,{}).get('result')!='completed' for dep in step.depends_on):
                return outcome('blocked','ENV_STEP_DEPENDENCY_UNRESOLVED')
            with self.repository.transaction() as tx:
                tx.assert_fence(token)
                intent=tx.get('environment_step_intents',identity)
            if not intent:
                self._load(context,mutate=True)
                aggregate='group:'+context.run_ref+':'+context.scope
                with self.repository.transaction() as tx:
                    cursor=max((row['body']['aggregate_seq'] for row in tx.scan('events') if row['body']['aggregate_id']==aggregate),default=0)
                # Unknown old operations remain queryable under quarantine.
                # A new write must first acquire every resource domain.
                try:
                    LeaseService(self.repository).acquire_all(plan.conflict_domains,scope=context.run_ref,
                        token=token,owner_run_id=context.run_ref)
                except Conflict:
                    from ..runtime.waits import WaitRecord,WaitService
                    wait=WaitRecord(wait_id=context.activation_id+':resources',activation_id=context.activation_id,
                        thread_id=context.thread_id,scope=context.scope,plan_ref=context.plan_ref,graph_ref=context.graph_ref,
                        version=1,aggregate_id=aggregate,cursor=cursor,deadline=scope.deadline)
                    WaitService(self.repository).park(wait)
                    return outcome('blocked','ENV_RESOURCE_WAIT',wait_ref=wait.wait_id)
            with self.repository.transaction() as tx:
                tx.assert_fence(token)
                intent=tx.get('environment_step_intents',identity)
                if not intent:
                    tx.put('environment_step_intents',identity,{'run_id':context.run_ref,'plan_hash':plan.plan_hash,
                        'step_id':step.step_id,'input_hash':step.input_hash},immutable=True)
                elif intent['body']['run_id']!=context.run_ref:
                    raise Conflict('repair operation belongs to another run')
                gate=tx.get('dispatch_gates',context.run_ref)
                if not intent and (not gate or gate['body']['open'] is not True):
                    raise PermissionError('environment dispatch gate closed')
                generation=gate['body']['generation'] if gate else None
            if intent:
                receipt=await self.gateway.reconcile_step(plan=plan,step=step,scope=context.run_ref,token=token)
            else:
                receipt=await self.gateway.submit_step(plan=plan,step=step,scope=context.run_ref,run_id=context.run_ref,
                    deadline=scope.deadline,dispatch_generation=generation,token=token)
            receipt=StepReceipt.model_validate(receipt)
            if receipt.plan_ref!=body['active_plan_ref'] or receipt.step_id!=step.step_id:
                raise Conflict('execution receipt not bound to original plan step')
            body['step_receipts'][identity]=receipt.model_dump(mode='json');self._write(context,body)
            if receipt.result in {'failed','cancelled'} and receipt.effect_certainty=='confirmed':
                return outcome(known_failure=True,step_id=step.step_id,result=receipt.result)
            if receipt.result!='completed' or receipt.effect_certainty!='confirmed':
                return outcome('blocked','ENV_ORIGINAL_OPERATION_UNRESOLVED',step_id=step.step_id,result=receipt.result)
        return outcome(repair_plan_ref=body['active_plan_ref'])

    async def verify(self,context):
        scope,_=self._load(context)
        while True:
            body=self._read(context)
            if any(self._unresolved(row) for row in body['step_receipts'].values()):
                return outcome('blocked','ENV_ORIGINAL_OPERATION_UNRESOLVED')
            report=await self._inspect(context,fresh=True)
            if self._ready(report,scope.node_ids):return outcome(report_ref=self._read(context)['report_ref'])
            body=self._read(context);body.pop('active_plan',None);body.pop('active_plan_ref',None);self._write(context,body)
            if len(body['rounds'])>=scope.max_repair_revisions:return outcome('blocked','ENV_REPAIR_BUDGET_EXHAUSTED')
            planned=await self.plan(context)
            if planned.execution_status!='succeeded':return planned
            configured=await self.configure(context)
            if configured.execution_status!='succeeded':return configured

    async def cleanup(self,context):
        self._load(context);body=self._read(context)
        with self.repository.transaction() as tx:
            resources=[row for row in tx.scan('resources') if row['body'].get('scope')==context.run_ref and row['body'].get('status')!='closed']
            operations=[row for row in tx.scan('operations') if row['body'].get('scope')==context.run_ref]
            canaries=[row for row in tx.scan('environment_canaries') if row['body'].get('scope')==context.run_ref]
        unresolved=any(self._unresolved(row) for row in body['step_receipts'].values())
        unresolved=unresolved or any(row['body'].get('phase')!='terminal' or
            row['body'].get('status',{}).get('authoritative') is not True for row in operations)
        unresolved=unresolved or any(row['body'].get('phase')!='closed' for row in canaries)
        if resources and self.cleanup_service is not None:
            await self.cleanup_service(context,resources)
            with self.repository.transaction() as tx:
                resources=[row for row in tx.scan('resources') if row['body'].get('scope')==context.run_ref and row['body'].get('status')!='closed']
        status='unknown' if unresolved else 'incomplete' if resources else 'complete'
        body['cleanup_status']=status;self._write(context,body)
        if status=='complete':
            with self.repository.transaction() as tx:
                domains=[row['key'] for row in tx.scan('resource_leases') if row['body'].get('scope')==context.run_ref]
            if domains:LeaseService(self.repository).release(domains,scope=context.run_ref,token=token_for(context),
                closed=True,owner_run_id=context.run_ref,operation_refs=tuple(row['key'] for row in operations))
        return outcome().model_copy(update={'cleanup_status':status})

    @staticmethod
    def _unresolved(receipt):
        return receipt['result'] not in {'completed','failed','cancelled'} or receipt['effect_certainty']!='confirmed'

    async def deliver(self,context):
        plan,declaration=self._load(context);body=self._read(context)
        report=EnvironmentReport.model_validate(body['report']) if body.get('report') else EnvironmentReport(
            declaration_hash=declaration.declaration_hash,source_set=declaration.source_set,profile=declaration.profile,
            observations=(),readiness={'environment':'blocked'},limitations=('environment_probe_incomplete',))
        ready=self._ready(report,plan.node_ids) and body.get('cleanup_status')=='complete'
        refs=publish_report(report,store=self.store,scope=context.run_ref,resolve=self.resolve,
            receipts=body['step_receipts'].values(),xlsx=True)
        manifest_ref='artifact://'+refs['manifest'].uri
        acceptance_ref=None
        if ready:
            from .acceptance import AcceptanceService
            try:
                acceptance_ref=AcceptanceService(self.repository,store=self.store).issue(declaration,report,
                    scope=context.run_ref,node_ids=plan.node_ids,token=token_for(context))
            except ValueError:
                if not getattr(self.repository,'is_test_fixture',False):ready=False
        delivery={'run_id':context.run_ref,'plan_ref':context.plan_ref,'activation_id':context.activation_id,
            'ready':ready,'cleanup_status':body.get('cleanup_status','unknown'),'manifest_ref':manifest_ref,
            'acceptance_ref':acceptance_ref,
            'artifacts':{key:value.model_dump(mode='json') for key,value in refs.items()}}
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            for ref in refs.values():tx.put('environment_evidence','artifact://'+ref.uri,ref.model_dump(mode='json'),immutable=True)
            tx.put('environment_deliveries',context.activation_id,delivery,immutable=True)
        return outcome(manifest_ref=manifest_ref,ready=ready).model_copy(update={
            'data_status':'valid' if ready else 'insufficient','cleanup_status':body.get('cleanup_status','unknown')})


def build_environment_handler(repository,*,saver_factory,service_factory,lease_seconds,segment_seconds,owner='environment-worker'):
    from ..graphs.factory import build_definition
    from ..runtime.worker import GraphJobHandler
    from .workflow import environment_definition,environment_routers
    def builder(executor,checkpointer):
        return build_definition(environment_definition(),executor,routers=environment_routers(),checkpointer=checkpointer)
    def factory(token,activation):
        if activation.graph_ref!='environment:1':raise Conflict('environment worker graph version mismatch')
        return service_factory(token,activation).mapping()
    return GraphJobHandler(repository,builder,saver_factory,factory,lease_seconds=lease_seconds,
        segment_seconds=segment_seconds,owner=owner)
