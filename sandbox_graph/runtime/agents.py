"""Evidence-scoped proposals; models cannot mutate measurements or execute code."""
import asyncio
from typing import Protocol
from pydantic import Field
from ..domain.models import Model,canonical_hash
from ..persistence.repository import Conflict

class ContextBudget(Model):
    max_input_bytes: int = Field(gt=0)
    max_output_bytes: int = Field(gt=0)
    max_calls: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    deadline: float

class Fact(Model):
    statement: str
    evidence_refs: tuple[str,...]

class DiagnosisProposal(Model):
    facts: tuple[Fact,...]
    hypotheses: tuple[str,...]
    suggested_actions: tuple[str,...]
    affected_scope: tuple[str,...]
    required_decisions: tuple[str,...]

class ModelGateway(Protocol):
    async def diagnose(self,context:dict) -> tuple[dict,int|None,str]: ...

class AgentGateway:
    def __init__(self,repository,model,*,registered_actions): self.repository=repository; self.model=model; self.actions=set(registered_actions)
    def build_context(self,*,scope,node_id,fields,budget,allowed_fields):
        import json
        selected={key:value for key,value in fields.items() if key in allowed_fields}
        # Preserve all included failure facts or reject; never silently truncate.
        payload={'scope':scope,'node_id':node_id,'fields':selected}
        if len(json.dumps(payload,ensure_ascii=False).encode())>budget.max_input_bytes: raise ValueError('context exceeds byte budget; request narrower evidence ranges')
        return payload|{'budget':budget.model_dump(mode='json'),'context_id':canonical_hash(payload)}
    def validate_proposal(self,proposal,*,scope,accessible_evidence):
        if set(proposal.affected_scope)-{scope}: raise PermissionError('proposal expands scope')
        if set(proposal.suggested_actions)-self.actions: raise ValueError('proposal requests unregistered action')
        for fact in proposal.facts:
            if not fact.evidence_refs or set(fact.evidence_refs)-set(accessible_evidence): raise ValueError('proposal fact lacks accessible evidence')
        return proposal
    async def diagnose(self,context,*,accessible_evidence,resources_disposed):
        import json
        if not resources_disposed: raise Conflict('dispose or quarantine active resources before diagnosis')
        budget=ContextBudget.model_validate(context['budget']); identity=context['context_id']
        with self.repository.transaction() as tx:
            old=tx.get('agent_budgets',identity); used=old['body'] if old else {'calls':0,'tokens':0}
            if used['calls']>=budget.max_calls or used['tokens']>=budget.max_tokens or tx.now>=budget.deadline: raise Conflict('model budget exhausted')
            timeout=budget.deadline-tx.now
            call=used['calls']+1
            tx.put('agent_budgets',identity,used|{'calls':call})
        output,usage,model_version=await asyncio.wait_for(self.model.diagnose(context),timeout=timeout)
        if len(json.dumps(output,ensure_ascii=False).encode())>budget.max_output_bytes: raise ValueError('model response exceeds byte budget')
        proposal=DiagnosisProposal.model_validate(output)
        self.validate_proposal(proposal,scope=context['scope'],accessible_evidence=accessible_evidence)
        if usage is not None and (not isinstance(usage,int) or usage<0): raise ValueError('invalid token usage')
        with self.repository.transaction() as tx:
            used=tx.get('agent_budgets',identity)['body']
            new_tokens=used['tokens']+(usage or 0)
            tx.put('agent_calls',f'{identity}:{call}',{'context_hash':identity,'scope':context['scope'],'model_version':model_version,'usage':usage,'usage_status':'unknown' if usage is None else 'known','reason':'diagnosis','proposal':proposal.model_dump(mode='json')},immutable=True)
            tx.put('agent_budgets',identity,used|{'tokens':new_tokens})
            if new_tokens>budget.max_tokens: raise Conflict('model token budget exceeded')
        return proposal
