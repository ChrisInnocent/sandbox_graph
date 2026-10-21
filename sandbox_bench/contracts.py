"""版本化测试输入。缺少真实实验条件时拒绝，不从工程夹具推导默认值。"""
from __future__ import annotations
import hashlib
import json
from typing import Annotated, Literal
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Count = Annotated[int, Field(gt=0, strict=True)]
# JSON/YAML carry arrays as lists; strict models must still accept them for tuple fields.
StrTuple = Annotated[tuple[str, ...], BeforeValidator(lambda v: tuple(v) if isinstance(v, list) else v)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False)

    def content_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode='json'), sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class SandboxSpec(StrictModel):
    vcpus: Positive
    memory_bytes: Count
    disk_bytes: Count | None = None


# Source identity dimensions (BCD-12 §12.1). The judge of an experiment method is the
# produced artifact kind and the resolved launch path, never the interface parameter name.
SourceArtifactKind = Literal['image_artifact', 'sandbox_artifact', 'snapshot_manifest']
LaunchPath = Literal['cold_boot', 'memory_restore', 'disk_restore']
ProducerStep = Literal['pre_existing', 'build_job', 'capture_publish']
CostScope = Literal['build_included', 'prepared']

# Creation entry topology and completion endpoint (BCD-13 §13.1). Synchrony is a property
# of the entry and the audited product behaviour, never inferred from the parameter name.
Topology = Literal['router', 'node_direct', 'co_located_cli']
EndpointKind = Literal['api_complete', 'state_ready', 'server_terminal', 'exec_complete', 'guest_ready']
ReadinessSignal = Literal['none', 'poll_state', 'ready_fd']


class Source(StrictModel):
    kind: Literal['template', 'snapshot', 'image']
    id: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    # Optional identity dimensions; required by validate_method for B00./B01. methods.
    artifact_kind: SourceArtifactKind | None = None
    launch_path: LaunchPath | None = None
    producer_step: ProducerStep | None = None
    producer_ref: str | None = None
    resolution_ref: str | None = None


class SourceResolution(StrictModel):
    """Read-only resolved source identity (schema source.v2).

    `unverified` is allowed to carry no kind/path; downstream must not treat it as
    known. A `resolved` record must name both the artifact kind and launch path.
    """
    schema_version: Literal['source.v2']
    product: Literal['cubesandbox', 'kuasar', 'agentenv']
    product_version: str
    parameter_name: str
    declared_kind: Literal['template', 'snapshot', 'image']
    artifact_kind: SourceArtifactKind | None = None
    launch_path: LaunchPath | None = None
    producer_step: ProducerStep
    artifact_id: str = Field(min_length=1)
    artifact_digest: str | None = None
    producer_ref: str | None = None
    spec_origin: Literal['request', 'inherited']
    path_evidence: str = Field(min_length=1)
    resolution_status: Literal['resolved', 'unverified']

    @model_validator(mode='after')
    def resolved_identity(self):
        if self.resolution_status == 'resolved' and (self.artifact_kind is None or self.launch_path is None):
            raise ValueError('resolved source requires artifact kind and launch path')
        return self


class CompletionContract(StrictModel):
    endpoint: Literal['API_COMPLETE', 'STATE_READY', 'SERVER_TERMINAL', 'EXEC_COMPLETE']
    timeout_s: Positive
    poll_interval_s: Positive
    target_state: str = 'running'
    validation_policy: Literal['none', 'batch_after', 'stream_after'] = 'none'
    command: str | None = None
    expected_stdout: str | None = None
    expected_exit: int = 0

    @model_validator(mode='after')
    def executable(self):
        if (self.endpoint == 'EXEC_COMPLETE' or self.validation_policy != 'none') and (
                not self.command or self.expected_stdout is None):
            raise ValueError('command completion/validation requires explicit command and output')
        return self


