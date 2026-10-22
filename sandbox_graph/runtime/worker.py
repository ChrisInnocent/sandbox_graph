"""Durable queue worker and transactional outbox/wakeup dispatcher.

Registered handlers perform business work; an unknown job kind is a persisted
blocked result. Notification delivery is optional: every iteration scans durable
outbox and waits before claiming due work.
"""
import asyncio
from contextlib import suppress
from uuid import uuid4
from pydantic import Field
from ..domain.models import Model,DeliveryManifest,canonical_hash
from ..persistence.repository import Conflict,StaleOwner
from .driver import Driver,Activation
from .events import EventService,EventEnvelope
from .tasks import TaskBackend,JobSpec

class WorkerPolicy(Model):
    claim_seconds: float = Field(gt=0)
    job_seconds: float = Field(gt=0)
    poll_seconds: float = Field(gt=0)
    max_outbox_per_tick: int = Field(default=100,gt=0)

class GraphJobHandler:
    def __init__(self,repository,graph_builder,saver_factory,handler_factory,*,lease_seconds,segment_seconds,owner='worker',delivery_finalizer=None,agent_allowlists=None):
        self.repository=repository
        self.graph_builder=graph_builder
        self.saver_factory=saver_factory
        self.handler_factory=handler_factory
        self.lease_seconds=lease_seconds
        self.segment_seconds=segment_seconds
        self.owner=owner
        self.delivery_finalizer=delivery_finalizer
        self.agent_allowlists=dict(agent_allowlists or {})
    async def __call__(self,job):
        payload=job['payload']
        activation=Activation(activation_id=job['activation_id'],run_id=job['run_id'],thread_id=job['thread_id'],scope=job['scope_ref'],plan_ref=payload['plan_ref'],graph_ref=payload['graph_ref'],input_refs={k:str(v) for k,v in payload.items() if k not in {'plan_ref','graph_ref','continuation_of','decision'}},lease_seconds=self.lease_seconds,segment_seconds=self.segment_seconds,continuation_of=payload.get('continuation_of'))
        with self.repository.transaction() as tx:
            tx.put('activation_jobs',activation.activation_id,{'job_kind':job['job_kind'],'run_id':job['run_id'],'job_id':job['job_id']},immutable=True)
        driver=Driver(self.repository,{},self.saver_factory,self.graph_builder,self.owner,handler_factory=self.handler_factory,agent_allowlists=self.agent_allowlists)
        result=await driver.start(activation,decision=payload.get('decision'))
        return await self.finalize(job,result)
    async def finalize(self,job,result):
        statuses=[r['execution_status'] for r in result.get('results',{}).values()]
        execution='blocked' if '__interrupt__' in result or 'blocked' in statuses else 'failed' if 'failed' in statuses else 'succeeded'
        if job['job_kind']=='work' and '__interrupt__' not in result:
            manifest=None
            if self.delivery_finalizer:
                manifest=await self.delivery_finalizer(job,result)
            else:
                with self.repository.transaction() as tx:
                    ref=result.get('result_refs',{}).get('N10')
                    receipt=tx.get('receipts',ref) if ref else None
                    if receipt: manifest=receipt['body']['result']['output'].get('delivery_manifest')
            if manifest is None:
                return {'execution_status':'blocked','graph_result':result,'activation_id':job['activation_id'],'error':{'code':'DELIVERY_MISSING','reason':'N10 delivery_manifest or delivery_finalizer required'}}
            manifest=DeliveryManifest.model_validate(manifest)
            if manifest.plan_ref!=job['payload']['plan_ref'] or job['scope_ref'] not in manifest.work_refs:
                raise Conflict('work delivery does not match frozen work/plan identity')
            with self.repository.transaction() as tx:
                work=tx.get('work_instances',job['scope_ref'])
                group=work['body']['group_id'] if work else job['payload'].get('group_id',job['scope_ref'])
                if manifest.scope not in {job['scope_ref'],group}:
                    raise Conflict('work delivery exceeds authorized group/work scope')
                if work and work['body']['contract_ref'] and manifest.contract_ref!=work['body']['contract_ref']:
                    raise Conflict('work delivery contract changed')
                if job.get('owner_epoch',0)>0:
                    claim=tx.get('jobs',job['job_id'])
                    if not claim or claim['body']['owner_epoch']!=job['owner_epoch'] or claim['body']['status'] not in {'running','draining'} or claim['body']['lease_until']<=tx.now:
                        raise StaleOwner('work claim changed before delivery publication')
                for lease in tx.scan('resource_leases'):
                    if lease['body']['scope']!=job['scope_ref']: continue
                    outstanding=[op for op in tx.scan('operations') if op['body']['scope']==job['scope_ref'] and op['body']['phase']!='terminal']
                    resources=[r for r in tx.scan('resources') if r['body']['scope']==job['scope_ref'] and r['body']['status']!='closed']
                    if manifest.result.cleanup_status in {'complete','not_needed'} and not outstanding and not resources:
                        tx.delete('resource_leases',lease['key'])
                        from .leases import LeaseService
                        LeaseService.announce_release(tx,[lease['key']],job['scope_ref'],max(1,job.get('owner_epoch',1)))
                    else:
                        tx.put('resource_leases',lease['key'],lease['body']|{'status':'quarantine','reason':'delivery cleanup remains unverified'})
                key=f'{manifest.delivery_id}:{manifest.version}'
                tx.put('work_deliveries',key,manifest.model_dump(mode='json'),immutable=True)
                tx.put('work_delivery_owners',key,{'run_id':job['run_id'],'group_id':group,'work_id':job['scope_ref']},immutable=True)
        return {'execution_status':execution,'graph_result':result,'activation_id':job['activation_id']}

