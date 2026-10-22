"""Bounded graph activations, immutable versions and resumable human interrupts."""
import asyncio
from contextlib import suppress
from pydantic import Field
from langgraph.types import Command
from ..domain.models import Model, canonical_hash
from ..graphs.executor import NodeExecutor
from ..persistence.repository import Conflict,StaleOwner,positive_duration

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

class Driver:
    def __init__(self,repository,handlers,saver_factory,graph_builder,owner='worker',*,handler_factory=None,agent_allowlists=None):
        self.repository=repository
        self.handlers=handlers
        self.saver_factory=saver_factory
        self.graph_builder=graph_builder
        self.owner=owner
        self.handler_factory=handler_factory
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
        positive_duration(activation.lease_seconds)
        positive_duration(activation.segment_seconds)
        digest=canonical_hash(activation)
        with self.repository.transaction() as tx:
            run=tx.get('runs',activation.run_id)
            if not run or run['body']['graph_ref']!=activation.graph_ref:
                raise Conflict('graph version mismatch; restore using original definition')
            previous=tx.get('activations',activation.activation_id)
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
        heartbeat_task=None
        execution=None
        try:
            saver=self.saver_factory(token)
            if saver is None: raise ValueError('persistent saver is required; no implicit memory fallback')
            handlers=self.handler_factory(token,activation) if self.handler_factory else self.handlers
            executor=NodeExecutor(self.repository,handlers,token,allowlists=self.agent_allowlists)
            graph=self.graph_builder(executor,checkpointer=saver)
            config={'configurable':{'thread_id':activation.thread_id,'checkpoint_ns':''},'recursion_limit':activation.recursion_limit}
            state={'thread_id':activation.thread_id,'scope':activation.scope,'run_ref':activation.run_id,'plan_ref':activation.plan_ref,'graph_ref':activation.graph_ref,'activation_id':activation.activation_id,'input_refs':dict(activation.input_refs),'result_refs':{},'results':{},'node_visits':{}}
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
                old=tx.get('activations',activation.activation_id)
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
                with self.repository.transaction() as tx: tx.release_thread(token)

    async def resume(self,activation_id:str,*,decision=None):
        with self.repository.transaction() as tx:
            row=tx.get('activations',activation_id)
            if not row: raise KeyError(activation_id)
            activation=Activation.model_validate(row['body']['record'])
        return await self.start(activation,decision=decision)
