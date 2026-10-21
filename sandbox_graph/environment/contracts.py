"""Versioned, secret-reference-only contracts for product environment work."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..domain.models import canonical_hash


SecretRef = str
ArtifactRef = str
Ref = str
_SECRET_REF = r"^secret://[^\s]+$"
_ARTIFACT_REF = r"^artifact://[^\s]+$"
_SHA256 = r"^[a-f0-9]{64}$"


class EnvironmentModel(BaseModel):
    """Strict immutable v2 environment model, separate from graph schema v1."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal[2] = 2


class ArtifactLock(EnvironmentModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(pattern=_SHA256)
    source_ref: str | None = None
    media_type: str | None = None


class TargetValue(EnvironmentModel):
    mode: Literal["preserve", "exact", "relax_for_plan"] = "preserve"
    value: Any | None = None
    unit: str | None = None
    reason: str | None = None
    budget_ref: str | None = None

    @model_validator(mode="after")
    def require_exact_value(self):
        if self.mode == "exact" and self.value is None:
            raise ValueError("exact target requires a value")
        if self.mode == "relax_for_plan" and not self.budget_ref:
            raise ValueError("relax_for_plan requires an explicit budget reference")
        return self


class TargetPolicy(EnvironmentModel):
    host_attributes: dict[str, TargetValue] = Field(default_factory=dict)
    platform_limits: dict[str, TargetValue] = Field(default_factory=dict)
    sandbox_spec: dict[str, TargetValue] = Field(default_factory=dict)
    performance_policy: dict[str, TargetValue] = Field(default_factory=dict)


class NodeDeclaration(EnvironmentModel):
    node_id: str = Field(min_length=1)
    machine_id: str = Field(min_length=1)
    roles: tuple[str, ...] = Field(min_length=1)
    ssh_address: str = Field(min_length=1)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(min_length=1)
    known_hosts_ref: ArtifactRef
    initial_credential_ref: SecretRef | None = None
    automation_credential_ref: SecretRef
    privilege_policy: Literal["user", "sudo", "root"] = "sudo"
    runtime_namespace: str | None = None
    paths: dict[str, str] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()

    @field_validator("known_hosts_ref")
    @classmethod
    def known_hosts_is_ref(cls, value: str) -> str:
        if not __import__("re").match(_ARTIFACT_REF, value):
            raise ValueError("known_hosts_ref must be an artifact reference")
        return value

    @field_validator("initial_credential_ref", "automation_credential_ref")
    @classmethod
    def credential_is_ref(cls, value: str | None) -> str | None:
        if value is not None and not __import__("re").match(_SECRET_REF, value):
            raise ValueError("credential must be a secret reference")
        return value

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not role.strip() for role in value):
            raise ValueError("roles must be non-empty and unique")
        return value


class AuthBinding(EnvironmentModel):
    plane: Literal["ssh", "control_api", "tenant", "sandbox_data", "registry", "store"]
    mode: str = Field(min_length=1)
    credential_ref: SecretRef
    credential_version: str | None = None
    server_targets: tuple[str, ...] = ()
    header_carrier: str | None = None
    tenant_scope: str | None = None
    provision: Literal["reuse", "create_if_absent", "rotate"] = "reuse"
    transport_ref: ArtifactRef | None = None

    @field_validator("credential_ref")
    @classmethod
    def credential_is_ref(cls, value: str) -> str:
        if not __import__("re").match(_SECRET_REF, value):
            raise ValueError("credential_ref must be a secret reference")
        return value


