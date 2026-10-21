"""Explicit bounded rework orders; unknown actions terminate as blocked."""
from typing import Literal
from uuid import uuid4
from pydantic import Field,field_validator
from ..domain.models import Model,NodeResult,ErrorRecord,canonical_hash
from ..runtime.tasks import JobSpec,TaskBackend
from ..persistence.repository import Conflict


class ReworkOrder(Model):
    run_id: str = Field(min_length=1)
    plan_ref: str = Field(min_length=1)
    graph_ref: str = Field(min_length=1)
    kind: Literal['recompute','supplement','remeasure']
    action_ref: str = Field(min_length=1)
    issue_type: str = Field(min_length=1)
    evidence_refs: tuple[str,...] = Field(min_length=1)
    affected_work_refs: tuple[str,...] = Field(min_length=1)
    scope_changed: bool = False
    max_dispatches: Literal[1]
    deadline: float
    analysis_input_ref: str | None = None

    @field_validator('max_dispatches',mode='before')
    @classmethod
    def strict_budget(cls,value):
        if type(value) is not int: raise ValueError('dispatch budget requires an explicit integer')
        return value


class ReworkService:
    """The registered action analysis.recompute:1 uses the real analysis worker.

    Supplement/measurement actions require additional deployment contracts and
    are deliberately rejected before enqueue. Rejected orders are immutable;
    a corrected contract is a new order, not a mutation of an execution receipt.
    """
    def __init__(self,repository): self.repository=repository

    def register(self,order:ReworkOrder):
        order=ReworkOrder.model_validate(order.model_dump(mode='json'))
        identity=canonical_hash(order)
        with self.repository.transaction() as tx:
            tx.put('rework_orders',identity,order.model_dump(mode='json'),immutable=True)
        return identity

    def dispatch(self,order_ref,*,token,run_id,plan_ref,graph_ref):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            row=tx.get('rework_orders',order_ref)
            def reject(code,message):
                if previous: return NodeResult.model_validate(previous['body']['result'])
                result=NodeResult(execution_status='blocked',coverage_status='blocked',error=ErrorRecord(code=code,observed_at=tx.now,message=message),output={'rework_ref':order_ref})
                tx.put('rework_execution',order_ref,{'status':'blocked','result':result.model_dump(mode='json')},immutable=True)
                return result
            previous=tx.get('rework_execution',order_ref)
            if not row: return reject('INPUT_MISSING','immutable rework order required')
            try: order=ReworkOrder.model_validate(row['body'])
            except ValueError: return reject('CONTRACT_CONFLICT','typed bounded rework order required')
            if canonical_hash(order)!=order_ref: return reject('CONTRACT_CONFLICT','rework order digest differs')
            if (order.run_id,order.plan_ref,order.graph_ref)!=(run_id,plan_ref,graph_ref):
                return NodeResult(execution_status='blocked',error=ErrorRecord(code='VERSION_CONFLICT',observed_at=tx.now,message='order belongs to another execution identity'))
            if previous: return NodeResult.model_validate(previous['body']['result'])
            run=tx.get('runs',run_id)
            if not run or (run['body']['active_plan_ref'],run['body']['graph_ref'])!=(plan_ref,graph_ref): return reject('VERSION_CONFLICT','run changed before rework dispatch')
            if order.deadline<=tx.now: return reject('BUDGET_EXHAUSTED','rework deadline elapsed')
            if order.scope_changed: return reject('REPLAN_REQUIRED','scope changes require a new plan')
            work=[tx.get('work_instances',ref) for ref in order.affected_work_refs]
            if any(not row or row['body']['run_id']!=run_id or row['body']['plan_ref']!=plan_ref for row in work): return reject('REPLAN_REQUIRED','affected work is outside this run and frozen plan')
            if order.kind!='recompute' or order.action_ref!='analysis.recompute:1': return reject('ADAPTER_MISSING','requested rework action has no registered executor')
            frozen=tx.get('analysis_inputs',order.analysis_input_ref) if order.analysis_input_ref else None
            if not frozen or frozen['body']['plan_hash']!=plan_ref or frozen['body'].get('missing_scope'): return reject('EVIDENCE_INSUFFICIENT','complete frozen analysis input required')
            manifest=frozen['body'];covered=set()
            for scope,(identity,version) in manifest['deliveries'].items():
                delivery=tx.get(manifest['delivery_kind'],f'{identity}:{version}')
                review=tx.get('reviews',manifest['reviews'][scope])
                if not delivery or not review or review['body']['decision']!='accepted': return reject('EVIDENCE_INSUFFICIENT','frozen input has missing or rejected deliveries')
                covered.update(delivery['body']['work_refs'])
            if not set(order.affected_work_refs)<=covered: return reject('REPLAN_REQUIRED','affected work not covered by frozen input')
            job_id=str(uuid4())
            TaskBackend.enqueue_in(tx,JobSpec(job_id=job_id,job_kind='analysis',scope_ref=run_id,thread_id=f'{run_id}:rework:{order_ref}',activation_id=str(uuid4()),dedupe_key=f'rework:{order_ref}',due_at=tx.now,deadline=order.deadline,run_id=run_id,dispatch_generation=run['body']['dispatch_generation'],payload={'analysis_input_ref':order.analysis_input_ref,'plan_ref':plan_ref,'graph_ref':graph_ref,'source_rework_ref':order_ref}))
            result=NodeResult(execution_status='succeeded',output={'rework_ref':order_ref,'job_ref':job_id,'rework_status':'dispatched'})
            tx.put('rework_execution',order_ref,{'status':'dispatched','job_ref':job_id,'result':result.model_dump(mode='json')},immutable=True)
            return result
