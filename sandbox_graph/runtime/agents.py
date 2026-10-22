"""Evidence-scoped proposals; models cannot mutate measurements or execute code."""
import asyncio
from typing import Literal,Protocol
from pydantic import Field
from ..catalog.registry import NODE_GROUPS
from ..domain.models import Model,canonical_hash
from ..persistence.repository import Conflict

# WF-04：主 agent（root）/ 环境组统筹（group）/ 工作节点（work）三级责任。
AgentLevel = Literal['root', 'group', 'work']
_LEVEL_BY_GROUP = {'run': 'root', 'group': 'group', 'work': 'work', 'analysis': 'root', 'report': 'root'}
_NODE_LEVELS = {node: _LEVEL_BY_GROUP[group] for group, nodes in NODE_GROUPS.items() for node in nodes}


def node_agent_level(node_id: str) -> str:
    """节点由哪一级 agent 归属看护；未注册节点不得默认为某个 agent。"""
    if node_id not in _NODE_LEVELS:
        raise KeyError(f'unregistered node for agent ownership: {node_id}')
    return _NODE_LEVELS[node_id]


def agent_id_for(level: str, scope: str) -> str:
    """稳定的 owner agent 身份；同一级别同一 scope 必须得到同一身份。"""
    if level not in ('root', 'group', 'work'):
        raise ValueError(f'invalid agent level: {level}')
    if not scope:
        raise ValueError('agent scope must be non-empty')
    return f'agent:{level}:{scope}'


def owner_agent_for(node_id: str, scope: str) -> str:
    """执行器用：注册节点按三级身份；未注册（合成测试图）退化为节点级身份，
    绝不把未知节点默认归到某一级。"""
    try:
        level = node_agent_level(node_id)
    except KeyError:
        return f'agent:node:{scope}:{node_id}'
    return agent_id_for(level, scope)

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

class AgentSupervisor:
    """按 owner agent 消费 `agent_supervision` 记录（事件/账本驱动，不轮询）。

    - 未配置模型网关 → 显式记 `model_unavailable`，**不墨、不放行、不把未验证变有效**；
    - 配置模型网关 → 用职责范围内的 allowlist 构造上下文，产出经过校验的提案；
    - 提案只写 `agent_reviews`，**绝不修改测量值、资源或下级事实**。
    """

    def __init__(self, repository, gateway=None):
        self.repository = repository
        self.gateway = gateway

    def pending(self, owner_agent_id):
        """返回该 owner agent 尚待复核的看护记录 (key, body)。"""
        with self.repository.transaction() as tx:
            return [(row['key'], row['body']) for row in tx.scan('agent_supervision')
                    if row['body']['owner_agent_id'] == owner_agent_id
                    and not tx.get('agent_reviews', row['key'])]

    async def review(self, owner_agent_id, *, allowed_fields, accessible_evidence=(),
                     budget=None, fields_by_node=None, resources_disposed=True):
        decisions = []
        for key, record in self.pending(owner_agent_id):
            node = record['node_id']
            if self.gateway is None:
                decision = {'node_id': node, 'owner_agent_id': owner_agent_id,
                            'status': 'model_unavailable', 'proposal': None, 'reason': 'no_model_gateway'}
            else:
                provider = fields_by_node
                fields = dict(provider(record['node_id'], record) or {}) if callable(provider) \
                    else dict((provider or {}).get(record['node_id'], {}))
                context = self.gateway.build_context(scope=record['scope'], node_id=node, fields=fields,
                                                     budget=budget, allowed_fields=allowed_fields)
                proposal = await self.gateway.diagnose(context, accessible_evidence=accessible_evidence,
                                                       resources_disposed=resources_disposed)
                decision = {'node_id': node, 'owner_agent_id': owner_agent_id, 'status': 'proposed',
                            'proposal': proposal.model_dump(mode='json')}
            with self.repository.transaction() as tx:
                tx.put('agent_reviews', key, decision, immutable=True)
            decisions.append(decision)
        return decisions


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