class LoadContract(StrictModel):
    mode: Literal['WINDOW', 'BATCH', 'OPEN_LOOP']
    requests: Count
    concurrency: Count
    run_timeout_s: Positive
    max_unknown_external: Annotated[int, Field(ge=0, strict=True)]
    batch_size: Count | None = None
    batch_gap_s: Nonnegative | None = None
    arrival_rate: Positive | None = None
    max_queue: Annotated[int, Field(ge=0, strict=True)] | None = None
    late_policy: Literal['drop', 'queue'] | None = None

    @model_validator(mode='after')
    def conditional(self):
        if self.mode == 'BATCH' and (self.batch_size is None or self.batch_gap_s is None):
            raise ValueError('batch requires size and gap')
        if self.mode == 'OPEN_LOOP' and any(x is None for x in (
                self.arrival_rate, self.max_queue, self.late_policy)):
            raise ValueError('open loop requires arrival rate and finite queue policy')
        return self


class ResourceContract(StrictModel):
    interval_s: Positive
    window_s: Positive
    max_wait_s: Positive
    max_gap_s: Positive
    min_samples: Count
    inventory_ref: str
    required_metrics: StrTuple = ('pss_bytes', 'cpu_seconds')
    guest_deduction_ref: str | None = None


class DensityContract(StrictModel):
    target_node: str
    initial_count: Annotated[int, Field(ge=0, strict=True)]
    batch_size: Count | None = None
    min_growth: Count | None = None
    stall_limit: Count | None = None
    max_batches: Count | None = None
    max_instances: Count | None = None
    batch_gap_s: Nonnegative | None = None
    reserve_bytes: Annotated[int, Field(ge=0, strict=True)] | None = None
    slo: dict[str, float] | None = None


class BackgroundConditions(StrictModel):
    """Structured background/condition inputs (RT-45, BCD-16 §16.4).

    Required per-scenario conditions must be present; optional-not-set is `not_selected`.
    """
    existing_count: Annotated[int, Field(ge=0, strict=True)] = 0            # N-01
    target_count: Annotated[int, Field(ge=0, strict=True)] | None = None     # N-02
    background_count: Annotated[int, Field(ge=0, strict=True)] = 0           # N-03
    instance_state: Literal['idle', 'active', 'paused', 'mixed'] | None = None  # N-04
    warm_pool_depth: Annotated[int, Field(ge=0, strict=True)] | None = None  # W-01
    spec_origin: Literal['request', 'inherited'] = 'request'                 # SP-02
    effective_spec_ref: str | None = None                                    # SP-03
    artifact_size_bytes: Annotated[int, Field(ge=0, strict=True)] | None = None  # D-01
    dirty_bytes: Annotated[int, Field(ge=0, strict=True)] | None = None      # D-02
    snapshot_chain_depth: Annotated[int, Field(ge=0, strict=True)] | None = None  # D-03
    interference_ref: str | None = None                                      # L-02
    warmup_s: Nonnegative | None = None                                      # TL-04


