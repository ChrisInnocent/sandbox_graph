"""Pure failure-to-action planner; unknown states never become mutations."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..domain.models import canonical_hash
from .contracts import ActionStep, EnvironmentDeclaration, EnvironmentReport, RepairPlan


@dataclass(frozen=True)
class ActionDefinition:
    product: str
    profile: str
    repair_codes: tuple[str, ...]
    action_ref: str
    roles: tuple[str, ...]
    timeout_seconds: int
    reconcile_rule: str
    retry_class: str = "reconcile"
    dependencies: tuple[str, ...] = ()
    compensation_ref: str | None = None
    source_ref: str = ""

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9._-]+", self.action_ref):
            raise ValueError("action ref must be a registered identifier")
        if self.timeout_seconds <= 0 or self.retry_class not in {"never", "bounded", "reconcile"}:
            raise ValueError("action timeout or retry class is invalid")
        if not self.repair_codes or not self.roles:
            raise ValueError("action requires repair codes and roles")


class ActionCatalog:
    def __init__(self, actions: Iterable[ActionDefinition]):
        self._by_code = {}
        seen = set()
        for action in actions:
            for code in action.repair_codes:
                key = (action.product, action.profile, code)
                if key in seen:
                    raise ValueError("duplicate repair action rule")
                seen.add(key)
                self._by_code[key] = action
        groups = {}
        for (product, profile, code), action in self._by_code.items():
            groups.setdefault((product, profile), {})[code] = action
        for actions_by_code in groups.values():
            visiting, visited = set(), set()
            def visit(code):
                if code in visiting:
                    raise ValueError("action dependency cycle")
                if code in visited:
                    return
                visiting.add(code)
                for dependency in actions_by_code[code].dependencies:
                    if dependency in actions_by_code:
                        visit(dependency)
                visiting.remove(code)
                visited.add(code)
            for code in sorted(actions_by_code):
                visit(code)

    def resolve(self, product: str, profile: str, code: str) -> ActionDefinition | None:
        return self._by_code.get((product, profile, code))


@dataclass(frozen=True)
class PlanBlocker:
    code: str
    node_id: str
    check_id: str
    reason: str


@dataclass(frozen=True)
class PlanBuildResult:
    status: str
    plan: RepairPlan
    blockers: tuple[PlanBlocker, ...]


class RepairPlanner:
    def __init__(self, catalog: ActionCatalog):
        self.catalog = catalog

    def plan(self, declaration: EnvironmentDeclaration, report: EnvironmentReport, *,
             report_ref: str, deadline: str, authorized_actions: set[str],
             repair_round: int = 0, max_repair_rounds: int = 2,
             max_steps: int = 64) -> PlanBuildResult:
        if report.declaration_hash != declaration.declaration_hash:
            raise ValueError("report does not belong to environment declaration")
        if report.profile != declaration.profile:
            raise ValueError("report profile does not match declaration")
        if not report_ref.startswith("artifact://"):
            raise ValueError("report ref must be an artifact reference")
        if not 0 <= repair_round < max_repair_rounds or max_repair_rounds < 1:
            raise ValueError("repair round budget is exhausted or invalid")
        if not 1 <= max_steps <= 256:
            raise ValueError("step budget must be between 1 and 256")
        nodes = {node.node_id: node for node in declaration.nodes}
        observations = sorted(report.observations, key=lambda item: (item.node_id, item.check_id, item.rule_ref))
        blockers: list[PlanBlocker] = []
        candidates = []
        for observation in observations:
            if observation.node_id not in nodes:
                blockers.append(PlanBlocker("NODE_NOT_IN_DECLARATION", observation.node_id,
                                            observation.check_id, "observation scope is undeclared"))
                continue
            if observation.status in {"satisfied", "not_applicable"}:
                continue
            if observation.status in {"unknown", "unsupported"}:
                code = "OBSERVATION_UNKNOWN" if observation.status == "unknown" else "RULE_UNSUPPORTED"
                blockers.append(PlanBlocker(code, observation.node_id, observation.check_id,
                                            f"observation status is {observation.status}"))
                continue
            if not observation.repair_code:
                blockers.append(PlanBlocker("NO_REPAIR_RULE", observation.node_id, observation.check_id,
                                            "no deterministic repair code is attached"))
                continue
            action = self.catalog.resolve(declaration.product, declaration.profile, observation.repair_code)
            if action is None:
                blockers.append(PlanBlocker("ACTION_UNREGISTERED", observation.node_id, observation.check_id,
                                            observation.repair_code))
                continue
            if action.action_ref not in authorized_actions:
                blockers.append(PlanBlocker("ACTION_NOT_AUTHORIZED", observation.node_id, observation.check_id,
                                            action.action_ref))
                continue
            if not set(nodes[observation.node_id].roles).intersection(action.roles):
                blockers.append(PlanBlocker("ROLE_MISMATCH", observation.node_id, observation.check_id,
                                            action.action_ref))
                continue
            candidates.append((observation, action))
        candidate_codes = {obs.repair_code for obs, _ in candidates}
        for observation, action in candidates:
            absent = set(action.dependencies) - candidate_codes
            if absent:
                blockers.append(PlanBlocker("ACTION_DEPENDENCY_MISSING", observation.node_id,
                    observation.check_id, ",".join(sorted(absent))))
        if len(candidates) > max_steps:
            blockers.append(PlanBlocker("STEP_BUDGET_EXCEEDED", "", "", str(len(candidates))))
        steps = []
        if not blockers:
            code_actions = {obs.repair_code: action for obs, action in candidates}
            ordered_codes = []
            visited_codes = set()
            def order_code(code):
                if code in visited_codes:
                    return
                action = code_actions[code]
                for dependency in sorted(action.dependencies):
                    if dependency in code_actions:
                        order_code(dependency)
                visited_codes.add(code)
                ordered_codes.append(code)
            for code in sorted(code_actions):
                order_code(code)
            candidates.sort(key=lambda pair: (ordered_codes.index(pair[0].repair_code), pair[0].node_id, pair[0].check_id, pair[0].rule_ref))
            step_ids_by_code: dict[str, list[str]] = {}
            for observation, action in candidates:
                step_id = canonical_hash({"declaration": declaration.declaration_hash,
                    "report": report_ref, "check": observation.check_id,
                    "node": observation.node_id, "action": action.action_ref})[:32]
                input_data = {"observation_id": observation.check_id,
                    "rule_ref": observation.rule_ref, "repair_code": observation.repair_code,
                    "boot_id": observation.boot_id,
                    "observed": observation.observed, "expected": observation.expected}
                dependencies = tuple(sorted(step for code in action.dependencies
                    for step in step_ids_by_code.get(code, ())))
                step_ids_by_code.setdefault(observation.repair_code, []).append(step_id)
                steps.append(ActionStep(step_id=step_id, action_ref=action.action_ref,
                    node_id=observation.node_id, input=input_data,
                    input_hash=canonical_hash({"action": action.action_ref, "input": input_data}),
                    expected_before={"observed": observation.observed},
                    preconditions=(f"observation:{observation.check_id}:{observation.status}",),
                    postconditions=(action.reconcile_rule,), secret_refs=(), depends_on=dependencies,
                    reconcile_rule=action.reconcile_rule, timeout_seconds=action.timeout_seconds,
                    retry_class=action.retry_class, compensation_ref=action.compensation_ref,
                    ownership=f"environment:{declaration.environment_id}"))
        domains = tuple(sorted({f"host:{node.machine_id}" for node in declaration.nodes}))
        body = {"report_ref": report_ref, "declaration_hash": declaration.declaration_hash,
            "steps": [step.model_dump(mode="json") for step in steps], "conflict_domains": domains,
            "revision": repair_round + 1, "deadline": deadline,
            "repair_round_budget": max_repair_rounds - repair_round - 1}
        plan = RepairPlan(plan_hash=canonical_hash(body), **body)
        status = "blocked" if blockers else "ready" if steps else "already_satisfied"
        return PlanBuildResult(status, plan, tuple(blockers))


__all__ = ["ActionCatalog", "ActionDefinition", "PlanBlocker", "PlanBuildResult", "RepairPlanner"]
