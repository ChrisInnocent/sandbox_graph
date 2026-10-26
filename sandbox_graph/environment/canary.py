"""One owned environment canary, state readiness and independently observed cleanup.

Creation and deletion intents survive disconnects. Recovery can discover the
original tagged instance, but never submits a second create. No command is run.
"""
import asyncio
from datetime import datetime,timezone
import json
import time
from dataclasses import asdict
from urllib.parse import quote
from pydantic import Field,model_validator
from .contracts import EnvironmentModel
from .workflow import EnvironmentScopePlan,EnvironmentRunService
from .evidence import SecretRedactor,assert_public_payload_safe
from .probes.auth import AuthProbe,AuthResponse
from ..domain.models import Operation,canonical_hash
from ..runtime.runner import RunnerStatus,RunnerCollection,CleanupResult
from ..persistence.repository import Conflict
from sandbox_bench.transport import TransportError

MANAGEMENT_ROLES={'kuasar':{'registry','placer','store','cache','proxy'},'agentenv':{'scheduler'},'cubesandbox':set()}

class ControlRoute(EnvironmentModel):
    endpoint_ref: str=Field(min_length=1)
    boot_id: str=Field(min_length=1)


class CanaryRequest(EnvironmentModel):
    declaration_hash: str=Field(pattern=r'^[a-f0-9]{64}$')
    node_ids: tuple[str,...]=Field(min_length=1,max_length=1)
    config_hash: str=Field(pattern=r'^[a-f0-9]{64}$')
    boot_id: str=Field(min_length=1)
    expected_vcpus: int=Field(gt=0,le=1024)
    expected_memory_mib: int=Field(ge=128,le=8*1024*1024)
    timeout_seconds: float=Field(gt=0,le=300)
    poll_seconds: float=Field(gt=0,le=10)
    max_observations: int=Field(default=20,ge=1,le=1000)
    control_routes: dict[str,ControlRoute]=Field(default_factory=dict,max_length=64)
    management_dependencies: dict[str,str]=Field(default_factory=dict,max_length=64)

    @model_validator(mode='after')
    def distinct_nodes(self):
        if len(set(self.node_ids))!=len(self.node_ids):raise ValueError('canary node scope must be unique')
        if set(self.node_ids)&set(self.control_routes):raise ValueError('compute and control scopes must be distinct')
        if (set(self.management_dependencies)&(set(self.node_ids)|set(self.control_routes))
                or any(not key or not boot for key,boot in self.management_dependencies.items())
                or self.management_dependencies and not self.control_routes):
            raise ValueError('distinct management dependencies require a group control route and boot identities')
        return self