class ResumeJobHandler:
    def __init__(self,repository,graph_handlers):
        self.repository=repository
        self.graph_handlers=dict(graph_handlers)
    async def __call__(self,job):
        identity=job['payload'].get('activation_id')
        with self.repository.transaction() as tx:
            source=tx.get('activations',identity) if identity else None
            metadata=tx.get('activation_jobs',identity) if identity else None
            if not source or not metadata or source['body']['record']['run_id']!=job['run_id']:
                raise Conflict('resume must reference original activation in this run')
            original=tx.get('jobs',metadata['body']['job_id'])
            if not original: raise Conflict('original job ledger is missing')
            handler=self.graph_handlers.get(Worker._job_kind(metadata['body']['job_kind']))
            if not isinstance(handler,GraphJobHandler):
                return {'execution_status':'blocked','error':{'code':'ADAPTER_MISSING','reason':'original graph version handler unavailable'}}
        driver=Driver(self.repository,{},handler.saver_factory,handler.graph_builder,handler.owner,handler_factory=handler.handler_factory,agent_allowlists=handler.agent_allowlists)
        result=await driver.resume(identity,decision=job['payload'].get('decision'))
        finalized=await handler.finalize(original['body'],result)
        return finalized|{'resumed_job_id':original['key']}

class Worker:
    def __init__(self,repository,handlers,*,policy:WorkerPolicy,owner='worker'):
        self.repository=repository
        self.handlers=dict(handlers)
        self.policy=policy
        self.owner=owner
        self.tasks=TaskBackend(repository)

    @staticmethod
    def _job_kind(kind):
        return {'graph:all':'run','graph:check':'run','graph:analysis':'analysis','graph:report':'report'}.get(kind,kind)

    def _notify_capacity_waiters(self,tx,job):
        """一个 work 终结会释放全局工作槽位；唤醒卡在 `concurrency_budget` 上的其他组。

        否则这些组已 park，且容量释放不会产生它们 aggregate 上的事件，会永久死锁。
        """
        run_id=job['run_id']
        groups={}
        for row in tx.scan('work_instances'):
            body=row['body']
            if (body.get('run_id')==run_id and body['status']=='pending'
                    and body.get('queue_reason')=='concurrency_budget'):
                groups[body['group_id']]=groups.get(body['group_id'],0)+1
        for group,waiting in sorted(groups.items()):
            identity=f'capacity:{job["job_id"]}:{group}'
            if tx.get('event_ids',identity): continue
            aggregate=f'group:{run_id}:{group}'
            seq=max((r['body']['aggregate_seq'] for r in tx.scan('events')
                     if r['body']['aggregate_id']==aggregate),default=0)+1
            EventService.append_in(tx,EventEnvelope(event_id=identity,producer_scope='resource-scheduler',
                producer_epoch=max(1,job.get('owner_epoch',1)),aggregate_id=aggregate,aggregate_seq=seq,
                run_ref=run_id,plan_ref=job['payload'].get('plan_ref',''),
                graph_ref=job['payload'].get('graph_ref',''),
                kind='work_capacity_available',payload={'waiting_works':waiting}))

    def _publish_completion(self,tx,body):
        jobrow=tx.get('jobs',body['job_id'])
        if not jobrow: raise Conflict('completion references missing job')
        job=jobrow['body']
        kind=self._job_kind(job['job_kind'])
        if kind=='resume' and job.get('result',{}).get('resumed_job_id'):
            original=tx.get('jobs',job['result']['resumed_job_id'])
            if not original: raise Conflict('resumed job source disappeared')
            job=original['body']|{'job_id':job['job_id'],'status':job['status'],'result':job['result'],'owner_epoch':job['owner_epoch']}
            kind=self._job_kind(job['job_kind'])
        if kind not in {'group','work','analysis','report','cleanup','reconcile'}: return
        rework_ref=job['payload'].get('source_rework_ref')
        if rework_ref:
            execution=tx.get('rework_execution',rework_ref)
            if not execution or execution['body'].get('job_ref')!=job['job_id']: raise Conflict('rework completion does not bind dispatched job')
            tx.put('rework_results',rework_ref,{'job_ref':job['job_id'],'status':job['status'],'result_ref':f'job:{job["job_id"]}'},immutable=True)
        identity=f'completion:{job["job_id"]}'
        if tx.get('event_ids',identity): return
        if kind=='work':
            work=tx.get('work_instances',job['scope_ref'])
            group=work['body']['group_id'] if work else job['payload'].get('group_id',job['scope_ref'])
            aggregate=f'group:{job["run_id"]}:{group}'
            if work:
                status='delivered' if job['status']=='complete' else job['status']
                tx.put('work_instances',job['scope_ref'],work['body']|{'status':status,'result_ref':f'job:{job["job_id"]}'})
        else:
            aggregate=f'run:{job["run_id"]}'
        if kind=='work':
            self._notify_capacity_waiters(tx,job)
        events=[r for r in tx.scan('events') if r['body']['aggregate_id']==aggregate]
        seq=max((r['body']['aggregate_seq'] for r in events),default=0)+1
        event=EventEnvelope(event_id=identity,producer_scope=job['scope_ref'],producer_epoch=job['owner_epoch'],aggregate_id=aggregate,aggregate_seq=seq,run_ref=job['run_id'],plan_ref=job['payload'].get('plan_ref',''),graph_ref=job['payload'].get('graph_ref',''),kind=f'{kind}_completed',payload={'job_id':job['job_id'],'status':job['status'],'result_ref':f'job:{job["job_id"]}'})
        EventService.append_in(tx,event)

    def _enqueue_activation(self,tx,run_id,*,dedupe,payload,thread_id=None,scope=None,kind='run',deadline=None):
        if tx.get('job_dedupe',dedupe): return
        run=tx.get('runs',run_id)
        if not run: raise Conflict('outbox references missing run')
        body=run['body']
        TaskBackend.enqueue_in(tx,JobSpec(job_id=str(uuid4()),job_kind=kind,scope_ref=scope or run_id,thread_id=thread_id or body['root_thread_id'],activation_id=str(uuid4()),dedupe_key=dedupe,due_at=tx.now,deadline=deadline or tx.now+self.policy.job_seconds,run_id=run_id,dispatch_generation=body['dispatch_generation'],payload={'plan_ref':body['active_plan_ref'],'graph_ref':body['graph_ref']}|payload))

    def _wake_waits(self,tx):
        for row in tx.scan('waits'):
            body=row['body'];wait=body['record']
            if body['status'] not in {'waiting','ready'}: continue
            if wait['deadline']<=tx.now: continue
            aggregate=wait.get('aggregate_id')
            event=tx.get('events',f'{aggregate}:{wait["cursor"]+1:020}') if aggregate else None
            if event is None: continue
            # Resolve the frozen parent activation rather than reinterpreting its
            # input with a successor plan or graph version.
            parent_row=tx.get('activations',wait['activation_id']) if wait.get('activation_id') else None
            if not parent_row or any(parent_row['body']['record'][key]!=wait[key] for key in ('thread_id','plan_ref','graph_ref')):
                tx.put('outbox_errors',f'wait:{row["key"]}',{'reason':'parent activation not found','wait_id':row['key']})
                continue
            parent=parent_row['body']['record']
            metadata=tx.get('activation_jobs',parent['activation_id'])
            kind=metadata['body']['job_kind'] if metadata else 'run' if aggregate.startswith('run:') else 'group'
            seq=event['body']['aggregate_seq']
            dedupe=f'wake:{row["key"]}:{wait["version"]}'
            self._enqueue_activation(tx,parent['run_id'],dedupe=dedupe,payload=parent['input_refs']|{'plan_ref':parent['plan_ref'],'graph_ref':parent['graph_ref'],'event_cursor':str(seq),'continuation_of':parent['activation_id']},thread_id=wait['thread_id'],scope=wait['scope'],kind=kind,deadline=wait['deadline'])
            tx.put('waits',row['key'],body|{'status':'resumed','event_seq':seq})

    def _notify_resource_waiters(self,tx,identity,body):
        from ..planning.compiler import Plan
        targets={}
        # Include registered groups before they park: release -> registration
        # must observe the same durable event as registration -> release.
        for row in tx.scan('groups'):
            group=row['body'];run=tx.get('runs',group['run_id'])
            if run:
                targets[(group['run_id'],group['group_id'],group['plan_ref'])]=run['body']['graph_ref']
        for row in tx.scan('waits'):
            wait=row['body']['record'];aggregate=wait.get('aggregate_id')
            if row['body']['status'] not in {'waiting','ready'} or not aggregate or not aggregate.startswith('group:'): continue
            parent=tx.get('activations',wait['activation_id'])
            if parent: targets[(parent['body']['record']['run_id'],wait['scope'],wait['plan_ref'])]=wait['graph_ref']
        for (run_id,scope,plan_ref),graph_ref in targets.items():
            frozen=tx.get('plans',plan_ref)
            if not frozen: continue
            domains={domain for case in Plan.model_validate(frozen['body']).cases if case.group_id==scope for domain in case.conflict_domains}
            relevant=domains&set(body['domains'])
            if not relevant: continue
            aggregate=f'group:{run_id}:{scope}';event_id=f'{identity}:{aggregate}'
            if tx.get('event_ids',event_id): continue
            seq=max((r['body']['aggregate_seq'] for r in tx.scan('events') if r['body']['aggregate_id']==aggregate),default=0)+1
            EventService.append_in(tx,EventEnvelope(event_id=event_id,producer_scope='resource-scheduler',producer_epoch=body['owner_epoch'],aggregate_id=aggregate,aggregate_seq=seq,run_ref=run_id,plan_ref=plan_ref,graph_ref=graph_ref,kind='resource_available',payload={'domains':sorted(relevant)}))

    def pump_outbox(self):
        processed=0
        with self.repository.transaction() as tx:
            for row in tx.scan('outbox'):
                if processed>=self.policy.max_outbox_per_tick: break
                if tx.get('outbox_delivery',row['key']): continue
                body=row['body'];kind=body.get('kind')
                try:
                    if kind=='job_complete': self._publish_completion(tx,body)
                    elif kind=='resource_domains_available': self._notify_resource_waiters(tx,row['key'],body)
                    elif kind=='activate':
                        self._enqueue_activation(tx,body['run_id'],dedupe=f'activation-outbox:{row["key"]}',payload={'plan_ref':body['plan_ref']})
                    elif kind=='delivery_reviewed':
                        for candidate in tx.scan('reuse_candidates'):
                            reuse=candidate['body']
                            if reuse.get('delivery_ref')!=body['delivery_ref'] or reuse['status'] not in {'waiting','accepted'}: continue
                            target=tx.get('work_instances',candidate['key'])
                            if not target or target['body']['status'] not in {'pending','blocked'}: continue
                            work=target['body'];aggregate=f'group:{work["run_id"]}:{work["group_id"]}'
                            identity=f'reuse-source:{row["key"]}:{candidate["key"]}'
                            if not tx.get('event_ids',identity):
                                seq=max((e['body']['aggregate_seq'] for e in tx.scan('events') if e['body']['aggregate_id']==aggregate),default=0)+1
                                EventService.append_in(tx,EventEnvelope(event_id=identity,producer_scope='reuse-review',producer_epoch=row['revision']+1,aggregate_id=aggregate,aggregate_seq=seq,run_ref=work['run_id'],plan_ref=work['plan_ref'],graph_ref=work['graph_ref'],kind='reuse_source_reviewed',payload={'work_id':candidate['key'],'delivery_ref':body['delivery_ref']}))
                        for run in tx.scan('runs'):
                            if run['body']['active_plan_ref']==body['plan_ref']:
                                self._enqueue_activation(tx,run['key'],dedupe=f'review-outbox:{row["key"]}:{run["key"]}',payload={})
                    elif kind=='resource_disposition':
                        operations=[r for r in tx.scan('operations') if r['body']['scope']==body['scope']]
                        for op in operations:
                            self._enqueue_activation(tx,op['body']['run_id'],dedupe=f'dispose:{row["key"]}:{op["key"]}',payload={'operation_id':op['key'],'policy':body['policy']},scope=body['scope'],thread_id=f'recovery:{op["key"]}',kind='cleanup' if body['policy']=='cleanup' else 'reconcile')
                        for lease in tx.scan('resource_leases'):
                            if lease['body']['scope']==body['scope']:
                                tx.put('resource_leases',lease['key'],lease['body']|{'status':'quarantine','reason':'wait deadline expired'})
                    elif kind=='agent_supervision':
                        run=tx.get('runs',body['run_ref'])
                        identity=str(uuid4())
                        TaskBackend.enqueue_in(tx,JobSpec(job_id=identity,job_kind='supervision',
                            scope_ref=body['owner_agent_id'],
                            thread_id=f'agent:{canonical_hash(body["owner_agent_id"])}',
                            activation_id=str(uuid4()),dedupe_key=f'supervision:{row["key"]}',due_at=tx.now,
                            deadline=tx.now+self.policy.job_seconds,run_id=body['run_ref'],
                            dispatch_generation=run['body']['dispatch_generation'] if run else 1,
                            payload={'owner_agent_id':body['owner_agent_id']}))
                    elif kind in {'job_ready','node_result'} or 'aggregate_id' in body or row['key'].startswith('wakeup:'):
                        pass
                    else:
                        tx.put('outbox_errors',row['key'],{'reason':'unregistered outbox kind','kind':kind})
                        continue
                    tx.put('outbox_delivery',row['key'],{'status':'delivered'},immutable=True)
                    processed+=1
                except (Conflict,KeyError) as exc:
                    tx.put('outbox_errors',row['key'],{'reason':type(exc).__name__})
            self._wake_waits(tx)
        return processed

    async def run_once(self):
        from .waits import WaitService
        WaitService(self.repository).watchdog()
        self.pump_outbox()
        job=self.tasks.claim(self.owner,self.policy.claim_seconds)
        if job is None: return False
        handler=self.handlers.get(self._job_kind(job['job_kind']))
        if handler is None:
            result={'execution_status':'blocked','error':{'code':'ADAPTER_MISSING','job_kind':job['job_kind']}}
            self.tasks.finish(job['job_id'],owner_epoch=job['owner_epoch'],result=result,status='blocked')
            return True
        async def heartbeat():
            while True:
                await asyncio.sleep(self.policy.claim_seconds/3)
                self.tasks.renew_claim(job['job_id'],owner_epoch=job['owner_epoch'],ttl=self.policy.claim_seconds)
        pulse=asyncio.create_task(heartbeat())
        execution=asyncio.create_task(handler(job))
        try:
            with self.repository.transaction() as tx: remaining=max(0,job['deadline']-tx.now)
            done,_=await asyncio.wait({pulse,execution},timeout=min(remaining,self.policy.job_seconds),return_when=asyncio.FIRST_COMPLETED)
            if pulse in done: pulse.result()
            if execution not in done: raise TimeoutError('worker job segment deadline exceeded')
            result=execution.result()
            if not isinstance(result,dict) or result.get('execution_status') not in {'succeeded','blocked','failed','cancelled'}:
                raise ValueError('job handler must return explicit execution_status')
            status={'succeeded':'complete','blocked':'blocked','failed':'failed','cancelled':'cancelled'}[result['execution_status']]
            self.tasks.finish(job['job_id'],owner_epoch=job['owner_epoch'],result=result,status=status)
        except StaleOwner:
            raise
        except Exception as exc:
            self.tasks.finish(job['job_id'],owner_epoch=job['owner_epoch'],result={'execution_status':'failed','error':{'code':'EXECUTION_FAILED','type':type(exc).__name__}},status='failed')
        finally:
            if not execution.done():
                execution.cancel()
                with suppress(asyncio.CancelledError): await execution
            pulse.cancel()
            with suppress(asyncio.CancelledError): await pulse
        return True

    async def serve(self,stop_event):
        while not stop_event.is_set():
            if not await self.run_once():
                try: await asyncio.wait_for(stop_event.wait(),timeout=self.policy.poll_seconds)
                except TimeoutError: pass