class CaseContract(StrictModel):
    def digest(self) -> str:
        return self.content_hash()

    schema_version: Literal['case.v1']
    method_id: str
    method_version: str
    repeat_id: Count = 1
    product: Literal['cubesandbox', 'kuasar', 'agentenv']
    product_version: str
    work_id: str
    group_id: str
    source: Source
    spec: SandboxSpec
    completion: CompletionContract
    load: LoadContract
    cleanup_timeout_s: Positive
    ttl_s: Positive
    cache_profile: str
    environment_ref: str
    contract_ref: str
    graph_ref: str
    plan_ref: str
    resources: ResourceContract | None = None
    density: DensityContract | None = None
    clone_count: Count | None = None
    source_must_run: bool = False
    fixture_ref: str | None = None
    target_node: str | None = None
    workload_ref: str | None = None
    # Per-scenario selection (RT-44/RT-45, BCD-15/BCD-16); validated by scenario_spec.
    declared_metrics: StrTuple = ()
    declared_conditions: StrTuple = ()
    background: BackgroundConditions | None = None
    # Creation endpoint identity (BCD-13). Optional; validated for consistency when present.
    topology: Topology | None = None
    endpoint_kind: EndpointKind | None = None
    readiness_signal: ReadinessSignal | None = None
    endpoint_ref: str | None = None

    @model_validator(mode='after')
    def validate_method(self):
        from .registry import METHODS
        if self.method_id not in METHODS:
            raise ValueError('unknown method')
        if self.method_id.startswith(('B01.', 'B00.')):
            if (self.source.artifact_kind is None or self.source.launch_path is None
                    or self.source.producer_step is None):
                raise ValueError('B01/B00 source requires artifact_kind, launch_path and producer_step')
            if self.method_id == 'B01.SNAPSHOT.MEMORY' and self.source.launch_path != 'memory_restore':
                raise ValueError('memory snapshot method requires memory_restore')
            if self.method_id == 'B01.SNAPSHOT.DISK' and self.source.launch_path not in ('disk_restore', 'cold_boot'):
                raise ValueError('disk snapshot method requires disk or cold restore')
            if self.method_id == 'B01.TEMPLATE' and self.source.artifact_kind == 'snapshot_manifest':
                raise ValueError('template method cannot use snapshot manifest')
            if self.method_id == 'B00.SNAPSHOT_BUILD' and self.source.artifact_kind != 'snapshot_manifest':
                raise ValueError('snapshot build must produce a snapshot manifest')
            if self.method_id == 'B00.TEMPLATE_BUILD' and self.source.artifact_kind == 'snapshot_manifest':
                raise ValueError('template build cannot produce a snapshot manifest')
        endpoint = self.completion.endpoint
        allowed = {'API_COMPLETE','STATE_READY','SERVER_TERMINAL','EXEC_COMPLETE'}
        if self.method_id.startswith(('B03.','B06.')):
            allowed = {'API_COMPLETE','STATE_READY'}
            if self.completion.validation_policy != 'none':
                raise ValueError('non-waking methods cannot run command postvalidation')
        elif self.method_id.startswith('B07.'):
            allowed = {'EXEC_COMPLETE'}
        elif self.method_id == 'B08.LOCAL':
            allowed = {'API_COMPLETE'}
        elif self.method_id == 'B08.PUBLISHED':
            allowed = {'STATE_READY'}
        elif self.method_id.startswith(('B09.','B10.')):
            allowed = {'STATE_READY'}
        if endpoint not in allowed:
            raise ValueError('method completion endpoint mismatch')
        if endpoint == 'SERVER_TERMINAL' and (self.product != 'kuasar' or
                not self.method_id.startswith(('B01.','B02.','B04.','B05.'))):
            raise ValueError('method completion has no audited server terminal provider')
        if self.ttl_s <= self.load.run_timeout_s + self.completion.timeout_s + self.cleanup_timeout_s:
            raise ValueError('TTL must cover measurement, drain and cleanup, or use a separately verified renewal profile')
        if self.method_id.startswith(('B09.', 'B10.')) and self.resources is None:
            raise ValueError('noise/density requires resource observation contract')
        if self.method_id.startswith('B10.') and self.density is None:
            raise ValueError('density requires node/cohort contract')
        if self.method_id.startswith('B10.GROWTH') and any(getattr(self.density, x) is None for x in (
                'batch_size', 'min_growth', 'stall_limit', 'max_batches', 'max_instances', 'batch_gap_s')):
            raise ValueError('growth stop and resource budgets required')
        if self.method_id == 'B10.PROJECTION' and self.density.reserve_bytes is None:
            raise ValueError('projection reserve required')
        if self.method_id == 'B10.SLO_VERIFY' and not self.density.slo:
            raise ValueError('SLO thresholds required')
        if self.method_id.endswith('.BATCH') and self.load.mode != 'BATCH':
            raise ValueError('method/load mismatch')
        if self.method_id.endswith('.WINDOW') and self.load.mode != 'WINDOW':
            raise ValueError('method/load mismatch')
        if self.method_id.startswith('B05.') and (self.clone_count is None or
                self.clone_count > self.load.concurrency or self.clone_count > self.load.requests
                or self.load.requests % self.clone_count):
            raise ValueError('fork must atomically fit child concurrency and budget')
        if self.method_id.startswith('E') and self.workload_ref is None:
            raise ValueError('extension requires explicitly selected workload profile')
        if self.topology == 'co_located_cli':
            if self.readiness_signal != 'ready_fd' or self.endpoint_kind != 'guest_ready':
                raise ValueError('co-located cli requires ready_fd and guest_ready')
        elif self.topology in ('router', 'node_direct'):
            if self.readiness_signal == 'ready_fd' or self.endpoint_kind == 'guest_ready':
                raise ValueError('remote topology cannot use ready_fd or guest_ready')
        if self.endpoint_kind == 'guest_ready' and self.readiness_signal != 'ready_fd':
            raise ValueError('guest_ready endpoint requires ready_fd readiness')
        return self