class EnvironmentCanaryRunner:
    def __init__(self,repository,*,token,config,store,private_root,adapter_factory=None,management_probe=None):
        from sandbox_bench.adapters import Adapter
        self.repository=repository;self.token=token;self.config=dict(config);self.store=store
        self.private_root=str(private_root);self.adapter_factory=adapter_factory or Adapter
        self.management_probe=management_probe

    def _fence(self):
        with self.repository.transaction() as tx:tx.assert_fence(self.token)

    def _request(self,operation):
        payload=operation.input
        if 'step' in payload:
            from .contracts import ActionStep
            step=ActionStep.model_validate(payload['step'])
            if step.action_ref!='environment.canary.v1':raise ValueError('unregistered canary action')
            payload=step.input
        if set(payload)!={'canary'}:raise ValueError('registered canary input required')
        request=CanaryRequest.model_validate(payload['canary'])
        if request.config_hash!=canonical_hash(self.config):raise Conflict('canary adapter config changed')
        return request

    def _authorize_in(self,tx,operation,request):
        tx.assert_fence(self.token)
        run=tx.get('runs',operation.run_id)
        if not run or operation.scope!=operation.run_id:raise PermissionError('canary must own its environment run scope')
        row=tx.get('environment_plans',run['body']['active_plan_ref'])
        if not row:raise PermissionError('canary environment plan missing')
        plan=EnvironmentScopePlan.model_validate(row['body']);EnvironmentRunService.verify_authority_in(tx,plan)
        declaration=tx.get('environment_declarations',plan.declaration_hash)['body']
        if self.config.get('product')!=declaration['product']:raise PermissionError('canary product differs from declaration')
        node=next((value for value in declaration['nodes'] if value['node_id']==request.node_ids[0]),None)
        if not node or not set(node['roles'])&{'compute','runtime','server','conductor'}:
            raise PermissionError('canary requires a declared compute role')
        lease=tx.get('resource_leases','host:'+node['machine_id'])
        if (not lease or lease['body'].get('status')!='reserved' or lease['body'].get('scope')!=operation.scope
                or lease['body'].get('run_id')!=operation.run_id or lease['body'].get('thread_id')!=self.token.thread_id
                or lease['body'].get('owner_epoch')!=self.token.owner_epoch):
            raise PermissionError('canary requires the current host reservation')
        target='node:'+request.node_ids[0]
        if (not self.config.get('api_url') or declaration['service_endpoints'].get(target)!=self.config['api_url']):
            raise PermissionError('canary must target the declared node API endpoint')
        if (plan.mode!='prepare' or request.declaration_hash!=plan.declaration_hash or not set(request.node_ids)<=set(plan.node_ids)
                or 'environment.canary.v1' not in plan.allowed_actions or tx.now>=min(operation.deadline,plan.deadline)):
            raise PermissionError('canary is outside current environment authority')
        for identity,route in request.control_routes.items():
            control=next((item for item in declaration['nodes'] if item['node_id']==identity),None)
            if (not control or identity not in plan.node_ids or route.endpoint_ref!='node:'+identity
                    or not set(control['roles'])&{'control','router','gateway'}
                    or not declaration['service_endpoints'].get(route.endpoint_ref)):
                raise PermissionError('declared selected control API route required')
            owned=tx.get('resource_leases','host:'+control['machine_id'])
            if (not owned or owned['body'].get('status')!='reserved' or owned['body'].get('scope')!=operation.scope
                    or owned['body'].get('run_id')!=operation.run_id or owned['body'].get('thread_id')!=self.token.thread_id
                    or owned['body'].get('owner_epoch')!=self.token.owner_epoch):
                raise PermissionError('current control host reservation required')
        for identity in request.management_dependencies:
            manager=next((item for item in declaration['nodes'] if item['node_id']==identity),None)
            if (not manager or identity not in plan.node_ids or not set(manager['roles'])
                    or not set(manager['roles'])<=MANAGEMENT_ROLES.get(declaration['product'],set())):
                raise PermissionError('declared selected non-API management role required')
            owned=tx.get('resource_leases','host:'+manager['machine_id'])
            if (not owned or owned['body'].get('status')!='reserved' or owned['body'].get('scope')!=operation.scope
                    or owned['body'].get('run_id')!=operation.run_id or owned['body'].get('thread_id')!=self.token.thread_id
                    or owned['body'].get('owner_epoch')!=self.token.owner_epoch):
                raise PermissionError('current management host reservation required')
        gate=tx.get('dispatch_gates',operation.run_id)
        if not gate or not gate['body']['open'] or gate['body']['generation']!=operation.dispatch_generation:
            raise PermissionError('canary creation dispatch is closed')

    def _read(self,operation):
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token);row=tx.get('environment_canaries',operation.operation_id)
            if row and row['body']['request_hash']!=canonical_hash(operation):raise Conflict('original canary request changed')
            return row['body'] if row else None

    def _write(self,operation,body):
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token);tx.put('environment_canaries',operation.operation_id,body)

    def _adapter(self,body,*,endpoint=None):
        config=self.config|{'instance_credentials':{'path':self.private_root,'scope':body['owner_tag'],'generation':1}}
        if endpoint is not None:config['api_url']=endpoint
        return self.adapter_factory(config)

    async def _call(self,method,*args):
        self._fence()
        return await asyncio.to_thread(method,*args)

    async def _authenticate(self,adapter,sandbox,remaining):
        original=dict(adapter.headers);correct=original.get('X-API-Key','')
        wrong=(correct[:-1]+('0' if correct[-1:]!='0' else '1')) if correct else None
        path='/sandboxes/'+quote(sandbox['sandbox_id'],safe='')
        async def call(credential):
            adapter.headers={key:value for key,value in original.items() if key.lower()!='x-api-key'}
            if credential is not None:adapter.headers['X-API-Key']=credential
            try:
                response=await self._call(lambda:adapter._control('GET',path,remaining(),sandbox_id=sandbox['sandbox_id']))
                return AuthResponse(response.status,json.loads(response.body))
            except TransportError as exc:return AuthResponse(exc.status or 0)
            finally:adapter.headers=original
        return await AuthProbe(call).verify(correct_credential=correct,wrong_credential=wrong,
            rejected_statuses=(401,403,404),accepted_statuses=(200,),endpoint=path,
            ownership_check=lambda response:isinstance(response.body,dict) and response.body.get('sandboxID')==sandbox['sandbox_id'])

    def _adopt(self,operation,request,body,sandbox_id):
        if not isinstance(sandbox_id,str) or not 0<len(sandbox_id)<=512:raise ValueError('canary returned no bounded instance ID')
        ref='environment-resource:'+canonical_hash({'declaration':request.declaration_hash,'sandbox_id':sandbox_id})
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token);old=tx.get('resources',ref)
            if old and (old['body']['scope'],old['body']['operation_id'])!=(operation.scope,operation.operation_id):
                raise PermissionError('canary resource belongs to another operation')
            resource={'scope':operation.scope,'operation_id':operation.operation_id,'status':'active',
                'sandbox_id':sandbox_id,'run_id':operation.run_id,'kind':'environment_canary'}
            tx.put('resources',ref,resource)
            body.update(sandbox_id=sandbox_id,resource_ref=ref,phase='created')
            tx.put('environment_canaries',operation.operation_id,body)
        return body

    async def submit(self,operation):
        request=self._request(operation)
        old=self._read(operation)
        if old:return await self._drive(operation,request,old,allow_create=False)
        with self.repository.transaction() as tx:
            self._authorize_in(tx,operation,request)
            body={'operation_id':operation.operation_id,'scope':operation.scope,'run_id':operation.run_id,
                'request_hash':canonical_hash(operation),'owner_tag':'env-canary-'+canonical_hash(operation.operation_id)[:32],
                'phase':'create_started','sandbox_id':None,'resource_ref':None,'delete_started':False,
                'observations':[],'reconciliations':0}
            tx.put('environment_canaries',operation.operation_id,body,immutable=True)
        adapter=self._adapter(body)
        try:sandbox=await self._call(adapter.create,body['owner_tag'],request.timeout_seconds)
        except Exception as exc:
            body['create_error']=type(exc).__name__;self._write(operation,body)
            return RunnerStatus(state='unknown',task_handle=operation.operation_id,reason='original_canary_create_unresolved')
        body=self._adopt(operation,request,body,sandbox['sandbox_id'])
        return await self._drive(operation,request,body,allow_create=False,adapter=adapter)

    def _original(self,operation_id,scope):
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token);row=tx.get('operations',operation_id)
            if not row or row['body']['scope']!=scope:raise PermissionError('canary original operation scope mismatch')
            return Operation.model_validate(row['body']['request'])

    async def reconcile(self,operation_id,scope):
        operation=self._original(operation_id,scope);body=self._read(operation)
        if body is None:return RunnerStatus(state='unknown',reason='original_canary_journal_missing')
        return await self._drive(operation,self._request(operation),body,allow_create=False)

    async def _drive(self,operation,request,body,*,allow_create,adapter=None):
        if body.get('terminal'):return RunnerStatus.model_validate(body['terminal'])
        if body['reconciliations']>=16:return RunnerStatus(state='unknown',reason='canary_observation_budget_exhausted')
        body['reconciliations']+=1;self._write(operation,body)
        adapter=adapter or self._adapter(body)
        until=time.monotonic()+request.timeout_seconds
        def remaining():return max(.001,until-time.monotonic())
        if body['sandbox_id'] is None:
            try:owned=await self._call(adapter.list_owned,body['owner_tag'],remaining())
            except Exception:return RunnerStatus(state='unknown',reason='original_canary_ownership_unresolved')
            if len(owned)!=1:return RunnerStatus(state='unknown',reason='original_canary_identity_not_unique')
            body=self._adopt(operation,request,body,owned[0]['sandbox_id'])
        sandbox={'sandbox_id':body['sandbox_id']}
        if not body.get('functional'):
            functional='failed';reason='state_not_ready';observed_spec=None
            for _ in range(request.max_observations):
                if time.monotonic()>=until:break
                try:
                    observed=await self._call(adapter.inspect,sandbox,remaining())
                    state=observed.get('state','unknown')
                    body['observations'].append({'state':state})
                    self._write(operation,body)
                    if state=='running':
                        observed_spec=await self._call(adapter.effective_spec,sandbox,remaining())
                        expected={'vcpus':request.expected_vcpus,'memory_mib':request.expected_memory_mib}
                        functional='passed' if observed_spec==expected else 'failed'
                        reason=None if functional=='passed' else 'effective_spec_mismatch_or_unavailable'
                        auth=await self._authenticate(adapter,sandbox,remaining)
                        body['authentication']=asdict(auth)
                        if auth.status!='verified':functional='failed';reason='protected_route_authentication_failed'
                        body['control_authentication']={}
                        with self.repository.transaction() as tx:
                            declaration=tx.get('environment_declarations',request.declaration_hash)['body']
                        for identity,route in request.control_routes.items():
                            endpoint=declaration['service_endpoints'][route.endpoint_ref]
                            control=self._adapter(body,endpoint=endpoint)
                            proof=await self._authenticate(control,sandbox,remaining)
                            body['control_authentication'][identity]=asdict(proof)|{'endpoint_ref':route.endpoint_ref}
                            if proof.status!='verified':functional='failed';reason='control_route_authentication_failed'
                        body['management_dependencies']={}
                        from .contracts import EnvironmentDeclaration
                        declared=EnvironmentDeclaration.model_validate(declaration)
                        for identity,boot in request.management_dependencies.items():
                            node=next(value for value in declared.nodes if value.node_id==identity)
                            proof={'status':'unavailable','reason':'management_probe_required'}
                            if self.management_probe is not None:
                                proof=await asyncio.wait_for(self.management_probe(declared,node,boot),remaining())
                            if (proof.get('status')!='verified' or proof.get('node_id')!=identity
                                    or proof.get('boot_id')!=boot or proof.get('roles')!=list(node.roles)
                                    or proof.get('evidence_level')!='group_dependency'):
                                functional='failed';reason='management_dependency_unverified'
                            body['management_dependencies'][identity]=proof
                        break
                    if state in {'failed','error','deleted','stopped'}:break
                except Exception as exc:reason=type(exc).__name__;break
                await asyncio.sleep(min(request.poll_seconds,remaining()))
            body['functional']={'status':functional,'reason':reason,'spec':observed_spec,
                'observed_at':datetime.now(timezone.utc).isoformat()}
            self._write(operation,body)
        if not body['delete_started']:
            body['delete_started']=True;self._write(operation,body)
            try:
                await self._call(adapter.delete,sandbox,remaining());body['delete_ack']=True
            except Exception as exc:body['delete_error']=type(exc).__name__
            self._write(operation,body)
        deleted=False
        for _ in range(request.max_observations):
            if time.monotonic()>=until:break
            try:
                observed=await self._call(adapter.inspect,sandbox,remaining())
                body['observations'].append({'cleanup_state':observed.get('state','unknown')});self._write(operation,body)
            except TransportError as exc:
                if exc.status==404:deleted=True;break
                break
            except Exception:break
            await asyncio.sleep(min(request.poll_seconds,remaining()))
        if not deleted:
            return RunnerStatus(state='unknown',task_handle=operation.operation_id,resource_refs=(body['resource_ref'],),reason='canary_cleanup_not_verified')
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token);resource=tx.get('resources',body['resource_ref'])
            if not resource or resource['body']['scope']!=operation.scope or resource['body']['operation_id']!=operation.operation_id:
                raise PermissionError('canary cleanup ownership changed')
            tx.put('resources',body['resource_ref'],resource['body']|{'status':'closed','cleanup_observation':'detail_404'})
        refs=[]
        for kind,status in [('functional',body['functional']['status']),('cleanup','complete')]:
            proof={'kind':kind,'status':status,'declaration_hash':request.declaration_hash,
                'scope':list(request.node_ids)+sorted(request.control_routes)+sorted(request.management_dependencies),'resource_ids':[body['sandbox_id']],
                'boot_ids':{request.node_ids[0]:request.boot_id}|{key:value.boot_id for key,value in request.control_routes.items()}|request.management_dependencies,
                'control_authentication':body.get('control_authentication',{}),
                'management_dependencies':body.get('management_dependencies',{}),
                'boot_id':request.boot_id,'observed_at':body['functional']['observed_at'] if kind=='functional' else datetime.now(timezone.utc).isoformat(),
                'authentication':body.get('authentication'),
                'operation_id':operation.operation_id,'owner_tag':body['owner_tag'],
                'observations':body['observations'],'spec':body['functional'].get('spec'),
                'cleanup_observation':'detail_404' if kind=='cleanup' else None}
            proof=SecretRedactor().redact(proof);assert_public_payload_safe(proof)
            ref=self.store.commit([json.dumps(proof,sort_keys=True,separators=(',',':'),allow_nan=False).encode()],
                scope=operation.scope,media_type='application/json',producer='environment-canary.v1')
            name='artifact://'+ref.uri;refs.append(name)
            with self.repository.transaction() as tx:
                tx.assert_fence(self.token);tx.put('environment_evidence',name,ref.model_dump(mode='json'),immutable=True)
        result=RunnerStatus(state='completed' if body['functional']['status']=='passed' else 'failed',authoritative=True,
            task_handle=operation.operation_id,artifact_refs=tuple(refs),output={'functional':body['functional']['status'],'cleanup':'complete'})
        body.update(phase='closed',terminal=result.model_dump(mode='json'));self._write(operation,body)
        return result

    async def cancel(self,operation_id,scope,reason):
        operation=self._original(operation_id,scope);body=self._read(operation)
        if body is None:return RunnerStatus(state='unknown',reason='original_canary_journal_missing')
        if not body.get('functional'):
            body['functional']={'status':'failed','reason':'cancel_requested','spec':None,
                'observed_at':datetime.now(timezone.utc).isoformat()};self._write(operation,body)
        return await self._drive(operation,self._request(operation),body,allow_create=False)

    async def collect(self,operation_id,scope,last_seq=0):
        operation=self._original(operation_id,scope);body=self._read(operation) or {}
        refs=tuple(body.get('terminal',{}).get('artifact_refs',()))
        return RunnerCollection(last_seq=1 if refs else 0,artifact_refs=refs)

    async def cleanup(self,scope,resource_ids):
        remaining=[];refs=[]
        for identity in resource_ids:
            with self.repository.transaction() as tx:
                tx.assert_fence(self.token);row=tx.get('resources',identity)
                if not row or row['body']['scope']!=scope or row['body'].get('kind')!='environment_canary':
                    raise PermissionError('cleanup target is not an owned canary')
                if row['body']['status']=='closed':continue
                op_id=row['body']['operation_id']
            result=await self.cancel(op_id,scope,'mandatory_cleanup');refs.extend(result.artifact_refs)
            if not result.authoritative:remaining.append(identity)
        return CleanupResult(status='incomplete' if remaining else 'complete',resource_ids=resource_ids,
            remaining=tuple(remaining),evidence_refs=tuple(refs))
