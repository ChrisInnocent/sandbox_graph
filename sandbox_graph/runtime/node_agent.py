"""节点 agent 层。

设计意图（对应 WF-04 / RT-24 与图工程惯例）：
- **图控上下文**：节点 agent 只拿到职责内的输入（allowlist），拿不到别人的对话/日志；
- **脚本保确定性**：agent 只**调用**注册脚本，不改写测量逻辑；同一输入得到同一结果；
- **agent 只做执行/复审/落账**：复审产出 findings/decision，**绝不覆盖下级执行事实**；
- LLM 只在规则答不了时（M 节点）介入，且只出提案（见 `agents.py`）。
"""
from dataclasses import replace

from pydantic import Field

from ..domain.models import Model, NodeResult


class NodeReview(Model):
    node_id: str
    owner_agent_id: str
    decision: str            # accept | reject
    findings: tuple[str, ...] = ()


class NodeAgent:
    """一个节点的执行 actor：小上下文 + 调用注册脚本 + 规则复审。"""

    def __init__(self, node_id, owner_agent_id, action, *, allowed_input_keys=None,
                 registered_actions=()):
        self.node_id = node_id
        self.owner_agent_id = owner_agent_id
        self.action = action
        self.allowed_input_keys = frozenset(allowed_input_keys) if allowed_input_keys is not None else None
        self.registered_actions = frozenset(registered_actions)

    def scope_context(self, context):
        """只把职责内的输入交给脚本；出现越界字段就拒绝（不静默扩大范围）。"""
        if self.allowed_input_keys is None:
            return context
        extra = set(context.input_refs) - self.allowed_input_keys
        if extra:
            raise PermissionError(f'agent context contains fields outside its duty: {sorted(extra)}')
        return replace(context, input_refs={k: v for k, v in context.input_refs.items()
                                            if k in self.allowed_input_keys})

    async def run(self, context):
        scoped = self.scope_context(context)
        result = NodeResult.model_validate(await self.action(scoped))
        return result, self.review(scoped, result)

    def review(self, context, result):
        """规则复审：只产出 findings/decision，**不修改**执行结果（下级事实不可被覆盖）。"""
        findings = []
        if (result.data_status == 'valid' and not result.artifact_refs
                and result.coverage_status != 'unsupported'):
            findings.append('valid data requires raw evidence')
        if result.execution_status == 'blocked' and result.error is None:
            findings.append('blocked requires an error record')
        return NodeReview(node_id=self.node_id, owner_agent_id=self.owner_agent_id,
                          decision='reject' if findings else 'accept', findings=tuple(findings))
