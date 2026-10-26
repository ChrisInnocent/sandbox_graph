"""Shared command service. Preview/start bind one immutable plan and authorization."""
from uuid import uuid4
from ..domain.models import RunRecord,canonical_hash
from ..planning.compiler import Plan
from ..graphs.factory import GraphDefinition,validate_definition
from ..runtime.tasks import TaskBackend,JobSpec
from ..services.revisions import RevisionService
from ..persistence.repository import Conflict,positive_duration

class RunService:
    def __init__(self,repository,compiler): self.repository=repository; self.compiler=compiler
    def preview(self,selection,*,authorized_groups,evidence=None,graph_ref='default:1',contract_ref=''):
        plan=self.compiler.compile(selection,authorized_groups=authorized_groups,evidence=evidence,graph_ref=graph_ref,contract_ref=contract_ref)
        with self.repository.transaction() as tx: tx.put('plans',plan.hash,plan.model_dump(mode='json'),immutable=True)
        return plan
    def start(self,request_id,plan_hash,*,authorized_groups,evidence=None,execution_seconds=3600,deployment_binding=None):
        positive_duration(execution_seconds)
        inputs={'plan_hash':plan_hash,'auth':sorted(authorized_groups),'evidence':evidence or {},'execution_seconds':execution_seconds}
        if deployment_binding is not None:inputs['deployment_binding']=deployment_binding
        digest=canonical_hash(inputs)
        with self.repository.transaction() as tx:
            old=tx.get('commands',request_id)
            if old:
                if old['body']['hash']!=digest: raise Conflict('request_id input changed')
                return RunRecord.model_validate(old['body']['result'])
            stored=tx.get('plans',plan_hash)
            if not stored: raise Conflict('unknown preview plan hash')
            plan=Plan.model_validate(stored['body'])
            if plan.hash!=plan_hash: raise Conflict('plan content digest mismatch')
            if plan.auth_hash!=canonical_hash(sorted(authorized_groups)) or plan.evidence_hash!=canonical_hash(evidence or {}): raise Conflict('authorization/evidence drift; create a new preview')
            if plan.status!='ready': raise Conflict(f'plan cannot start: {plan.status}')
            if plan.stage in {'analysis','report'}:
                source=(evidence or {}).get('source_run_id')
                row=tx.get('runs',source) if source else None
                if not row: raise Conflict('stage-only start requires an exact source_run_id')
                run=RunRecord.model_validate(row['body'])
                if deployment_binding is not None:
                    binding=tx.get('evaluation_deployments',run.run_id)
                    if not binding or any(binding['body'].get(k)!=v for k,v in deployment_binding.items()):
                        raise Conflict('stage-only deployment differs from source run')
                payload=self._stage_payload(tx,row['body'],plan.stage,evidence or {})
                frozen=tx.get('analysis_inputs',payload['analysis_input_ref'])['body']
                if set(frozen.get('deliveries',{}))-authorized_groups: raise PermissionError('analysis source exceeds authorization')
                if plan.graph_ref!=run.graph_ref: raise Conflict('stage-only source graph version mismatch')
                activation_id=str(uuid4())
                if deployment_binding is not None:
                    tx.put('evaluation_stage_deadlines',activation_id,{'run_id':run.run_id,'deadline':tx.now+execution_seconds},immutable=True)
                TaskBackend.enqueue_in(tx,JobSpec(job_id=str(uuid4()),job_kind=plan.stage,scope_ref=run.run_id,thread_id=f'{run.run_id}:{plan.stage}:{request_id}',activation_id=activation_id,dedupe_key=f'stage-only:{request_id}',due_at=tx.now,deadline=tx.now+execution_seconds,run_id=run.run_id,dispatch_generation=run.dispatch_generation,payload=payload|{'plan_ref':run.active_plan_ref,'graph_ref':run.graph_ref}))
                tx.put('commands',request_id,{'hash':digest,'result':run.model_dump(mode='json')},immutable=True)
                return run
            identity=str(uuid4())
            run=RunRecord(run_id=identity,root_thread_id=f'run:{identity}',graph_ref=plan.graph_ref,contract_ref=plan.contract_ref,active_plan_ref=plan.hash,auth_ref=plan.auth_hash,lineage_id=identity)
            tx.put('runs',identity,run.model_dump(mode='json'),immutable=True)
            if deployment_binding is not None:
                if (set(deployment_binding)!={'runtime_hash','graph_ref'} or deployment_binding['graph_ref']!=plan.graph_ref
                        or not __import__('re').fullmatch('[a-f0-9]{64}',deployment_binding['runtime_hash'])):
                    raise ValueError('frozen deployment graph/hash required')
                tx.put('evaluation_deployments',identity,deployment_binding|{'deadline':tx.now+execution_seconds},immutable=True)
            tx.put('dispatch_gates',identity,{'open':True,'generation':1},immutable=True)
            tx.put('closure_revisions',identity,{'plan_ref':plan.hash,'revision':0},immutable=True)
            TaskBackend.enqueue_in(tx,JobSpec(job_id=str(uuid4()),job_kind=f'graph:{plan.stage}',scope_ref=identity,thread_id=run.root_thread_id,activation_id=str(uuid4()),dedupe_key=f'run:{identity}:start',due_at=tx.now,deadline=tx.now+execution_seconds,run_id=identity,payload={'plan_ref':plan.hash,'graph_ref':plan.graph_ref}))
            tx.put('commands',request_id,{'hash':digest,'result':run.model_dump(mode='json')},immutable=True)
            return run
    @staticmethod
    def _stage_payload(tx,run,kind,payload):
        if kind=='analysis':
            ref=payload.get('analysis_input_ref')
            frozen=tx.get('analysis_inputs',ref) if ref else None
            if not frozen or frozen['body']['plan_hash']!=run['active_plan_ref']:
                raise Conflict('analysis requires exact input from active plan')
            return {'analysis_input_ref':ref}
        identity=payload.get('analysis_ref')
        analysis=tx.get('analysis_results',identity) if identity else None
        if not analysis or (analysis['body']['run_id'],analysis['body']['plan_ref'])!=(run['run_id'],run['active_plan_ref']):
            raise Conflict('report requires exact analysis from this run and plan')
        return {'analysis_ref':identity,'analysis_input_ref':analysis['body']['analysis_input_ref'],'report_facts_ref':canonical_hash({'analysis_ref':identity,'report_version':1})}
    def status(self,run_id): return QueryService(self.repository).status(run_id)
    def _command(self,request_id,run_id,kind,payload=None):
        payload=payload or {}; digest=canonical_hash({'run_id':run_id,'kind':kind,'payload':payload})
        with self.repository.transaction() as tx:
            previous=tx.get('commands',request_id)
            if previous:
                if previous['body']['hash']!=digest: raise Conflict('request_id input changed')
                return previous['body']['result']
            run=tx.get('runs',run_id)
            if not run: raise KeyError(run_id)
            body=run['body']
            if kind in {'analysis','report'}: payload=self._stage_payload(tx,body,kind,payload)
            if kind=='cancel':
                gate=tx.get('dispatch_gates',run_id)
                tx.put('dispatch_gates',run_id,gate['body']|{'open':False,'policy':'cancel'})
                from ..services.lifecycle_stop import LifecycleStopService
                LifecycleStopService.request_in(tx,request_id='cancel:' + request_id,
                    run_ref=run_id,plan_ref=body['active_plan_ref'],graph_ref=body['graph_ref'],
                    reason=payload['reason'],policy='cancel')
            result={'request_id':request_id,'run_id':run_id,'kind':kind,'status':'queued'}
            TaskBackend.enqueue_in(tx,JobSpec(job_id=str(uuid4()),job_kind='cleanup' if kind=='cancel' else kind,scope_ref=run_id,thread_id=f'cancel:{run_id}:{request_id}' if kind=='cancel' else f'{run_id}:{kind}:{request_id}',activation_id=request_id,dedupe_key=f'command:{request_id}',due_at=tx.now,deadline=tx.now+3600,run_id=run_id,dispatch_generation=body['dispatch_generation'],payload=payload|{'plan_ref':body['active_plan_ref'],'graph_ref':body['graph_ref']}))
            tx.put('commands',request_id,{'hash':digest,'result':result},immutable=True)
            return result
    def resume(self,request_id,run_id,**payload): return self._command(request_id,run_id,'resume',payload)
    def cancel(self,request_id,run_id,reason): return self._command(request_id,run_id,'cancel',{'reason':reason})
    def retry(self,request_id,run_id,work_id): return self._command(request_id,run_id,'retry',{'work_id':work_id})
    def reanalyze(self,request_id,run_id,input_manifest_ref): return self._command(request_id,run_id,'analysis',{'analysis_input_ref':input_manifest_ref})
    def rerender(self,request_id,run_id,analysis_ref): return self._command(request_id,run_id,'report',{'analysis_ref':analysis_ref})

