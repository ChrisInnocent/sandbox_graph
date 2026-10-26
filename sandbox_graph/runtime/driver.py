"""Bounded graph activations, immutable versions and resumable human interrupts."""
import asyncio
from contextlib import suppress
from pydantic import Field
from langgraph.types import Command
from ..domain.models import Model, canonical_hash
from ..graphs.executor import NodeExecutor
from ..persistence.repository import Conflict,StaleOwner,positive_duration
from ..services.lifecycle_safety import LifecycleSafetyService
from .observation import ObservationService

class Activation(Model):
    activation_id: str
    run_id: str
    thread_id: str
    scope: str
    plan_ref: str
    graph_ref: str
    input_refs: dict[str,str]
    lease_seconds: float = Field(gt=0)
    segment_seconds: float = Field(gt=0)
    recursion_limit: int = Field(default=100,gt=0)
    continuation_of: str | None = None
    recovery_plan_ref: str | None = None

class Driver:
    def __init__(self,repository,handlers,saver_factory,graph_builder,owner='worker',*,handler_factory=None,agent_allowlists=None,repair_catalog=None):
        self.repository=repository
        self.handlers=handlers
        self.saver_factory=saver_factory
        self.graph_builder=graph_builder
        self.owner=owner
        self.handler_factory=handler_factory
        self.repair_catalog=repair_catalog
        self.agent_allowlists=dict(agent_allowlists or {})

    @staticmethod
    def _validate_decision(decision,interrupts):
        matching=[item for item in interrupts if item['id']==decision.get('interrupt_id')]
        if len(matching)!=1 or not decision.get('actor'):
            raise Conflict('STALE_DECISION: unknown interrupt or actor')
        question=matching[0]['value']
        for key in ('question_id','scope','plan_ref','graph_ref','wait_version'):
            if key in question and decision.get(key)!=question[key]:
                raise Conflict(f'STALE_DECISION: {key} differs')
        if 'options' in question and decision.get('answer') not in question['options']:
            raise Conflict('STALE_DECISION: answer not in authorized options')

    async def start(self,activation:Activation,*,decision=None):
        if "resume_from" in activation.input_refs:
            raise Conflict("raw resume_from is forbidden; compiled RecoveryPlan required")
        positive_duration(activation.lease_seconds)
        positive_duration(activation.segment_seconds)
        digest=canonical_hash(activation)
        with self.repository.transaction() as tx:
            run=tx.get('runs',activation.run_id)
            if not run or run['body']['graph_ref']!=activation.graph_ref:
                raise Conflict('graph version mismatch; restore using original definition')
            from ..services.measurement_generation import MeasurementGenerationService
            previous=tx.get('activations',activation.activation_id)
            binding=tx.get('run_generation_bindings',activation.run_id)
            if previous and binding:
                # An existing exact activation may drain after revocation; every
                # business node still checks live authority before its handler.
                generation_plan=MeasurementGenerationService._load_in(tx,binding['body']['plan_ref'])
            else:
                generation_plan=MeasurementGenerationService.validate_run_in(tx,activation.run_id,plan_ref=activation.plan_ref)
            if previous:
                record=previous['body']
                if record['hash']!=digest: raise Conflict('activation input is immutable')
                if record['status']=='complete':
                    if decision is not None and record.get('decision')!=decision:
                        raise Conflict('decision changed after activation completed')
                    return record['result']
                if record['status']=='waiting' and decision is None:
                    return record['result']
                if decision is not None:
                    self._validate_decision(decision,record.get('interrupts',[]))
                    tx.put('decisions',f'{activation.activation_id}:{decision["interrupt_id"]}',decision,immutable=True)
                    record=record|{'decision':decision,'status':'resuming'}
                    tx.put('activations',activation.activation_id,record)
                elif record.get('decision') is not None:
                    decision=record['decision']
            else:
                if decision is not None: raise Conflict('cannot answer an activation that never interrupted')
                if run['body']['active_plan_ref']!=activation.plan_ref:
                    source=tx.get('activations',activation.continuation_of) if activation.continuation_of else None
                    if not source or any(source['body']['record'][k]!=getattr(activation,k) for k in ('run_id','thread_id','plan_ref','graph_ref')):
                        raise Conflict('activation plan is stale')
                record={'hash':digest,'record':activation.model_dump(mode='json'),'status':'pending'}
                tx.put('activations',activation.activation_id,record,immutable=True)
            token=tx.acquire_thread(activation.thread_id,self.owner,activation.lease_seconds)
            work=tx.get('work_instances',activation.scope)
            owns_work=work is not None
            if owns_work:
                body=work['body']
                if generation_plan: MeasurementGenerationService.validate_work_in(tx,generation_plan,body)
                if any(body.get(key)!=value for key,value in {
                        'run_id':activation.run_id,'plan_ref':activation.plan_ref,
                        'graph_ref':activation.graph_ref}.items()):
                    raise Conflict('activation differs from frozen work identity')
                LifecycleSafetyService.acquire_work_in(tx,activation.scope,token,
                    purpose="recovery" if activation.recovery_plan_ref else "execution")
                if activation.recovery_plan_ref:
                    from ..services.recovery import RecoveryService
                    RecoveryService.revalidate_in(tx, activation.recovery_plan_ref,
                        scope=activation.scope, run_ref=activation.run_id, plan_ref=activation.plan_ref,
                        graph_ref=activation.graph_ref, input_refs=activation.input_refs,
                        activation_id=activation.activation_id, token=token)
                # A prior attempt's closed receipt is not permission for this one.
                tx.put('work_safety_facts',activation.scope,{
                    **LifecycleSafetyService._identity_in(tx,activation.scope),
                    'schema_version':1,'producer':'work_runtime','producer_version':'1',
                    'activation_id':activation.activation_id,'stopped':False,
                    'admission_status':'open','sampler_status':'unknown','attempt_closed':False,
                    'receipt_refs':{}})
            elif activation.recovery_plan_ref:
                raise Conflict("recovery requires a registered frozen work")
        heartbeat_task=None
        execution=None
        try:
            saver=self.saver_factory(token)
            if saver is None: raise ValueError('persistent saver is required; no implicit memory fallback')
            handlers=self.handler_factory(token,activation) if self.handler_factory else self.handlers
            def work_fence(tx):
                if owns_work:
                    LifecycleSafetyService.assert_work_in(tx,activation.scope,token)
            executor=NodeExecutor(self.repository,handlers,token,allowlists=self.agent_allowlists,
                                  additional_fence=work_fence)
            graph=self.graph_builder(executor,checkpointer=saver)
            config={'configurable':{'thread_id':activation.thread_id,'checkpoint_ns':''},'recursion_limit':activation.recursion_limit}
            state={'thread_id':activation.thread_id,'scope':activation.scope,'run_ref':activation.run_id,'plan_ref':activation.plan_ref,'graph_ref':activation.graph_ref,'activation_id':activation.activation_id,'input_refs':dict(activation.input_refs),'result_refs':{},'results':{},'node_visits':{}}
            state.update(recovery_plan_ref=activation.recovery_plan_ref or '', recovery_route=[],
                         recovery_acceptance={}, recovery_dependency_failures={}, measurement_blocked=False)
            async def heartbeat():
                while True:
                    await asyncio.sleep(activation.lease_seconds/3)
                    with self.repository.transaction() as tx:
                        tx.renew_thread(token,activation.lease_seconds)
            heartbeat_task=asyncio.create_task(heartbeat())
            saved=await saver.aget_tuple(config)
            if saved:
                existing=saved.checkpoint.get('channel_values',{})
                if existing.get('graph_ref',activation.graph_ref)!=activation.graph_ref:
                    raise Conflict('checkpoint graph mismatch')
                if existing.get('activation_id')==activation.activation_id:
                    state=None
            if decision is not None:
                snapshot=await graph.aget_state(config)
                actual=[{'id':item.id,'value':item.value} for task in snapshot.tasks for item in task.interrupts]
                self._validate_decision(decision,actual)
                state=Command(resume={decision['interrupt_id']:decision})
            execution=asyncio.create_task(graph.ainvoke(state,config,durability='sync'))
            done,_=await asyncio.wait({execution,heartbeat_task},timeout=activation.segment_seconds,return_when=asyncio.FIRST_COMPLETED)
            if heartbeat_task in done:
                heartbeat_task.result()
            if execution not in done:
                raise TimeoutError('activation segment deadline exceeded; reconcile before resume')
            result=execution.result()
            interrupts=[{'id':item.id,'value':item.value} for item in result.get('__interrupt__',())]
            if interrupts:
                result=result|{'__interrupt__':interrupts}
            with self.repository.transaction() as tx:
                tx.assert_fence(token)
                work_fence(tx)
                old=tx.get('activations',activation.activation_id)
                if owns_work and not interrupts:
                    refs={n:result.get('result_refs',{}).get(n) for n in ('N07','N09','N10')}
                    # Verify actual receipts; an arbitrary graph result cannot assert closure.
                    closed=True
                    for node,ref in refs.items():
                        receipt=tx.get('receipts',ref) if ref else None
                        identity=receipt['body']['identity'] if receipt else {}
                        expected={'scope':activation.scope,'run_ref':activation.run_id,
                            'plan_ref':activation.plan_ref,'graph_ref':activation.graph_ref,
                            'activation_id':activation.activation_id,'node_id':node}
                        if (any(identity.get(k)!=v for k,v in expected.items()) or
                                receipt['body']['result']['execution_status'] not in
                                {'succeeded','failed','blocked','cancelled'}):
                            closed=False
                    observations=LifecycleSafetyService._rows_in(tx,'observations',activation.scope)
                    sampler=ObservationService.sampler_status_in(tx,observations)
                    tx.put('work_safety_facts',activation.scope,{
                        **LifecycleSafetyService._identity_in(tx,activation.scope),
                        'schema_version':1,'producer':'work_runtime','producer_version':'1',
                        'activation_id':activation.activation_id,'stopped':closed,
                        'admission_status':'closed','sampler_status':sampler,
                        'attempt_closed':closed,'receipt_refs':refs,
                        'attempt_receipt_ref':refs['N10']})
                    if activation.recovery_plan_ref:
                        from ..services.recovery import RecoveryService
                        acceptance = RecoveryService.finalize_acceptance_in(tx,
                            activation.recovery_plan_ref, result, token, catalog=self.repair_catalog)
                        result = result | {'recovery_acceptance':acceptance}
                tx.put('activations',activation.activation_id,old['body']|{'status':'waiting' if interrupts else 'complete','interrupts':interrupts,'result':result})
            return result
        finally:
            if execution is not None and not execution.done():
                execution.cancel()
                with suppress(asyncio.CancelledError): await execution
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError): await heartbeat_task
            # Only execution ownership ends; reservations and actual resources persist.
            with suppress(StaleOwner):
                with self.repository.transaction() as tx:
                    if owns_work:
                        LifecycleSafetyService.release_work_in(tx,activation.scope,token)
                    tx.release_thread(token)

    async def resume(self,activation_id:str,*,decision=None):
        with self.repository.transaction() as tx:
            row=tx.get('activations',activation_id)
            if not row: raise KeyError(activation_id)
            activation=Activation.model_validate(row['body']['record'])
        return await self.start(activation,decision=decision)
