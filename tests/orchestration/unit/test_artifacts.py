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