class SupervisionJobHandler:
    """WF-04：owner agent 消费看护记录（有模型出提案，无模型显式记缺口），不轮询。"""
    def __init__(self,supervisor,*,allowed_fields=(),budget=None,accessible_evidence=(),fields_by_node=None,remediation=None):
        self.supervisor=supervisor;self.allowed_fields=set(allowed_fields);self.budget=budget
        self.accessible_evidence=set(accessible_evidence);self.fields_by_node=fields_by_node
        self.remediation=remediation
    async def __call__(self,job):
        owner=job['payload']['owner_agent_id']
        if self.budget is None:
            return {'execution_status':'blocked','error':{'code':'CONTRACT_MISSING','reason':'agent context budget required'}}
        records=self.supervisor.pending(owner)
        decisions=await self.supervisor.review(owner,allowed_fields=self.allowed_fields,
            accessible_evidence=self.accessible_evidence,budget=self.budget,fields_by_node=self.fields_by_node)
        # 循环工程：agent 可据提案发起**有界**修复（新 attempt 重执行失败节点；不改证据）
        remedies=[]
        if self.remediation is not None:
            for (key,record),decision in zip(records,decisions):
                remedies.append(self.remediation.order(owner_agent_id=owner,work_ref=record['scope'],
                    node_id=record['node_id'],run_ref=record['run_ref'],plan_ref=record['plan_ref'],
                    graph_ref=record['graph_ref'],decision=decision,deadline=job['deadline']))
        return {'execution_status':'succeeded','decisions':decisions,'remedies':remedies}