class QueryService:
    def __init__(self,repository): self.repository=repository
    def status(self,run_id):
        with self.repository.transaction() as tx:
            row=tx.get('runs',run_id)
            if not row: raise KeyError(run_id)
            work_ids={w['key'] for w in tx.scan('work_instances') if w['body'].get('run_id')==run_id}
            deliveries=[]
            for record in tx.scan('work_deliveries'):
                body=record['body']
                if not set(body.get('work_refs',())) & work_ids: continue
                deliveries.append({'work_refs':list(body.get('work_refs',())),'scope':body.get('scope'),
                    'case_index_ref':(body.get('result',{}).get('output') or {}).get('case_index_ref'),
                    'evidence':sorted(r['artifact_id'] for r in body.get('evidence',()))})
            analyses=[]
            for record in tx.scan('analysis_results'):
                body=record['body']
                if body.get('run_id')!=run_id: continue
                export=tx.get('analysis_exports',record['key'])
                analyses.append({'analysis_ref':record['key'],'analysis_input_ref':body.get('analysis_input_ref'),
                    'files':sorted((export['body'].get('files') if export else {}) or {})})
            archives=[a['body'] for a in tx.scan('archives') if a['body'].get('run_id')==run_id]
            return {'run':row['body'],'revision':row['revision'],
                    'jobs':[j['body'] for j in tx.scan('jobs') if j['body'].get('run_id')==run_id],
                    'operations':[o['body'] for o in tx.scan('operations') if o['body'].get('run_id')==run_id],
                    'deliveries':deliveries,'analyses':analyses,'archives':archives}
    def versions(self,kind,*,prefix=''):
        if kind not in {'graph_definitions','plans','deliveries','reviews','analysis_inputs','archives','revision_activations'}: raise ValueError('not a versioned query kind')
        with self.repository.transaction() as tx: return [r for r in tx.scan(kind) if r['key'].startswith(prefix)]

