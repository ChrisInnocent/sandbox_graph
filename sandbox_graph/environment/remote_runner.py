"""Per-primitive remote recipes using the ordinary durable OperationService ledger."""
from dataclasses import asdict
import json
from .contracts import ActionStep,RepairPlan
from .planner import PlanBuildResult,PlanBlocker
from .transport import SSHRequest
from .declarations import _unique_json_object
from .evidence import SecretRedactor,assert_public_payload_safe
from ..domain.models import canonical_hash,Operation
from ..runtime.runner import RunnerStatus,RunnerCollection,CleanupResult
from ..persistence.repository import Conflict


class RecipeRepairPlanner:
    def __init__(self,planner,bindings,canaries=None):
        self.planner=planner;self.bindings=dict(bindings);self.canaries=dict(canaries or {})

    def plan(self,declaration,report,**kwargs):
        result=self.planner.plan(declaration,report,**kwargs)
        if result.status!='ready':return result
        expanded=[];tails={}
        for root in result.plan.steps:
            if root.action_ref=='environment.canary.v1':
                binding=self.canaries.get(root.node_id)
                if binding is None:raise ValueError('canary node configuration is missing')
                from .canary import CanaryRequest
                compute=binding.get('compute_node_id',root.node_id)
                controls=set(binding.get('control_node_ids',()))
                managers=set(binding.get('management_node_ids',()))
                declared={node.node_id:node for node in declaration.nodes}
                if compute!=root.node_id:
                    if set(declared[root.node_id].roles)&{'control','router','gateway'}:controls.add(root.node_id)
                    else:managers.add(root.node_id)
                if compute not in declared or not set(declared[compute].roles)&{'compute','runtime','server','conductor'}:
                    raise ValueError('canary must bind a declared compute node')
                boots={}
                for node_id in {compute,*controls,*managers}:
                    if node_id not in declared:raise ValueError('declared canary participant required')
                    identities={obs.boot_id for obs in report.observations if obs.node_id==node_id and obs.boot_id}
                    if len(identities)!=1:raise ValueError('canary requires one current observed boot per participating node')
                    boots[node_id]=next(iter(identities))
                routes={node:{'endpoint_ref':'node:'+node,'boot_id':boots[node]} for node in controls}
                request=CanaryRequest(declaration_hash=declaration.declaration_hash,node_ids=(compute,),
                    config_hash=canonical_hash(binding['config']),boot_id=boots[compute],control_routes=routes,
                    management_dependencies={node:boots[node] for node in managers},**binding['request'])
                data={'canary':request.model_dump(mode='json')}
                expanded.append(root.model_copy(update={'input':data,'input_hash':canonical_hash({'action':root.action_ref,'input':data}),
                    'depends_on':tuple(tails[dep] for dep in root.depends_on)}))
                tails[root.step_id]=root.step_id
                continue
            binding=self.bindings.get((root.node_id,root.action_ref))
            if not binding:return PlanBuildResult('blocked',result.plan.model_copy(update={'steps':()}),
                (PlanBlocker('RECIPE_BINDING_MISSING',root.node_id,root.step_id,root.action_ref),))
            if canonical_hash([asdict(step) for step in binding['steps']])!=binding['recipe_hash']:
                raise ValueError('controller recipe lock digest differs')
            dependencies=tuple(tails[dep] for dep in root.depends_on)
            for primitive in binding['steps']:
                identity=canonical_hash({'parent_step':root.step_id,'primitive':primitive.step_id})[:32]
                data={key:binding[key] for key in ('recipe','recipe_hash','expected_before')}
                data['step_id']=primitive.step_id
                step=root.model_copy(update={'step_id':identity,'input':data,
                    'input_hash':canonical_hash({'action':root.action_ref,'input':data}),
                    'depends_on':dependencies,'timeout_seconds':primitive.timeout_s})
                expanded.append(step);dependencies=(identity,)
            if not dependencies:raise ValueError('empty mutation recipe is not an installation')
            tails[root.step_id]=dependencies[-1]
        if len(expanded)>kwargs.get('max_steps',64):return PlanBuildResult('blocked',result.plan.model_copy(update={'steps':()}),
            (PlanBlocker('STEP_BUDGET_EXCEEDED','','',str(len(expanded))),))
        body=result.plan.model_dump(mode='json',exclude={'schema_version','plan_hash'})
        body['steps']=[step.model_dump(mode='json') for step in expanded]
        return PlanBuildResult('ready',RepairPlan(plan_hash=canonical_hash(body),**body),())


