"""Node-local contexts and receipt-first execution, independent of checkpoint commits."""
from dataclasses import dataclass
from uuid import uuid4
from ..domain.models import NodeResult, ErrorRecord, canonical_hash
from ..persistence.repository import Conflict
from ..runtime.agents import owner_agent_for
from ..runtime.node_agent import NodeAgent

@dataclass(frozen=True)
class NodeContext:
    node_id: str
    owner_agent_id: str
    thread_id: str
    scope: str
    run_ref: str
    plan_ref: str
    graph_ref: str
    activation_id: str
    visit_id: str
    input_refs: dict
    result_refs: dict
    owner_epoch: int
    lease_id: str

    @property
    def node_supervisor_id(self):
        return 'node-supervisor:' + canonical_hash({'run': self.run_ref,
            'owner': self.owner_agent_id, 'node': self.node_id})

class NodeExecutor:
    def __init__(self,repository,handlers,token,*,agents=None,allowlists=None,additional_fence=None):
        self.repository=repository; self.handlers=dict(handlers); self.token=token
        self.agents=dict(agents or {}); self.allowlists=dict(allowlists or {})
        self.additional_fence=additional_fence

    async def execute(self,node_id,state):
        identity={k:state[k] for k in ('thread_id','scope','run_ref','plan_ref','graph_ref','activation_id','input_refs')}
        identity['node_id']=node_id
        identity['visit_number']=state.get('node_visits',{}).get(node_id,0)
        digest=canonical_hash(identity)
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            if self.additional_fence is not None:
                self.additional_fence(tx)
            if state['thread_id']!=self.token.thread_id: raise PermissionError('node thread does not match driver lease')
            key=f'{state["thread_id"]}:{state["activation_id"]}:{node_id}:{identity["visit_number"]}'
            visit=tx.get('visits',key)
            if not visit:
                tx.put('visits',key,{'visit_id':str(uuid4()),'input_hash':digest},immutable=True)
                visit=tx.get('visits',key)
            if visit['body']['input_hash']!=digest: raise Conflict('replayed node input changed')
            visit_id=visit['body']['visit_id']
            receipt_key=f'{state["thread_id"]}:{node_id}:{visit_id}:{digest}'
            receipt=tx.get('receipts',receipt_key)
            if receipt: return NodeResult.model_validate(receipt['body']['result']),receipt_key
            from ..services.measurement_continuity import MeasurementContinuityService
            continuity_result = MeasurementContinuityService.guard_in(tx, node_id, state)
        context=NodeContext(node_id,owner_agent_for(node_id,state['scope']),state['thread_id'],state['scope'],state['run_ref'],state['plan_ref'],state['graph_ref'],state['activation_id'],visit_id,dict(state['input_refs']),dict(state.get('result_refs',{})),self.token.owner_epoch,self.token.lease_id)
        handler=self.handlers.get(node_id)
        review=None
        if continuity_result is not None:
            result=continuity_result
        elif handler is None:
            result=NodeResult(execution_status='blocked',error=ErrorRecord(code='ADAPTER_MISSING',observed_at=0,message=f'handler {node_id} not configured'),coverage_status='blocked')
        else:
            agent=self.agents.get(node_id) or NodeAgent(node_id,context.owner_agent_id,handler,
                allowed_input_keys=self.allowlists.get(node_id))
            try:
                result,review=await agent.run(context)
            except Exception as exc:
                # The operation service retains uncertain external state; failure still routes to cleanup.
                result=NodeResult(execution_status='failed',error=ErrorRecord(code='EXECUTION_FAILED',effect_certainty='unknown',observed_at=0,message=type(exc).__name__),cleanup_status='unknown')
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            if self.additional_fence is not None:
                self.additional_fence(tx)
            result=MeasurementContinuityService.qualify_in(tx,node_id,state,result)
            tx.put('receipts',receipt_key,{'identity':identity,'visit_id':visit_id,'result':result.model_dump(mode='json')},immutable=True)
            tx.put('outbox',f'receipt:{receipt_key}',{'kind':'node_result','scope':state['scope'],'node_id':node_id,'receipt_ref':receipt_key},immutable=True)
            # 节点 agent 复审：只记录 findings/decision，不改写上面的执行事实。
            if review is not None:
                tx.put('node_reviews',receipt_key,review.model_dump(mode='json'),immutable=True)
            # WF-04 看护以事件/账本驱动，不做模型轮询：非成功节点写一条待 owner agent 复核的记录。
            issue_sources = []
            if result.execution_status in {'failed', 'blocked'}:
                issue_sources.append('execution')
            if result.review_status == 'rejected':
                issue_sources.append('result_review')
            if review is not None and review.decision == 'reject':
                issue_sources.append('node_review')
            work=tx.get('work_instances',state['scope'])
            linked=work and work['body'].get('source_lifecycle_ref')
            if issue_sources and linked:
                tx.put('generation_failure_links',receipt_key,{'source_lifecycle_ref':linked,
                    'source_work_ref':work['body']['source_work_ref'],'work_ref':state['scope'],
                    'run_ref':state['run_ref'],'plan_ref':state['plan_ref'],'node_id':node_id,
                    'receipt_ref':receipt_key,'status':'blocked_needs_decision'},immutable=True)
            if issue_sources and not linked:
                tx.put('agent_supervision',receipt_key,{'owner_agent_id':context.owner_agent_id,'node_id':node_id,
                    'scope':state['scope'],'run_ref':state['run_ref'],'plan_ref':state['plan_ref'],
                    'graph_ref':state['graph_ref'],'execution_status':result.execution_status,
                    'receipt_ref':receipt_key,'reason':(result.error.code if result.error else None),
                    'issue_sources':issue_sources,'node_supervisor_id':context.node_supervisor_id,
                    'input_refs':dict(context.input_refs),'result_refs':dict(context.result_refs)},immutable=True)
                tx.put('outbox',f'agent-supervision:{receipt_key}',{'kind':'agent_supervision',
                    'owner_agent_id':context.owner_agent_id,'scope':state['scope'],'run_ref':state['run_ref'],
                    'node_id':node_id,'receipt_ref':receipt_key},immutable=True)
        return result,receipt_key
