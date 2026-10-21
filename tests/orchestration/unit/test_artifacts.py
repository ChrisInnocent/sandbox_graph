import pytest
from sandbox_graph.artifacts.store import FileArtifactStore


def test_atomic_publication_scope_read_and_tamper_detection(tmp_path):
    store = FileArtifactStore(tmp_path)
    ref = store.commit([b'abc', b'def'], scope='G_A', media_type='text/plain')
    assert store.open_range(ref, scope='G_A', offset=1, length=3) == b'bcd'
    with pytest.raises(PermissionError):
        store.open_range(ref, scope='G_B', offset=0, length=3)
    assert store.commit([b'abcdef'], scope='G_A', media_type='text/plain') == ref
    store.path_for(ref).write_bytes(b'bad')
    with pytest.raises(ValueError, match='digest|size'):
        store.verify(ref)


def test_path_traversal_is_rejected(tmp_path):
    store = FileArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.commit([b'abc'], scope='../outside', media_type='text/plain')


def test_file_success_database_failure_leaves_a_recoverable_orphan(tmp_path):
    store = FileArtifactStore(tmp_path)
    ref = store.commit([b'payload'], scope='G_A', media_type='text/plain')
    # The caller's ledger transaction failed after publication: no reference was written.
    assert store.path_for(ref).exists()          # orphan is recoverable, not data loss
    store.verify(ref)                            # content is intact
    # A missing file must never verify as a valid delivery.
    store.path_for(ref).unlink()
    with pytest.raises(FileNotFoundError):
        store.verify(ref)
    # A tampered file must never verify as a valid delivery.
    ref2 = store.commit([b'original'], scope='G_A', media_type='text/plain')
    store.path_for(ref2).write_bytes(b'altered!')
    with pytest.raises(ValueError):
        store.verify(ref2)