class RemedyJobHandler:
    """有界失败循环的执行端：以**新 attempt/新 activation** 重执行失败节点。

    旧 receipt/证据不可变；重执行只会产生新 receipt。预算由 NodeRemediationService 把守。
    """
    def __init__(self,repository,node_service_factory,*,lease_seconds,segment_seconds,owner='remedy'):
        self.repository=repository;self.node_service_factory=node_service_factory
        self.lease_seconds=lease_seconds;self.segment_seconds=segment_seconds;self.owner=owner
    async def __call__(self,job):
        from ..persistence.repository import LeaseToken
        from ..graphs.executor import NodeExecutor
        from .driver import Activation
        ref=job['payload']['remedy_ref']
        with self.repository.transaction() as tx:
            row=tx.get('remedy_orders',ref)
            if not row: return {'execution_status':'blocked','error':{'code':'INPUT_MISSING','reason':'remedy order missing'}}
            order=row['body']
        token=LeaseToken(job['thread_id'],job['owner_epoch'],self.owner,job['deadline'])
        activation=Activation(activation_id=job['activation_id'],run_id=order['run_ref'],thread_id=job['thread_id'],
            scope=order['work_ref'],plan_ref=order['plan_ref'],graph_ref=order['graph_ref'],
            input_refs=order.get('input_refs',{}),lease_seconds=self.lease_seconds,segment_seconds=self.segment_seconds)
        handlers=self.node_service_factory(token,activation)
        executor=NodeExecutor(self.repository,handlers,token)
        state={'thread_id':job['thread_id'],'scope':order['work_ref'],'run_ref':order['run_ref'],
               'plan_ref':order['plan_ref'],'graph_ref':order['graph_ref'],'activation_id':job['activation_id'],
               'input_refs':order.get('input_refs',{}),'result_refs':{},
               'node_visits':{order['node_id']:order['attempt']}}   # 新 visit => 新 receipt
        try:
            result,receipt=await executor.execute(order['node_id'],state)
            status=result.execution_status
        except StaleOwner:
            raise
        except Exception:
            status='failed';receipt=None
        with self.repository.transaction() as tx:
            tx.put('agent_remedies',f'{ref}:outcome',{'status':status,'attempt':order['attempt'],
                   'action_ref':order['action_ref'],'receipt_ref':receipt},immutable=True)
        return {'execution_status':status,'output':{'remedy_ref':ref,'receipt_ref':receipt,'attempt':order['attempt']}}


