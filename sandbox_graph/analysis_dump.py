"""只读导出：把某次分析的版本化产物（含 facts.xlsx）落盘。

不能替代归档：只证明产物流水存在且摘要可校验，不证明交付/覆盖完整。
F4 被阻塞时仍可用，用于取回 A4 已生成但未被归档暴露的文件。
"""
from pathlib import Path, PurePosixPath
import json
from .domain.models import EvidenceRef
from .artifacts.store import FileArtifactStore

_ALLOWED_SUFFIX = ('.xlsx', '.json', '.md', '.csv')


def resolve_analysis_refs(repository, *, run_id=None, analysis_ref=None):
    """显式 analysis_ref 优先；否则按 run_id 找该 run 的全部分析版本（排序去重）。"""
    if analysis_ref:
        return [analysis_ref]
    if not run_id:
        raise ValueError('run_id or analysis_ref is required')
    with repository.transaction() as tx:
        return sorted({row['key'] for row in tx.scan('analysis_results')
                       if row['body'].get('run_id') == run_id})


def dump_analysis_exports(repository, store, *, out, run_id=None, analysis_ref=None):
    """把 analysis_exports 引用的每个文件写到 out 目录；返回 {落盘路径: artifact_id}。"""
    refs = resolve_analysis_refs(repository, run_id=run_id, analysis_ref=analysis_ref)
    if not refs:
        raise KeyError('no analysis results for the given run_id/analysis_ref')
    written = {}
    multiple = len(refs) > 1
    for ref in refs:
        with repository.transaction() as tx:
            row = tx.get('analysis_exports', ref)
        if not row:
            continue
        target = Path(out) / (ref[:12] if multiple else '')
        target.mkdir(parents=True, exist_ok=True)
        for name, raw in sorted(row['body']['files'].items()):
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or str(path) != name:
                raise ValueError(f'unsafe export file name: {name}')
            if path.suffix not in _ALLOWED_SUFFIX:
                raise ValueError(f'unexpected export file type: {name}')
            evidence = EvidenceRef.model_validate(raw)
            store.verify(evidence)  # 内容寻址 + 摘要/大小校验，篡改或缺失直接报错
            data = store.path_for(evidence).read_bytes()
            destination = target / name
            destination.write_bytes(data)
            written[str(destination)] = evidence.artifact_id
    if not written:
        raise KeyError('no analysis_exports found for the resolved analysis version')
    return written


def dump_with_root(repository, root, *, out, run_id=None, analysis_ref=None):
    return dump_analysis_exports(repository, FileArtifactStore(root), out=out,
                                 run_id=run_id, analysis_ref=analysis_ref)


def dump_case_evidence(repository, store, *, out, run_id):
    """把某 run 下每个 work 的封存用例证据（数据/原始载荷/资源样本）落盘。

    取出的是 N10 封存、且经 `delivery_finalizer` 入库的同一份证据；
    它就是 A3 统一指标测算的输入，因此可与分析产物对账。
    """
    with repository.transaction() as tx:
        work_ids = {row['key'] for row in tx.scan('work_instances') if row['body'].get('run_id') == run_id}
        manifests = [row['body'] for row in tx.scan('work_deliveries')
                     if set(row['body'].get('work_refs', ())) & work_ids]
    if not work_ids:
        raise KeyError(f'no work instances for run_id={run_id}')
    if not manifests:
        raise KeyError(f'no delivered work evidence for run_id={run_id}')
    written = {}
    multiple = len(manifests) > 1
    for manifest in manifests:
        refs = {ref['artifact_id']: ref for ref in manifest['evidence']}
        index_id = (manifest['result'].get('output') or {}).get('case_index_ref')
        if not index_id or index_id not in refs:
            continue
        index_ref = EvidenceRef.model_validate(refs[index_id])
        store.verify(index_ref)
        index = json.loads(store.path_for(index_ref).read_bytes())
        work = manifest['work_refs'][0]
        target = Path(out) / (work if multiple else '')
        target.mkdir(parents=True, exist_ok=True)
        for name, raw in sorted(index['paths'].items()):
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or str(path) != name:
                raise ValueError(f'unsafe evidence name: {name}')
            evidence = EvidenceRef.model_validate(raw)
            if evidence.artifact_id not in refs:
                raise ValueError('index references evidence outside the delivery')
            store.verify(evidence)
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(store.path_for(evidence).read_bytes())
            written[str(destination)] = evidence.artifact_id
    if not written:
        raise KeyError('no sealed case evidence found for the requested run')
    return written


def dump_cases_with_root(repository, root, *, out, run_id):
    return dump_case_evidence(repository, FileArtifactStore(root), out=out, run_id=run_id)