class MetricValue(StrictModel):
    name: str
    value: float | None
    unit: str
    status: Literal['valid', 'invalid', 'insufficient', 'unavailable', 'not_applicable']
    reason: str | None = None
    scope: str
    window: tuple[float, float] | None = None
    cohort_ref: str | None = None
    sample_count: Annotated[int, Field(ge=0, strict=True)]
    formula_version: str
    evidence_refs: tuple[str, ...]

    @model_validator(mode='after')
    def valid_value(self):
        if self.status != 'valid' and self.value is not None:
            raise ValueError('invalid/unavailable metric cannot carry a numeric value')
        if self.status == 'valid' and (self.value is None or not self.evidence_refs):
            raise ValueError('valid metric requires value and evidence')
        return self


Identifier = Annotated[str, Field(min_length=1)]
Counter = Annotated[int, Field(ge=0, strict=True)]


class EventEnvelope(StrictModel):
    schema_version: Literal['event.v1']
    event_id: Identifier
    seq: Count
    work_id: Identifier
    operation_id: Identifier
    parent_operation_id: Identifier | None = None
    request_index: Counter | None = None
    stage: Identifier
    event_kind: Identifier
    clock_domain: Identifier
    process_epoch: Identifier
    monotonic_ns: Counter | None = None
    wall_ns: Counter | None = None
    source_event_time: str | None = None
    observed_at_ns: Counter
    payload_ref: Identifier
    validity: Literal['valid','invalid','unverified']


class ResourceIdentity(StrictModel):
    environment: Identifier
    node: Identifier
    boot_id: Identifier
    pid: Count
    start_ticks: Counter


class ResourceSample(StrictModel):
    schema_version: Literal['resource.v1']
    identity: ResourceIdentity
    roles: tuple[Identifier, ...]
    components: tuple[Identifier, ...]
    scope: Literal['process','cgroup']
    cgroup_ref: Identifier | None = None
    sandbox_id: Identifier | None = None
    sandbox_generation: Identifier | None = None
    clock_domain: Identifier
    process_epoch: Identifier
    observed_at_ns: Counter
    pss_bytes: Counter | None
    cpu_seconds: Nonnegative | None
    status: Literal['valid','partial','unavailable']
    reason: str | None = None

    @model_validator(mode='after')
    def coverage(self):
        if not self.roles or not self.components:raise ValueError('role/component inventory required')
        if self.sandbox_id is not None and self.sandbox_generation is None:raise ValueError('sandbox generation required')
        if self.scope=='cgroup' and self.cgroup_ref is None:raise ValueError('cgroup identity required')
        if self.status=='valid' and (self.pss_bytes is None or self.cpu_seconds is None):raise ValueError('valid sample lacks counters')
        if self.status=='unavailable' and (self.pss_bytes is not None or self.cpu_seconds is not None):raise ValueError('unavailable counters cannot carry values')
        return self
