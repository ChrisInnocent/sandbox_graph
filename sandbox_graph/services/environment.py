"""Environment inspection, registered configuration, and independent reinspection."""
from typing import Literal,Protocol
from ..domain.models import Model,NodeResult,Operation,canonical_hash
from .workflows import blocked,token_for
from ..planning.compiler import Plan
from ..runtime.leases import LeaseService
from ..persistence.repository import Conflict

class EnvironmentSnapshot(Model):
    scope: str
    connected: bool
    authenticated: bool
    identity_confirmed: bool
    state: Literal['satisfied','drift','unknown']
    source_ref: str
    differences: tuple[str,...] = ()
    roles: tuple[str,...] = ()
    diagnostics: tuple[str,...] = ()

class EnvironmentProbe(Protocol):
    async def connect(self,scope:str,environment_ref:str) -> EnvironmentSnapshot: ...
    async def inspect(self,scope:str,environment_ref:str) -> EnvironmentSnapshot: ...

class EnvironmentHandlers:
    def __init__(self,repository,probe:EnvironmentProbe,*,deadline,operation_service=None,configuration_script=None,configuration_verified=False,only_check=False):
        self.repository=repository;self.probe=probe;self.deadline=deadline;self.operations=operation_service;self.script=configuration_script;self.verified=configuration_verified;self.only_check=only_check
    def mapping(self): return {'E1':self.connect,'E2':self.inspect,'C1':self.plan_changes,'C2':self.apply,'C3':self.verify}
    def _record(self,context,snapshot):
        snapshot=EnvironmentSnapshot.model_validate(snapshot)
        if snapshot.scope!=context.scope: raise PermissionError('probe scope mismatch')
        ref=canonical_hash(snapshot)
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context));tx.put('environment_snapshots',ref,snapshot.model_dump(mode='json'),immutable=True)
        return ref
    async def connect(self,context):
        environment_ref=context.input_refs.get('environment_ref')
        if not environment_ref: return blocked('INPUT_MISSING','versioned environment reference required')
        snapshot=await self.probe.connect(context.scope,environment_ref);ref=self._record(context,snapshot)
        if not (snapshot.connected and snapshot.authenticated and snapshot.identity_confirmed):
            return NodeResult(execution_status='blocked',coverage_status='blocked',continuation='GB',output={'snapshot_ref':ref,'reason':'connect/auth/identity failed'})
        return NodeResult(execution_status='succeeded',output={'snapshot_ref':ref},continuation='E2')
    async def inspect(self,context):
        environment_ref=context.input_refs.get('environment_ref')
        if not environment_ref: return blocked('INPUT_MISSING','versioned environment reference required')
        snapshot=await self.probe.inspect(context.scope,environment_ref);ref=self._record(context,snapshot)
        accessible=snapshot.connected and snapshot.authenticated and snapshot.identity_confirmed
        route='T8' if self.only_check else 'T0' if snapshot.state=='satisfied' and accessible else 'C1' if snapshot.state=='drift' and accessible else 'GB'
        return NodeResult(execution_status='succeeded',continuation=route,output={'snapshot_ref':ref,'state':snapshot.state,'differences':list(snapshot.differences)})
    async def plan_changes(self,context):
        if self.only_check: return blocked('CONTRACT_CONFLICT','check-only scope cannot configure')
        if not self.verified or self.script is None or self.operations is None: return blocked('ADAPTER_UNVERIFIED','verified registered configuration adapter required')
        receipt_ref=context.result_refs.get('E2')
        with self.repository.transaction() as tx:
            receipt=tx.get('receipts',receipt_ref) if receipt_ref else None
            snapshot_ref=receipt['body']['result']['output'].get('snapshot_ref') if receipt else None
            record=tx.get('environment_snapshots',snapshot_ref) if snapshot_ref else None
            if not record: return blocked('INPUT_MISSING','actual E2 snapshot required')
            snapshot=EnvironmentSnapshot.model_validate(record['body'])
            if snapshot.scope!=context.scope or snapshot.state!='drift' or not snapshot.authenticated or not snapshot.identity_confirmed: return blocked('CONTRACT_CONFLICT','configuration requires authenticated explicit differences')
            frozen=tx.get('plans',context.plan_ref)
            if not frozen: return blocked('INPUT_MISSING','frozen plan required for configuration domains')
            cases=Plan.model_validate(frozen['body']).cases
            domains=sorted({domain for case in cases if case.group_id==context.scope for domain in case.conflict_domains})
            if not domains: return blocked('INPUT_MISSING','configuration conflict domains required')
            plan={'scope':context.scope,'run_id':context.run_ref,'plan_ref':context.plan_ref,'domains':domains,'snapshot_ref':snapshot_ref,'differences':list(snapshot.differences),'script_ref':self.script.model_dump(mode='json')}
            ref=canonical_hash(plan);tx.put('configuration_plans',ref,plan,immutable=True)
        try: LeaseService(self.repository).acquire_all(domains,scope=context.scope,token=token_for(context),owner_run_id=context.run_ref)
        except Conflict: return blocked('RESOURCE_CONFLICT','configuration domains are reserved or quarantined')
        return NodeResult(execution_status='succeeded',continuation='C2',output={'configuration_ref':ref})
    async def apply(self,context):
        if self.only_check or not self.verified or self.operations is None or self.script is None: return blocked('ADAPTER_UNVERIFIED','configuration is not authorized/verified')
        with self.repository.transaction() as tx:
            receipt=tx.get('receipts',context.result_refs.get('C1',''))
            ref=receipt['body']['result']['output'].get('configuration_ref') if receipt else None
            plan=tx.get('configuration_plans',ref) if ref else None
            run=tx.get('runs',context.run_ref)
            if not plan or not run: return blocked('INPUT_MISSING','configuration plan/run missing')
            inputs=plan['body'];generation=run['body']['dispatch_generation']
            tx.assert_fence(token_for(context))
            for domain in inputs['domains']:
                lease=tx.get('resource_leases',domain)
                if not lease or lease['body']['scope']!=context.scope or lease['body'].get('run_id')!=context.run_ref:
                    return blocked('RESOURCE_CONFLICT','configuration domain ownership changed')
        identity=canonical_hash({'run':context.run_ref,'scope':context.scope,'configuration':ref})
        operation=Operation(operation_id=f'configuration:{identity}',input_hash=canonical_hash(inputs),script_ref=self.script,scope=context.scope,input=inputs,deadline=self.deadline,node_id='C2',run_id=context.run_ref,dispatch_generation=generation)
        with self.repository.transaction() as tx:
            existing=tx.get('operations',operation.operation_id)
            for domain in inputs['domains']:
                lease=tx.get('resource_leases',domain)
                if lease['body']['status']=='quarantine' and (not existing or existing['body']['phase']=='intent'):
                    return blocked('RESOURCE_CONFLICT','quarantined domain cannot start a new configuration')
        status=await self.operations.submit(operation,token_for(context))
        return NodeResult(execution_status='succeeded' if status.state=='completed' and status.authoritative else 'blocked',continuation='C3' if status.state=='completed' and status.authoritative else 'GB',operation_refs=(operation.operation_id,),output={'runner_state':status.state})
    async def verify(self,context):
        environment_ref=context.input_refs.get('environment_ref')
        if not environment_ref: return blocked('INPUT_MISSING','versioned environment reference required')
        snapshot=await self.probe.inspect(context.scope,environment_ref);ref=self._record(context,snapshot)
        satisfied=snapshot.state=='satisfied' and snapshot.connected and snapshot.authenticated and snapshot.identity_confirmed
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            c1=tx.get('receipts',context.result_refs.get('C1',''))
            config_ref=c1['body']['result']['output'].get('configuration_ref') if c1 else None
            plan=tx.get('configuration_plans',config_ref) if config_ref else None
            c2=tx.get('receipts',context.result_refs.get('C2',''))
            operations=c2['body']['result']['operation_refs'] if c2 else []
            terminal=bool(operations)
            for identity in operations:
                op=tx.get('operations',identity)
                terminal=terminal and bool(op and op['body']['phase']=='terminal' and op['body']['status']['state']=='completed' and op['body']['scope']==context.scope and op['body']['run_id']==context.run_ref)
            if not plan: return blocked('INPUT_MISSING','exact configuration plan required to release domains')
            domains=plan['body']['domains']
        leases=LeaseService(self.repository)
        if satisfied and terminal:
            leases.release(domains,scope=context.scope,token=token_for(context),closed=True,operation_refs=operations,owner_run_id=context.run_ref)
        else:
            leases.quarantine(domains,scope=context.scope,reason='configuration operation or reinspection unresolved',token=token_for(context))
            satisfied=False
        return NodeResult(execution_status='succeeded' if satisfied else 'blocked',continuation='T0' if satisfied else 'GB',output={'snapshot_ref':ref,'independent_reinspection':True})