class GraphService:
    def __init__(self,repository,handlers): self.repository=repository; self.handlers=handlers
    def validate(self,definition): return validate_definition(definition,self.handlers)
    def propose_patch(self,definition):
        self.validate(definition)
        with self.repository.transaction() as tx: tx.put('graph_definitions',f'{definition.graph_id}:{definition.version}',definition.model_dump(mode='json'),immutable=True)
        return definition
    def get(self,graph_id,version):
        with self.repository.transaction() as tx:
            row=tx.get('graph_definitions',f'{graph_id}:{version}')
            if not row: raise KeyError((graph_id,version))
            return GraphDefinition.model_validate(row['body'])
    def list(self): return QueryService(self.repository).versions('graph_definitions')
    def diff(self,old,new):
        return {'added':sorted(set(new.nodes)-set(old.nodes)),'removed':sorted(set(old.nodes)-set(new.nodes)),'state_compatible':old.state_schema_version==new.state_schema_version,'new_run_required':old.hash!=new.hash}
    def activate_revision(self,**request):
        graph_id,version=request['graph_ref'].rsplit(':',1)
        definition=self.get(graph_id,version); self.validate(definition)
        with self.repository.transaction() as tx:
            if not tx.get('plans',request['candidate_plan_hash']): raise Conflict('candidate plan is missing')
        return RevisionService(self.repository).activate_revision(**request)
