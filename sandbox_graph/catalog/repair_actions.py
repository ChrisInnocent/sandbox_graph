"""版本化修复白名单；登记不授予权限，也不证明现场适配已经验收。

动作只能通过固定 NodeExecutor 路由执行，绝不按模型提供的名字加载代码。
范围、失败证据来源、授权、互斥、预算和资源处置仍由应用服务在事务中核验。
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, model_validator

from .registry import NODE_GROUPS
from ..domain.agent_lifecycle import LifecycleModel, Ref, ResourceDispositionReceipt
from ..domain.models import NodeResult
from ..runtime.node_agent import NodeReview
from ..runtime.runner import RunnerCollection, RunnerStatus

if TYPE_CHECKING:
    from ..graphs.executor import NodeExecutor


class RepairActionNotRegistered(ValueError):
    """动作、版本、节点三元组没有登记。"""


class RepairActionUnavailable(ValueError):
    """逻辑合同存在，但具体执行能力不可用。"""


class RepairActionInput(LifecycleModel):
    source_receipt: Ref
    evidence_refs: Annotated[tuple[Ref, ...], Field(min_length=1)]
    input_refs: dict[Ref, Ref] = Field(default_factory=dict)


class RepairApplicabilityRule(LifecycleModel):
    """Trusted data: exact receipt errors, never executable model predicates."""
    rule_ref: Ref
    node_id: Ref
    action_ref: Ref
    predicate: Literal['source_error', 'repair_failure', 'postcheck_rejected']
    error_codes: Annotated[tuple[Ref, ...], Field(max_length=64)] = ()
    preceding_action_ref: Ref | None = None
    preceding_postcheck_ref: Ref | None = None

    @model_validator(mode='after')
    def explicit_predecessor(self):
        if (self.predicate != 'source_error') != (self.preceding_action_ref is not None):
            raise ValueError('repair predicates require a preceding registered action')
        postcheck = self.predicate == 'postcheck_rejected'
        if postcheck != (self.preceding_postcheck_ref is not None):
            raise ValueError('only postcheck_rejected requires an exact preceding postcheck')
        if postcheck == bool(self.error_codes):
            raise ValueError('execution predicates require error codes; postcheck rejection carries none')
        for ref in (self.action_ref, self.preceding_action_ref):
            if ref is not None and ('@' not in ref or not all(ref.rpartition('@')[::2])):
                raise ValueError('versioned action reference required')
        return self


@dataclass(frozen=True)
class RepairActionReference:
    action_id: str
    version: str

    @property
    def ref(self) -> str:
        return f'{self.action_id}@{self.version}'


@dataclass(frozen=True)
class NodeExecutorRoute:
    """固定节点的真实执行入口；不接收 action 字符串或可执行代码。"""
    node_id: str

    async def execute(self, executor: NodeExecutor, state: dict):
        from ..graphs.executor import NodeExecutor
        if not isinstance(executor, NodeExecutor):
            raise RepairActionUnavailable('repair requires the controlled NodeExecutor')
        if not callable(executor.handlers.get(self.node_id)):
            raise RepairActionUnavailable(f'handler {self.node_id} not configured')
        return await executor.execute(self.node_id, state)


@dataclass(frozen=True)
class RepairActionDefinition:
    action_id: str
    version: str
    node_id: str
    availability: Literal['available', 'unavailable']
    input_schema: type[RepairActionInput]
    executor_route: NodeExecutorRoute | None
    effect_class: str
    permissions: tuple[str, ...]
    conflict_domains: tuple[str, ...]
    postcheck_ref: str
    reconcile_ref: str
    cleanup_ref: str
    reentry_mode: str
    verification_status: Literal['planned', 'locally_verified']
    unavailable_reason: str | None = None

    @property
    def ref(self) -> str:
        return f'{self.action_id}@{self.version}'

    def require_available(self) -> RepairActionDefinition:
        if self.availability != 'available' or self.executor_route is None:
            raise RepairActionUnavailable(self.unavailable_reason or 'repair route unavailable')
        return self

    def postcheck(self, result: NodeResult, review: NodeReview | None, *,
                  disposition: ResourceDispositionReceipt | None = None,
                  checked_at: float | None = None,
                  operation_statuses: Mapping[str, RunnerStatus] | None = None,
                  collection: RunnerCollection | None = None) -> bool:
        """对可信服务已核验的事实作动作专属检查；缺证明即拒绝。

        服务须在同一 fenced 事务中核验 receipt 归属、proof 来源及当前账本，
        再传入这些对象；不得从模型或 result.output 构造“已验证”证明。
        """
        if not (self.availability == 'available' and result.execution_status == 'succeeded'
                and result.review_status != 'rejected' and result.error is None
                and review is not None and review.node_id == self.node_id
                and review.decision == 'accept'):
            return False
        if self.ref == 'case.reconcile@1' and self.node_id == 'N01':
            return (bool(result.operation_refs) and isinstance(operation_statuses, Mapping)
                    and set(operation_statuses) == set(result.operation_refs)
                    and all(isinstance(status, RunnerStatus) and status.authoritative is True
                            and status.state in {'not_submitted', 'completed', 'failed', 'cancelled'}
                            for status in operation_statuses.values()))
        if self.ref == 'evidence.seal@1' and self.node_id == 'N07':
            return (result.data_status == 'valid' and bool(result.artifact_refs)
                    and isinstance(collection, RunnerCollection) and not collection.interrupted
                    and not collection.missing_sequences
                    and set(collection.artifact_refs) == set(result.artifact_refs))
        if self.ref == 'resource.cleanup@1' and self.node_id == 'N09':
            return (result.cleanup_status == 'complete'
                    and isinstance(disposition, ResourceDispositionReceipt)
                    and type(checked_at) in {int, float} and isfinite(checked_at)
                    and disposition.observed_at <= checked_at < disposition.expires_at
                    and review.owner_agent_id == f'agent:work:{disposition.work_ref}'
                    and disposition.producer == 'lifecycle_safety' and disposition.producer_version == '1'
                    and disposition.stopped and disposition.admission_status == 'closed'
                    and disposition.sampler_status in {'stopped', 'not_started'}
                    and not disposition.residual_resources and not disposition.isolation_domains
                    and set(disposition.operation_refs) == set(result.operation_refs)
                    and set(disposition.owned_resources) == set(result.resource_refs))
        return False


@dataclass(frozen=True)
class NodeRepairPolicy:
    node_id: str
    owner_role: str
    scope_kind: Literal['run', 'group', 'work']
    policy_version: str
    actions: tuple[RepairActionReference, ...]
    repair_availability: Literal['available', 'unavailable']
    unavailable_reason: str | None
    diagnosis_allowed: bool = True
    candidate_allowed: bool = True


class RepairActionCatalog:
    def __init__(self, definitions: tuple[RepairActionDefinition, ...],
                 policies: tuple[NodeRepairPolicy, ...], *,
                 rules: tuple[RepairApplicabilityRule, ...] = ()):
        actions = {}
        for definition in definitions:
            key = (definition.action_id, definition.version, definition.node_id)
            if key in actions:
                raise ValueError('duplicate repair action registration')
            if definition.availability == 'available':
                definition.require_available()
                if definition.executor_route.node_id != definition.node_id:
                    raise ValueError('repair route node differs from registration')
                if (not isinstance(definition.input_schema, type)
                        or not issubclass(definition.input_schema, RepairActionInput)):
                    raise ValueError('repair requires the strict versioned input schema')
                if not all((definition.permissions, definition.conflict_domains,
                            definition.postcheck_ref, definition.reconcile_ref,
                            definition.cleanup_ref)):
                    raise ValueError('repair registration lacks permissions, effects or verification hooks')
            elif definition.executor_route is not None or not definition.unavailable_reason:
                raise ValueError('unavailable repair must have a reason and no executable route')
            actions[key] = definition
        self._actions = MappingProxyType(actions)
        registered_rules = {}
        for rule in rules:
            if not isinstance(rule, RepairApplicabilityRule):
                raise ValueError('strict versioned applicability rule required')
            rule = RepairApplicabilityRule.model_validate_json(rule.model_dump_json())
            if rule.rule_ref in registered_rules:
                raise ValueError('duplicate applicability rule')
            for ref in (rule.action_ref, rule.preceding_action_ref):
                if ref is not None:
                    action_id, _, version = ref.rpartition('@')
                    self.resolve(action_id, version, rule.node_id).require_available()
            if rule.predicate == 'postcheck_rejected':
                action_id, _, version = rule.preceding_action_ref.rpartition('@')
                preceding = self.resolve(action_id, version, rule.node_id)
                if rule.preceding_postcheck_ref != preceding.postcheck_ref:
                    raise ValueError('rule postcheck differs from registered predecessor postcheck')
            registered_rules[rule.rule_ref] = rule
        self.rules = tuple(registered_rules.values())
        policy_map = {}
        for policy in policies:
            if policy.node_id in policy_map:
                raise ValueError('duplicate node policy')
            for action in policy.actions:
                if (action.action_id, action.version, policy.node_id) not in actions:
                    raise ValueError('policy references an unregistered action')
            policy_map[policy.node_id] = policy
        self.node_policies = MappingProxyType(policy_map)

    def resolve(self, action_id: str, version: str, node_id: str) -> RepairActionDefinition:
        if any(type(value) is not str for value in (action_id, version, node_id)):
            raise RepairActionNotRegistered('action, version and node must be explicit strings')
        try:
            return self._actions[(action_id, version, node_id)]
        except KeyError:
            raise RepairActionNotRegistered('action/version/node not registered') from None


# 这些名称是白名单中的控制合同引用，不是可供动态 import 的 Python 路径。
# 所有现场操作的 reconcile/cleanup 都必须经过已有 OperationService 的 fence/所有权校验。
_WORK_ACTIONS = (
    # node, action, effect, permissions, postcheck, reconcile, cleanup, reentry, available
    ('N01', 'case.reconcile', 'external_read', ('operation.read_owned',),
     'case.known_operation@1', 'runner.reconcile_owned@1', 'runner.cleanup_owned@1', 'compile_recovery', True),
    ('N02', 'fixture.repair', 'external_mutation', ('fixture.repair_owned',),
     'fixture.revalidate@1', 'runner.reconcile_owned@1', 'runner.cleanup_owned@1', 'revalidate_N01_N02_N03', False),
    ('N03', 'preflight.recheck', 'external_read', ('preflight.read_owned',),
     'preflight.assertions@1', 'runner.reconcile_owned@1', 'runner.cleanup_owned@1', 'compile_recovery', False),
    ('N04', 'observation.repair', 'external_mutation', ('collector.repair_owned',),
     'observation.baseline@1', 'runner.reconcile_owned@1', 'runner.cleanup_owned@1', 'new_baseline', False),
    ('N05', 'resume_owned_operation', 'external_mutation', ('operation.resume_owned',),
     'operation.continuity@1', 'runner.reconcile_owned@1', 'runner.cleanup_owned@1', 'original_operation_only', False),
    ('N06', 'steady.reobserve', 'external_read', ('observation.read_owned',),
     'observation.continuity@1', 'runner.reconcile_owned@1', 'runner.cleanup_owned@1', 'continuous_sampler_only', False),
    ('N07', 'evidence.seal', 'artifact_write', ('evidence.seal_owned',),
     'evidence.complete_collection@1', 'runner.collect_owned@1', 'evidence.preserve_raw@1', 'compile_recovery', True),
    ('N08', 'analysis.recompute', 'artifact_write', ('analysis.write_revision',),
     'analysis.frozen_raw@1', 'analysis.reconcile_revision@1', 'evidence.preserve_raw@1', 'new_result_revision', False),
    ('N09', 'resource.cleanup', 'external_mutation', ('resource.cleanup_owned',),
     'resource.closed_disposition@1', 'runner.reconcile_owned@1', 'runner.cleanup_owned@1', 'revalidate_destroyed_dependencies', True),
    ('N10', 'delivery.republish', 'artifact_write', ('delivery.publish_owned',),
     'delivery.manifest@1', 'delivery.lookup@1', 'evidence.preserve_raw@1', 'idempotent_delivery', False),
)


def standard_repair_catalog() -> RepairActionCatalog:
    definitions = tuple(RepairActionDefinition(
        action_id=action, version='1', node_id=node,
        availability='available' if available else 'unavailable',
        input_schema=RepairActionInput,
        executor_route=NodeExecutorRoute(node) if available else None,
        effect_class=effect, permissions=permissions, conflict_domains=('work_owned_resources',),
        postcheck_ref=postcheck, reconcile_ref=reconcile, cleanup_ref=cleanup,
        reentry_mode=reentry, verification_status='locally_verified' if available else 'planned',
        unavailable_reason=None if available else 'versioned repair handler/postcheck not implemented',
    ) for node, action, effect, permissions, postcheck, reconcile, cleanup, reentry, available in _WORK_ACTIONS)
    by_node = {definition.node_id: definition for definition in definitions}
    policies = tuple(NodeRepairPolicy(
        node_id=node, owner_role=group,
        scope_kind=group if group in {'run', 'group', 'work'} else 'run',
        policy_version='1', actions=(RepairActionReference(by_node[node].action_id, '1'),)
        if node in by_node else (),
        repair_availability=by_node[node].availability if node in by_node else 'unavailable',
        unavailable_reason=by_node[node].unavailable_reason if node in by_node else 'no_registered_repair_action',
    ) for group, nodes in NODE_GROUPS.items() for node in nodes)
    # Exact adapter failures are the currently known execution failure class.
    # Other errors/quality findings require bounded investigation or input.
    rules = tuple(RepairApplicabilityRule(schema_version=1,
        rule_ref=f'{definition.action_id}.benchmark-step-error@1',
        node_id=definition.node_id, action_ref=definition.ref,
        predicate='source_error', error_codes=('BENCHMARK_STEP_FAILED', 'BENCHMARK_STEP_BLOCKED'))
        for definition in definitions if definition.availability == 'available')
    return RepairActionCatalog(definitions, policies, rules=rules)


def resolve(action_id: str, version: str, node_id: str) -> RepairActionDefinition:
    """默认目录的便捷入口；始终按完整三元组解析。"""
    return standard_repair_catalog().resolve(action_id, version, node_id)
