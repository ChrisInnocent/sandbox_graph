import os

import pytest

from sandbox_graph.environment.secrets import SecretStore


def test_create_if_absent_is_atomic_and_reuses_same_secret_version(tmp_path):
    store = SecretStore(tmp_path / "secret-root")
    calls = []
    first = store.create_if_absent("cube-lab", "control-api", lambda: calls.append(1) or "key-value-1")
    second = store.create_if_absent("cube-lab", "control-api", lambda: calls.append(2) or "key-value-2")
    assert first.ref == second.ref == "secret://cube-lab/control-api/v1"
    assert store.resolve(first.ref, scope="cube-lab") == "key-value-1"
    assert calls == [1]
    assert first.created is True and second.created is False


def test_secret_files_are_private_and_scope_is_enforced(tmp_path):
    store = SecretStore(tmp_path / "secret-root")
    item = store.put("group-a", "api", "do-not-print")
    path = store.path_for(item.ref)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent.parent).st_mode & 0o777 == 0o700
    with pytest.raises(PermissionError):
        store.resolve(item.ref, scope="group-b")


def test_secret_store_refuses_overwrite_and_symlinked_values(tmp_path):
    store = SecretStore(tmp_path / "secret-root")
    item = store.put("group-a", "api", "first")
    with pytest.raises(RuntimeError, match="already exists"):
        store.put("group-a", "api", "second")
    path = store.path_for(item.ref)
    path.unlink()
    target = tmp_path / "outside"; target.write_text("secret")
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        store.resolve(item.ref, scope="group-a")