class RemoteRecipeRunner:
    def __init__(self,repository,*,token,declaration,transport,resolver,grants,store):
        self.repository=repository;self.token=token;self.declaration=declaration
        self.nodes={node.node_id:node for node in declaration.nodes}
        self.transport=transport;self.resolver=resolver;self.grants=grants;self.store=store

    def _request(self,node,payload,timeout):
        return SSHRequest(address=node.ssh_address,port=node.ssh_port,user=node.ssh_user,
            known_hosts=self.resolver.resolve(node.known_hosts_ref),identity_file=self.resolver.resolve(node.automation_credential_ref),
            payload=payload,timeout_seconds=timeout)

    def _binding(self,operation):
        step=ActionStep.model_validate(operation.input['step'])
        node=self.nodes[step.node_id]
        data=dict(step.input)
        if set(data)!={'recipe','recipe_hash','step_id','expected_before'}:raise ValueError('frozen recipe primitive input required')
        data['operation_id']='environment-step:'+canonical_hash({'operation':operation.operation_id,'input_hash':operation.input_hash})
        return step,node,data

    def _status(self,operation,node,data,row):
        if row is None:return RunnerStatus(state='unknown',reason='original_remote_journal_missing')
        if (row.get('operation_id')!=data['operation_id'] or row.get('machine_id')!=node.machine_id
                or row.get('action')!='product.recipe.step.v1'
                or row.get('input_hash')!=canonical_hash({'action':'product.recipe.step.v1','input':data})
                or not row.get('boot_id')):
            raise Conflict('remote journal identity does not match original operation')
        state={'accepted':'not_submitted','started':'running','completed':'completed','failed':'failed',
            'cancelled':'cancelled','unknown':'unknown'}.get(row['state'],'unknown')
        public=SecretRedactor().redact({'operation_id':operation.operation_id,'remote':row})
        assert_public_payload_safe(public)
        ref=self.store.commit([json.dumps(public,sort_keys=True,separators=(',',':'),allow_nan=False).encode()],
            scope=operation.scope,media_type='application/json',producer='environment-remote-receipt.v1')
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            tx.put('environment_evidence','artifact://'+ref.uri,ref.model_dump(mode='json'),immutable=True)
        authoritative=state in {'completed','failed','cancelled'}
        return RunnerStatus(state=state,task_handle=data['operation_id'],authoritative=authoritative,
            artifact_refs=('artifact://'+ref.uri,),output={'remote_state':row['state']})

    async def submit(self,operation):
        step,node,data=self._binding(operation)
        def authorize(tx):
                tx.assert_fence(self.token)
                original=tx.get('operations',operation.operation_id)
                if (not original or original['body'].get('phase')!='submission_started'
                        or canonical_hash(original['body'].get('request'))!=canonical_hash(operation)):
                    raise PermissionError('original operation no longer authorizes remote submission')
                run=tx.get('runs',operation.run_id)
                from .workflow import EnvironmentRunService,EnvironmentScopePlan
                if not run:raise Conflict('environment run disappeared')
                scope=EnvironmentScopePlan.model_validate(tx.get('environment_plans',run['body']['active_plan_ref'])['body'])
                EnvironmentRunService.verify_authority_in(tx,scope)
                if tx.now>=scope.deadline or step.action_ref not in scope.allowed_actions or step.node_id not in scope.node_ids:
                    raise PermissionError('remote recipe no longer covered by environment scope')
        def issue(challenge,round_trip):
            return self.grants.issue_for_challenge(token=self.token,challenge=challenge,round_trip_seconds=round_trip,
                authorize_in=authorize,
                operation_id=data['operation_id'],step_id=data['step_id'],run_id=operation.run_id,scope=operation.scope,
                machine_id=node.machine_id,resource_domains=('host:'+node.machine_id,),action='product.recipe.step.v1',input_data=data)
        with self.repository.transaction() as tx:remaining=operation.deadline-tx.now
        if remaining<=0:raise PermissionError('environment operation deadline expired')
        row=await self.transport.request_granted(self._request(node,{'action':'product.recipe.step.v1','input':data},
            min(remaining,step.timeout_seconds+20)),issue)
        return self._status(operation,node,data,row)

    def _original(self,operation_id,scope):
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token);row=tx.get('operations',operation_id)
            if not row or row['body']['scope']!=scope:raise PermissionError('original environment operation missing or belongs to another scope')
            return Operation.model_validate(row['body']['request'])

    async def reconcile(self,operation_id,scope):
        operation=self._original(operation_id,scope);step,node,data=self._binding(operation)
        response=await self.transport.request(self._request(node,{'action':'operation.journal.query.v1',
            'input':{'operation_id':data['operation_id']}},10))
        if response.returncode:return RunnerStatus(state='unknown',reason='remote_journal_query_unavailable')
        body=json.loads(response.stdout,object_pairs_hook=_unique_json_object)
        if body.get('machine_id')!=node.machine_id:raise Conflict('remote host identity changed')
        return self._status(operation,node,data,body.get('observation'))

    async def collect(self,operation_id,scope,last_seq=0):
        original=self._original(operation_id,scope)
        with self.repository.transaction() as tx:row=tx.get('operations',operation_id)['body']
        refs=tuple(row.get('status',{}).get('artifact_refs',()))
        return RunnerCollection(last_seq=1 if refs else 0,artifact_refs=refs)

    async def cancel(self,operation_id,scope,reason):
        status=await self.reconcile(operation_id,scope)
        if status.authoritative:return status
        operation=self._original(operation_id,scope);step,node,data=self._binding(operation)
        identity={'operation_id':data['operation_id'],
            'input_hash':canonical_hash({'action':'product.recipe.step.v1','input':data})}
        grant=self.grants.issue_safety(original_operation_id=operation_id,token=self.token,
            operation_id=data['operation_id'],step_id=data['step_id'],run_id=operation.run_id,scope=scope,
            machine_id=node.machine_id,resource_domains=('host:'+node.machine_id,),
            action='operation.recipe.cancel.v1',input_data=identity)
        response=await self.transport.request(self._request(node,{'action':'operation.recipe.cancel.v1',
            'input':identity,'grant':grant.model_dump(mode='json')},10))
        if response.returncode:return status.model_copy(update={'reason':'original_remote_stop_unconfirmed'})
        body=json.loads(response.stdout,object_pairs_hook=_unique_json_object)
        if body.get('machine_id')!=node.machine_id:raise Conflict('remote host identity changed')
        return self._status(operation,node,data,body.get('observation'))

    async def compensate(self,operation_id,scope):
        operation=self._original(operation_id,scope);step,node,data=self._binding(operation)
        identity={'operation_id':data['operation_id'],'input_hash':canonical_hash({'action':'product.recipe.step.v1','input':data})}
        key='environment-compensation:'+canonical_hash({'operation_id':operation_id,'scope':scope,'identity':identity})
        intent={'operation_id':operation_id,'scope':scope,'remote_identity':identity,'status':'started'}
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token);old=tx.get('environment_compensations',key)
            if old and old['body']['status']=='completed':
                from ..domain.models import EvidenceRef
                evidence=tx.get('environment_evidence',old['body']['evidence_ref'])
                if not evidence:raise Conflict('original compensation evidence is missing')
                self.store.verify(EvidenceRef.model_validate(evidence['body']))
                return old['body']['observation']
            if not old:tx.put('environment_compensations',key,intent,immutable=True)
        grant=self.grants.issue_safety(original_operation_id=operation_id,token=self.token,
            operation_id=data['operation_id'],step_id=data['step_id'],run_id=operation.run_id,scope=scope,
            machine_id=node.machine_id,resource_domains=('host:'+node.machine_id,),
            action='operation.recipe.compensate.v1',input_data=identity)
        response=await self.transport.request(self._request(node,{'action':'operation.recipe.compensate.v1',
            'input':identity,'grant':grant.model_dump(mode='json')},10))
        if response.returncode:raise Conflict('original file compensation unconfirmed')
        body=json.loads(response.stdout,object_pairs_hook=_unique_json_object)
        if body.get('machine_id')!=node.machine_id or not body.get('boot_id'):raise Conflict('remote host identity changed')
        observed=body.get('observation',{});path=observed.get('path')
        if (observed.get('status')!='completed' or path not in data['expected_before']
                or observed.get('restored_sha256')!=data['expected_before'][path]):
            raise Conflict('compensation does not prove the original file restoration')
        public=SecretRedactor().redact(intent|{'machine_id':node.machine_id,'boot_id':body['boot_id'],
            'status':'completed','observation':observed})
        assert_public_payload_safe(public)
        ref=self.store.commit([json.dumps(public,sort_keys=True,allow_nan=False).encode()],scope=scope,
            media_type='application/json',producer='environment-compensation.v1')
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            tx.put('environment_evidence','artifact://'+ref.uri,ref.model_dump(mode='json'),immutable=True)
            tx.put('environment_compensations',key,public|{'evidence_ref':'artifact://'+ref.uri})
        return observed

    async def cleanup(self,scope,resource_ids):
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            rows=[tx.get('resources',identity) for identity in resource_ids]
            if any(not row or row['body']['scope']!=scope for row in rows):raise PermissionError('cleanup resource ownership mismatch')
            remaining=tuple(identity for identity,row in zip(resource_ids,rows) if row['body']['status']!='closed')
        return CleanupResult(status='incomplete' if remaining else 'complete',resource_ids=resource_ids,remaining=remaining)
