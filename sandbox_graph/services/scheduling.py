"""Finite hierarchy expansion and durable independent group/work dispatch."""
from uuid import uuid4
from ..domain.models import WorkKey,canonical_hash
from ..planning.compiler import Plan
from ..runtime.tasks import TaskBackend,JobSpec
from ..persistence.repository import Conflict

class SchedulingService:
    def __init__(self,repository,*,review_service=None): self.repository=repository;self.review_service=review_service
    def materialize_reuse(self,run_id,group_id,*,token):
        """Finish explicitly authorized reuse before reserving measurement capacity."""
        from .reuse import ReuseService,ReuseContract
        from ..persistence.repository import StaleOwner
        reuse=ReuseService(self.repository,self.review_service)
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            candidates=[r['body'] for r in tx.scan('reuse_candidates') if (work:=tx.get('work_instances',r['key'])) and work['body']['run_id']==run_id and work['body']['group_id']==group_id and work['body']['status'] in {'pending','blocked'} and r['body']['status'] in {'waiting','accepted'}]
        projected=[]
        for candidate in candidates:
            with self.repository.transaction() as tx:
                # G0 precedes T2: let the registered benchmark first seal the
                # target's actual measurement parameters; T5 projects later.
                if not tx.get('measurement_bindings',candidate['work_id']): continue
            try:
                candidate=reuse.consider(candidate['work_id'],candidate['delivery_ref'],old_contract=ReuseContract.model_validate(candidate['old_contract']),new_contract=ReuseContract.model_validate(candidate['new_contract']),deadline=candidate['deadline'],allowed_source_scopes=candidate.get('authorized_source_scopes',()))
                if candidate['status']=='accepted': projected.append(reuse.project_accepted(candidate['work_id'],token=token))
            except StaleOwner: raise
            except (ValueError,PermissionError,OSError) as exc:
                with self.repository.transaction() as tx:
                    tx.assert_fence(token)
                    work=tx.get('work_instances',candidate['work_id'])['body']
                    if work['status'] not in {'pending','blocked'}: raise Conflict('reuse target started while rejecting evidence')
                    tx.put('reuse_candidates',candidate['work_id'],candidate|{'status':'blocked','reason':str(exc)})
                    tx.put('work_instances',candidate['work_id'],work|{'status':'blocked','queue_reason':'reuse_evidence_rejected','reuse_error':str(exc)})
        return projected
    def dispatch_groups(self,run_id,*,token,deadline):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            run=tx.get('runs',run_id)['body']; gate=tx.get('dispatch_gates',run_id)['body']
            if not gate['open']: raise Conflict('run dispatch closed')
            plan=Plan.model_validate(tx.get('plans',run['active_plan_ref'])['body'])
            if plan.stage in {'analysis','report'}: return []
            groups=sorted({c.group_id for c in plan.cases}); ids=[]
            for group in groups:
                key=f'{run_id}:{plan.hash}:{group}'
                existing=tx.get('groups',key)
                if existing: ids.append(existing['body']['job_id']); continue
                environment_refs={case.environment_ref for case in plan.cases if case.group_id==group and case.environment_ref}
                if len(environment_refs)>1: raise Conflict('group environment references conflict')
                environment_ref=next(iter(environment_refs),'')
                identity=str(uuid4()); activation=str(uuid4())
                TaskBackend.enqueue_in(tx,JobSpec(job_id=identity,job_kind='group',scope_ref=group,thread_id=f'{run_id}:group:{group}',activation_id=activation,dedupe_key=f'group:{key}',due_at=tx.now,deadline=deadline,run_id=run_id,dispatch_generation=gate['generation'],payload={'plan_ref':plan.hash,'graph_ref':run['graph_ref'],'group_id':group,'stage':plan.stage,'environment_ref':environment_ref}))
                tx.put('groups',key,{'run_id':run_id,'group_id':group,'plan_ref':plan.hash,'job_id':identity,'status':'pending'},immutable=True)
                ids.append(identity)
            return ids
    def expand_work(self,run_id,group_id,*,plan_ref):
        with self.repository.transaction() as tx:
            run=tx.get('runs',run_id)
            if not run: raise KeyError(run_id)
            plan=Plan.model_validate(tx.get('plans',plan_ref)['body'])
            if plan.stage!='all': return []
            ids=[]
            for case in plan.cases:
                if case.group_id!=group_id: continue
                gaps=[g for g in plan.gaps if g.case_id==case.case_id]
                if gaps:
                    tx.put('coverage',f'{run_id}:{plan_ref}:{case.case_id}',{'scope':group_id,'run_id':run_id,'plan_ref':plan_ref,'case_id':case.case_id,'status':'unsupported' if case.capability=='unsupported' else 'blocked','gaps':[g.model_dump(mode='json') for g in gaps]},immutable=True)
                    continue
                from ..planning.identity import binding_payload, condition_identity
                for condition in case.conditions:
                    key=WorkKey(group_id=group_id,scenario_id=case.scenario_id,method_id=case.method_id,method_version=case.method_version,condition_hash=canonical_hash(condition_identity(case,condition)),repeat_id=case.repeat_id)
                    identity=f'{run_id}:{plan_ref}:{key.identity}'
                    body={'work_id':identity,'key':key.model_dump(mode='json'),'run_id':run_id,'group_id':group_id,'plan_ref':plan_ref,'graph_ref':run['body']['graph_ref'],'contract_ref':case.contract_ref,'environment_ref':case.environment_ref,'condition':condition,**binding_payload(case),'domains':list(case.conflict_domains),'status':'pending','queue_reason':None}
                    existing=tx.get('work_instances',identity)
                    if not existing: tx.put('work_instances',identity,body,immutable=True)
                    ids.append(identity)
            return ids
    def dispatch_work(self,run_id,group_id,*,token,max_concurrency,deadline):
        if max_concurrency<1: raise ValueError('positive finite work concurrency required')
        self.materialize_reuse(run_id,group_id,token=token)
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            gate=tx.get('dispatch_gates',run_id)
            if not gate or not gate['body']['open']: raise Conflict('run dispatch closed')
            active=sum(j['body']['status'] in {'pending','running'} and j['body'].get('run_id')==run_id and j['body']['job_kind']=='work' for j in tx.scan('jobs'))
            slots=max(0,max_concurrency-active); ids=[]
            for row in tx.scan('work_instances'):
                work=row['body']
                if work['run_id']!=run_id or work['group_id']!=group_id or work['status']!='pending': continue
                candidate=tx.get('reuse_candidates',work['work_id'])
                if candidate and candidate['body']['status']!='remeasure':
                    tx.put('work_instances',row['key'],work|{'queue_reason':f'reuse_candidate_{candidate["body"]["status"]}'})
                    continue
                if not slots:
                    tx.put('work_instances',row['key'],work|{'queue_reason':'concurrency_budget'}); continue
                domains=sorted(set(work['domains']))
                if not domains:
                    tx.put('work_instances',row['key'],work|{'status':'blocked','queue_reason':'missing_conflict_domains'});continue
                reservations=[tx.get('resource_leases',d) for d in domains]
                if any(reservations):
                    # issue #3：区分“被隔离待核销”与“被其他 run 占用”，不再静默归为同一冲突。
                    quarantined=sorted(d for d,r in zip(domains,reservations)
                                       if r and r['body'].get('status')=='quarantine')
                    tx.put('work_instances',row['key'],work|{'queue_reason':'lease_quarantined' if quarantined else 'resource_conflict'})
                    continue
                # Reservation belongs to work, not group driver: takeover never frees it.
                for domain in domains: tx.put('resource_leases',domain,{'scope':work['work_id'],'thread_id':token.thread_id,'owner_epoch':token.owner_epoch,'status':'reserved'})
                identity=str(uuid4())
                TaskBackend.enqueue_in(tx,JobSpec(job_id=identity,job_kind='work',scope_ref=work['work_id'],thread_id=f'work:{canonical_hash(work["work_id"])}',activation_id=str(uuid4()),dedupe_key=f'work:{work["work_id"]}',due_at=tx.now,deadline=deadline,run_id=run_id,dispatch_generation=gate['body']['generation'],payload={'work_ref':work['work_id'],'group_id':group_id,'plan_ref':work['plan_ref'],'graph_ref':work['graph_ref']}))
                tx.put('work_instances',row['key'],work|{'status':'dispatched','queue_reason':None,'job_id':identity})
                slots-=1;ids.append(identity)
            return ids
