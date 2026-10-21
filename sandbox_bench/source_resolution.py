"""来源产物身份解析与方法路由（BCD-12 §12.2／§12.4）。

决定实验方法身份的是**产物实际形态 + 解析出的启动路径 + 生产步骤**，不是接口参数名。
凡不能证明的一律 `unverified`；未覆盖组合直接 `ScenarioBlocked`，绝不猜测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .contracts import Source, SourceResolution

# 已审计的 Kuasar 可移植模板 id kind 标记（docs/source-audit/alignment.md §8）。
_KUASAR_MARKERS = {
    'img': ('image_artifact', 'cold_boot'),
    'sbx': ('sandbox_artifact', 'cold_boot'),
    'snp': ('snapshot_manifest', 'memory_restore'),
}


def _blocked(reason: str):
    # 延迟导入避免与 scenarios.py 的模块级依赖形成环。
    from .scenarios import ScenarioBlocked
    raise ScenarioBlocked(reason)


@dataclass(frozen=True)
class Resolution:
    artifact_kind: str | None
    launch_path: str | None
    producer_step: str
    artifact_id: str
    artifact_digest: str | None
    producer_ref: str | None
    spec_origin: str
    path_evidence: str
    status: str  # 'resolved' | 'unverified'


def kuasar_template_kind(source_id: str) -> tuple[str, str] | None:
    """从 Kuasar 可移植模板 id 解析出 (artifact_kind, launch_path)，无法确定返回 None。"""
    if not isinstance(source_id, str):
        return None
    for marker, identity in _KUASAR_MARKERS.items():
        if f'-{marker}-' in source_id:
            return identity
    return None


def resolve_source(*, product: str, product_version: str, parameter_name: str,
                   declared_kind: str, source_id: str, digest: str | None,
                   probe: Callable[[str], dict] | None, evidence_ref: str,
                   declared_artifact_kind: str | None = None,
                   declared_launch_path: str | None = None) -> Resolution:
    """只读解析来源身份。缺版本或无法证明时返回 `unverified`，不按名称推断。"""
    if not product_version or not isinstance(source_id, str) or not source_id:
        return Resolution(None, None, 'pre_existing', source_id, digest, None,
                          'request', evidence_ref, 'unverified')
    observed: dict = {}
    if probe is not None:
        try:
            value = probe(source_id)
        except Exception:
            value = None
        if isinstance(value, dict):
            observed = value
    artifact_kind = observed.get('artifact_kind') or declared_artifact_kind
    launch_path = observed.get('launch_path') or declared_launch_path
    producer_step = observed.get('producer_step') or 'pre_existing'
    spec_origin = observed.get('spec_origin')
    producer_ref = observed.get('producer_ref')
    path_evidence = observed.get('path_evidence') or evidence_ref
    declared = observed.get('declared_kind') or declared_kind
    # 已审计的产品确定性：仅在 checkout 证据支持处补全，不做通用名称猜测。
    if artifact_kind is None and product == 'agentenv' and declared in ('template', 'snapshot'):
        # AgentENV 的 `template_id` 实为 snapshot id，温启走内存恢复并继承规格。
        artifact_kind, launch_path = 'snapshot_manifest', launch_path or 'memory_restore'
        spec_origin = spec_origin or 'inherited'
    if artifact_kind is None and product == 'agentenv' and declared == 'image':
        artifact_kind, launch_path = 'image_artifact', launch_path or 'cold_boot'
    if artifact_kind is None and product == 'kuasar' and declared == 'template':
        identity = kuasar_template_kind(source_id)
        if identity is not None:
            artifact_kind, launch_path = identity[0], launch_path or identity[1]
    if artifact_kind is None or launch_path is None:
        return Resolution(None, None, producer_step, source_id, digest, producer_ref,
                          spec_origin or 'request', path_evidence, 'unverified')
    return Resolution(artifact_kind, launch_path, producer_step, source_id, digest,
                      producer_ref, spec_origin or 'request', path_evidence, 'resolved')


def method_for_source(res: Resolution, *, cost_scope: str, measuring_build: bool = False,
                      family: str | None = None) -> str:
    """按 BCD-12 §12.2 决策表路由；未覆盖组合与同族冲突不静默。"""
    if res.status != 'resolved' or res.artifact_kind is None or res.launch_path is None:
        _blocked('unresolved_source_identity')
    if measuring_build:
        return ('B00.SNAPSHOT_BUILD' if res.artifact_kind == 'snapshot_manifest'
                else 'B00.TEMPLATE_BUILD')
    if res.artifact_kind == 'image_artifact' and res.launch_path == 'cold_boot':
        if res.producer_step == 'build_job' and cost_scope == 'build_included':
            return 'B02.FULL'
        if res.producer_step == 'pre_existing' and cost_scope == 'prepared':
            if family == 'template':
                return 'B01.TEMPLATE'
            if family == 'image':
                return 'B02.PREPARED'
            _blocked('unresolved_source_identity')  # 同族冲突须显式声明族别
    if (res.artifact_kind == 'sandbox_artifact' and res.launch_path == 'cold_boot'
            and res.producer_step == 'pre_existing' and cost_scope == 'prepared'):
        return 'B01.TEMPLATE'
    if res.artifact_kind == 'snapshot_manifest' and cost_scope == 'prepared':
        if res.launch_path == 'memory_restore':
            return 'B01.SNAPSHOT.MEMORY'
        if res.launch_path in ('cold_boot', 'disk_restore'):
            return 'B01.SNAPSHOT.DISK'
    _blocked('unresolved_source_identity')


def source_from_resolution(res: Resolution, method_id: str) -> Source:
    """把解析结果回写为用例 `Source`，保留全部身份维度与追溯引用。"""
    if method_id.startswith(('B01.SNAPSHOT', 'B00.SNAPSHOT_BUILD')):
        kind = 'snapshot'
    elif method_id.startswith(('B01.TEMPLATE', 'B00.TEMPLATE_BUILD')):
        kind = 'template'
    elif method_id.startswith('B02'):
        kind = 'image'
    elif res.artifact_kind == 'snapshot_manifest':
        kind = 'snapshot'
    elif res.artifact_kind == 'image_artifact':
        kind = 'image'
    else:
        kind = 'template'
    return Source(kind=kind, id=res.artifact_id, digest=res.artifact_digest or 'unknown',
                  artifact_kind=res.artifact_kind, launch_path=res.launch_path,
                  producer_step=res.producer_step, producer_ref=res.producer_ref)


def to_source_resolution(res: Resolution, *, product: str, product_version: str,
                         parameter_name: str, declared_kind: str) -> SourceResolution:
    return SourceResolution(
        schema_version='source.v2', product=product, product_version=product_version,
        parameter_name=parameter_name, declared_kind=declared_kind,
        artifact_kind=res.artifact_kind, launch_path=res.launch_path,
        producer_step=res.producer_step, artifact_id=res.artifact_id,
        artifact_digest=res.artifact_digest, producer_ref=res.producer_ref,
        spec_origin=res.spec_origin, path_evidence=res.path_evidence,
        resolution_status=res.status)
