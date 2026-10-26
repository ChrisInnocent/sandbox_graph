"""Versioned extension envelope. Legacy Plan and Case encoding stays unchanged."""
from typing import Literal
from pydantic import Field, model_validator
from ..domain.models import Model, WorkKey, canonical_hash
from .compiler import Plan
from .identity import condition_identity


class MeasurementGenerationPlan(Model):
    kind: Literal['measurement-generation.v1'] = 'measurement-generation.v1'
    target_plan: Plan
    target_plan_hash: str
    target_case_id: str
    source_work_ref: str
    source_work_revision: int
    source_run_ref: str
    source_run_revision: int
    source_plan_ref: str
    source_graph_ref: str
    source_contract_ref: str
    source_receipt_ref: str
    source_node: Literal['N04','N05','N06','N07','N08']
    source_lifecycle_ref: str
    source_lifecycle_revision: int
    lineage_id: str
    source_key: WorkKey
    source_material_hash: str
    condition: dict
    authorization_ref: str = Field(min_length=1)
    authorization_revision: Literal[0] = 0
    measurement_generation: int = Field(ge=2, strict=True)

    @model_validator(mode='after')
    def validate_extension(self):
        if self.target_plan.hash != self.target_plan_hash or self.target_plan.stage != 'all' or self.target_plan.status != 'ready':
            raise ValueError('exact ready measurement plan required')
        matches=[c for c in self.target_plan.cases if c.case_id==self.target_case_id]
        if len(matches)!=1: raise ValueError('exact target case required')
        case=matches[0]
        if self.condition not in case.conditions or canonical_hash(condition_identity(case,self.condition))!=self.source_key.condition_hash:
            raise ValueError('original experimental condition required')
        if (case.group_id,case.method_id,case.method_version,case.scenario_id,case.repeat_id)!=(
            self.source_key.group_id,self.source_key.method_id,self.source_key.method_version,self.source_key.scenario_id,self.source_key.repeat_id):
            raise ValueError('original method identity required')
        if self.measurement_generation != self.source_key.measurement_generation+1:
            raise ValueError('generation must succeed frozen source')
        return self

    @property
    def hash(self): return canonical_hash(self)

    @property
    def selected_case(self):
        return next(c for c in self.target_plan.cases if c.case_id==self.target_case_id).model_copy(update={'conditions':(self.condition,)})


class GenerationPlanView:
    """Only the frozen case/condition is executable; identity is the envelope."""
    def __init__(self,envelope): self.envelope=envelope
    @property
    def hash(self): return self.envelope.hash
    @property
    def cases(self): return (self.envelope.selected_case,)
    @property
    def gaps(self): return tuple(g for g in self.envelope.target_plan.gaps if g.case_id==self.envelope.target_case_id)
    def __getattr__(self,name): return getattr(self.envelope.target_plan,name)


def load_plan(body):
    if body.get('kind')=='measurement-generation.v1':
        return GenerationPlanView(MeasurementGenerationPlan.model_validate(body))
    return Plan.model_validate(body)