class RecoveryJobHandler:
    """Finite reconcile/cancel/cleanup entrypoint; unknown effects stay isolated."""
    def __init__(self,repository,operation_service,*,lease_seconds,owner='recovery'):
        self.repository=repository
        self.operations=operation_service
        self.lease_seconds=lease_seconds
        self.owner=owner
    async def __call__(self,job):
        from .leases import LeaseService
        token=LeaseService(self.repository).acquire_thread(job['thread_id'],self.owner,self.lease_seconds)
        scope=job['scope_ref']
        try:
            if scope==job['run_id'] and job['job_kind']=='cleanup':
                with self.repository.transaction() as tx:
                    tx.assert_fence(token)
                    run=tx.get('runs',job['run_id'])['body']
                    scopes=sorted({r['body']['scope'] for r in tx.scan('operations') if r['body']['run_id']==job['run_id']})
                    children=[]
                    for owned_scope in scopes:
                        dedupe=f'run-cleanup:{job["activation_id"]}:{owned_scope}'
                        existing=tx.get('job_dedupe',dedupe)
                        if existing:
                            children.append(existing['body']['job_id'])
                            continue
                        identity=str(uuid4())
                        TaskBackend.enqueue_in(tx,JobSpec(job_id=identity,job_kind='cleanup',scope_ref=owned_scope,thread_id=f'cleanup:{job["run_id"]}:{owned_scope}',activation_id=str(uuid4()),dedupe_key=dedupe,due_at=tx.now,deadline=job['deadline'],run_id=job['run_id'],dispatch_generation=run['dispatch_generation'],payload={'plan_ref':run['active_plan_ref'],'graph_ref':run['graph_ref'],'policy':'cleanup'}))
                        children.append(identity)
                return {'execution_status':'succeeded','cleanup_status':'pending' if children else 'not_needed','cleanup_jobs':children}
            with self.repository.transaction() as tx:
                records=[r for r in tx.scan('operations') if r['body']['scope']==scope and r['body']['run_id']==job['run_id']]
                requested=job['payload'].get('operation_id')
                if requested: records=[r for r in records if r['key']==requested]
            if not records:
                return {'execution_status':'blocked','error':{'code':'INPUT_MISSING','reason':'no owned operation inventory'}}
            unknown=[]
            statuses={}
            for record in records:
                identity=record['key']
                status=await self.operations.reconcile(identity,scope,token)
                if job['job_kind']=='cleanup' and status.state=='running':
                    status=await self.operations.cancel(identity,scope,'authorized recovery cleanup',token)
                statuses[identity]=status.state
                if status.state=='unknown' or not status.authoritative or status.state=='running':
                    unknown.append(identity)
            if unknown:
                with self.repository.transaction() as tx:
                    tx.assert_fence(token)
                    for lease in tx.scan('resource_leases'):
                        if lease['body']['scope']==scope:
                            tx.put('resource_leases',lease['key'],lease['body']|{'status':'quarantine','reason':'recovery cannot prove effects closed'})
                return {'execution_status':'blocked','operation_status':statuses,'cleanup_status':'unknown','unknown_operations':unknown}
            if job['job_kind']!='cleanup':
                return {'execution_status':'succeeded','operation_status':statuses,'cleanup_status':'pending'}
            with self.repository.transaction() as tx:
                resources=tuple(r['key'] for r in tx.scan('resources') if r['body']['scope']==scope and r['body']['status']!='closed')
            if resources:
                cleanup=await self.operations.cleanup(scope,resources,token)
                if cleanup.status!='complete':
                    return {'execution_status':'blocked','operation_status':statuses,'cleanup_status':cleanup.status,'remaining':list(cleanup.remaining)}
            with self.repository.transaction() as tx:
                tx.assert_fence(token)
                for lease in tx.scan('resource_leases'):
                    if lease['body']['scope']==scope:
                        tx.delete('resource_leases',lease['key'])
                        LeaseService.announce_release(tx,[lease['key']],scope,token.owner_epoch)
            return {'execution_status':'succeeded','operation_status':statuses,'cleanup_status':'complete'}
        finally:
            with suppress(StaleOwner):
                with self.repository.transaction() as tx: tx.release_thread(token)
