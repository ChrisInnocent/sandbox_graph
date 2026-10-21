"""Node-local contexts and receipt-first execution, independent of checkpoint commits."""
from dataclasses import dataclass
from uuid import uuid4
from ..domain.models import NodeResult, ErrorRecord, canonical_hash
from ..persistence.repository import Conflict

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

class NodeExecutor:
    def __init__(self,repository,handlers,token):
        self.repository=repository; self.handlers=dict(handlers); self.token=token

    async def execute(self,node_id,state):
        identity={k:state[k] for k in ('thread_id','scope','run_ref','plan_ref','graph_ref','activation_id','input_refs')}
        identity['node_id']=node_id
        identity['visit_number']=state.get('node_visits',{}).get(node_id,0)
        digest=canonical_hash(identity)
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
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
        context=NodeContext(node_id,f'{state["scope"]}/{node_id}',state['thread_id'],state['scope'],state['run_ref'],state['plan_ref'],state['graph_ref'],state['activation_id'],visit_id,dict(state['input_refs']),dict(state.get('result_refs',{})),self.token.owner_epoch,self.token.lease_id)
        handler=self.handlers.get(node_id)
        if handler is None:
            result=NodeResult(execution_status='blocked',error=ErrorRecord(code='ADAPTER_MISSING',observed_at=0,message=f'handler {node_id} not configured'),coverage_status='blocked')
        else:
            try:
                result=NodeResult.model_validate(await handler(context))
            except Exception as exc:
                # The operation service retains uncertain external state; failure still routes to cleanup.
                result=NodeResult(execution_status='failed',error=ErrorRecord(code='EXECUTION_FAILED',effect_certainty='unknown',observed_at=0,message=type(exc).__name__),cleanup_status='unknown')
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            tx.put('receipts',receipt_key,{'identity':identity,'visit_id':visit_id,'result':result.model_dump(mode='json')},immutable=True)
            tx.put('outbox',f'receipt:{receipt_key}',{'kind':'node_result','scope':state['scope'],'node_id':node_id,'receipt_ref':receipt_key},immutable=True)
        return result,receipt_key
