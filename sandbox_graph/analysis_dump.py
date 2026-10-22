"""只读导出：把某次分析的版本化产物（含 facts.xlsx）落盘。

不能替代归档：只证明产物流水存在且摘要可校验，不证明交付/覆盖完整。
F4 被阻塞时仍可用，用于取回 A4 已生成但未被归档暴露的文件。
"""
from pathlib import Path, PurePosixPath
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