class EnvironmentDeclaration(EnvironmentModel):
    environment_id: str = Field(min_length=1)
    group_id: str = Field(default="default", min_length=1)
    product: Literal["cubesandbox", "kuasar", "agentenv"]
    product_version: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    source_set: tuple[str, ...] = ()
    nodes: tuple[NodeDeclaration, ...] = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    service_endpoints: dict[str, str] = Field(default_factory=dict)
    targets: TargetPolicy = Field(default_factory=TargetPolicy)
    auth: tuple[AuthBinding, ...] = ()
    artifacts: tuple[ArtifactLock, ...] = ()
    policy: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, int | float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope_and_dag(self):
        if any(not name.strip() or not url.startswith(("https://", "http://", "unix://")) for name, url in self.service_endpoints.items()):
            raise ValueError("service endpoints require a name and supported URL scheme")
        node_ids = [node.node_id for node in self.nodes]
        machine_ids = [node.machine_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("node identities must be unique")
        if len(machine_ids) != len(set(machine_ids)):
            raise ValueError("machine identity must be unique; co-locate roles on one node")
        known = set(node_ids)
        graph: dict[str, tuple[str, ...]] = {}
        for node in self.nodes:
            if len(node.depends_on) != len(set(node.depends_on)):
                raise ValueError(f"duplicate dependencies for node {node.node_id}")
            missing = set(node.depends_on) - known
            if missing:
                raise ValueError(f"unknown node dependencies: {sorted(missing)}")
            if node.node_id in node.depends_on:
                raise ValueError("dependency cycle")
            graph[node.node_id] = node.depends_on
        pending = {node: set(deps) for node, deps in graph.items()}
        ready = [node for node, deps in pending.items() if not deps]
        visited: set[str] = set()
        while ready:
            current = ready.pop()
            visited.add(current)
            for node, deps in pending.items():
                if current in deps:
                    deps.remove(current)
                    if not deps and node not in visited:
                        ready.append(node)
        if len(visited) != len(graph):
            raise ValueError("dependency cycle")
        if len({artifact.name for artifact in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact names must be unique")
        if any(value < 0 for value in self.budgets.values()):
            raise ValueError("budgets must be non-negative")
        return self

    @property
    def declaration_hash(self) -> str:
        return canonical_hash(self)


ObservationStatus = Literal[
    "satisfied", "missing", "mismatch", "unknown", "unsupported", "not_applicable"
]


class CheckObservation(EnvironmentModel):
    check_id: str
    rule_ref: str
    node_id: str
    component: str
    status: ObservationStatus
    observed: Any | None = None
    expected: Any | None = None
    unit: str | None = None
    assertions: tuple[dict[str, Any], ...] = ()
    observed_at: str
    boot_id: str | None = None
    evidence_refs: tuple[ArtifactRef, ...] = ()
    repair_code: str | None = None


class EnvironmentReport(EnvironmentModel):
    declaration_hash: str = Field(pattern=_SHA256)
    source_set: tuple[str, ...]
    profile: str
    observations: tuple[CheckObservation, ...]
    readiness: dict[str, str]
    limitations: tuple[str, ...] = ()
    secret_refs: tuple[SecretRef, ...] = ()
    cleanup_refs: tuple[ArtifactRef, ...] = ()
    valid_until: str | None = None
    artifact_manifest: tuple[ArtifactRef, ...] = ()


class ActionStep(EnvironmentModel):
    step_id: str
    action_ref: str
    node_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    input_hash: str = Field(pattern=_SHA256)
    expected_before: dict[str, Any] = Field(default_factory=dict)
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    secret_refs: tuple[SecretRef, ...] = ()
    depends_on: tuple[str, ...] = ()
    reconcile_rule: str
    timeout_seconds: int = Field(gt=0)
    retry_class: Literal["never", "bounded", "reconcile"] = "never"
    compensation_ref: str | None = None
    ownership: str

    @model_validator(mode="after")
    def verify_action_input_hash(self):
        if canonical_hash({"action": self.action_ref, "input": self.input}) != self.input_hash:
            raise ValueError("action input hash mismatch")
        return self


class RepairPlan(EnvironmentModel):
    plan_hash: str = Field(pattern=_SHA256)
    report_ref: ArtifactRef
    declaration_hash: str = Field(pattern=_SHA256)
    steps: tuple[ActionStep, ...] = ()
    conflict_domains: tuple[str, ...]
    revision: int = Field(ge=1)
    deadline: str
    repair_round_budget: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_steps(self):
        ids = {step.step_id for step in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("step identities must be unique")
        if any(set(step.depends_on) - ids for step in self.steps):
            raise ValueError("unknown step dependency")
        return self


class StepReceipt(EnvironmentModel):
    plan_ref: ArtifactRef
    step_id: str
    attempt: int = Field(ge=1)
    remote_handle: str | None = None
    started_at: str
    finished_at: str | None = None
    boot_id: str | None = None
    process_identity: str | None = None
    input_hash: str = Field(pattern=_SHA256)
    output_refs: tuple[ArtifactRef, ...] = ()
    effect_certainty: Literal["none", "confirmed", "unknown"]
    result: Literal["accepted", "running", "completed", "failed", "cancelled", "unknown"]
    evidence_refs: tuple[ArtifactRef, ...] = ()
    recovery_ref: str | None = None


class EnvironmentAcceptance(EnvironmentModel):
    report_manifest_ref: ArtifactRef
    declaration_hash: str = Field(pattern=_SHA256)
    product: str
    product_version: str
    profile: str
    scope: tuple[str, ...]
    credential_versions: dict[str, str]
    valid_until: str
    invalidation_fingerprint: str = Field(pattern=_SHA256)
    functional_verification_refs: tuple[ArtifactRef, ...] = ()
    cleanup_refs: tuple[ArtifactRef, ...] = ()


class ExecutionGrant(EnvironmentModel):
    """Single-use, short-lived authorization for one remote side effect."""

    grant_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    input_hash: str = Field(pattern=_SHA256)
    machine_id: str = Field(min_length=1)
    resource_generations: dict[str, int] = Field(min_length=1)
    run_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    owner_epoch: int = Field(ge=1)
    nonce: str = Field(min_length=32, max_length=128)
    not_before: float
    not_after: float
    max_clock_skew_seconds: float = Field(ge=0, le=2)
    signature: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def verify_interval(self):
        if self.not_after <= self.not_before or self.not_after - self.not_before > 60:
            raise ValueError("execution grant lifetime must be within 60 seconds")
        if any(not domain or generation < 1 for domain, generation in self.resource_generations.items()):
            raise ValueError("resource generations must be positive and scoped")
        return self
