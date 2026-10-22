"""Offline tests for read-only analysis export retrieval (issue: missing Excel)."""
import pytest
from tests.orchestration.conftest import MemoryRepository, FakeClock
from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.analysis_dump import dump_analysis_exports, resolve_analysis_refs


def _commit(tmp_path, scope='r'):
    store = FileArtifactStore(tmp_path / 'artifacts')
    xlsx = store.commit((b'PK\x03\x04 fake xlsx bytes',), scope=scope,
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', producer='t')
    markdown = store.commit((b'# report',), scope=scope, media_type='text/markdown', producer='t')
    files = {'facts.xlsx': xlsx.model_dump(mode='json'), 'report.md': markdown.model_dump(mode='json')}
    return store, files, xlsx, markdown


def test_dump_writes_xlsx_and_markdown(tmp_path):
    repo = MemoryRepository(FakeClock())
    store, files, xlsx, markdown = _commit(tmp_path)
    with repo.transaction() as tx:
        tx.put('analysis_results', 'a1', {'run_id': 'r', 'analysis_input_ref': 'i'})
        tx.put('analysis_exports', 'a1', {'analysis_ref': 'a1', 'files': files})
    written = dump_analysis_exports(repo, store, out=tmp_path / 'out', analysis_ref='a1')
    assert (tmp_path / 'out' / 'facts.xlsx').read_bytes() == b'PK\x03\x04 fake xlsx bytes'
    assert (tmp_path / 'out' / 'report.md').read_bytes() == b'# report'
    assert xlsx.artifact_id in written.values() and markdown.artifact_id in written.values()


def test_resolve_by_run_id_lists_every_analysis_version():
    repo = MemoryRepository(FakeClock())
    with repo.transaction() as tx:
        tx.put('analysis_results', 'a1', {'run_id': 'r'})
        tx.put('analysis_results', 'a2', {'run_id': 'r'})
        tx.put('analysis_results', 'b1', {'run_id': 'other'})
    assert resolve_analysis_refs(repo, run_id='r') == ['a1', 'a2']


def test_multiple_versions_are_split_into_subdirectories(tmp_path):
    repo = MemoryRepository(FakeClock())
    store = FileArtifactStore(tmp_path / 'artifacts')
    for ref in ('a1', 'a2'):
        blob = store.commit((b'x',), scope='r', media_type='text/markdown', producer='t')
        with repo.transaction() as tx:
            tx.put('analysis_results', ref, {'run_id': 'r'})
            tx.put('analysis_exports', ref, {'files': {'report.md': blob.model_dump(mode='json')}})
    written = dump_analysis_exports(repo, store, out=tmp_path / 'out', run_id='r')
    assert {p.split('/')[-2] for p in written} == {'a1', 'a2'}


def test_unknown_run_or_analysis_reports_error(tmp_path):
    repo = MemoryRepository(FakeClock())
    store = FileArtifactStore(tmp_path / 'artifacts')
    with pytest.raises(KeyError):
        dump_analysis_exports(repo, store, out=tmp_path / 'o', run_id='nope')
    with pytest.raises(KeyError):
        dump_analysis_exports(repo, store, out=tmp_path / 'o', analysis_ref='missing')


def test_tampered_blob_is_rejected(tmp_path):
    repo = MemoryRepository(FakeClock())
    store, files, xlsx, _ = _commit(tmp_path)
    with repo.transaction() as tx:
        tx.put('analysis_results', 'a1', {'run_id': 'r'})
        tx.put('analysis_exports', 'a1', {'files': files})
    store.path_for(xlsx).write_bytes(b'tampered')
    with pytest.raises(ValueError, match='digest'):
        dump_analysis_exports(repo, store, out=tmp_path / 'out2', analysis_ref='a1')
