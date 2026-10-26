import hashlib
import os
from pathlib import Path

import pytest

from sandbox_graph.environment.file_transaction import replace_file


def digest(data): return hashlib.sha256(data).hexdigest()


def test_replace_file_checks_expected_hash_and_preserves_mode(tmp_path):
    target = tmp_path / "service.conf"; target.write_bytes(b"old")
    target.chmod(0o640)
    backups = tmp_path / "private-backups"
    receipt = replace_file(target, b"new", expected_sha256=digest(b"old"), backup_dir=backups)
    assert target.read_bytes() == b"new"
    assert receipt.before_sha256 == digest(b"old")
    assert receipt.after_sha256 == digest(b"new")
    assert os.stat(target).st_mode & 0o777 == 0o640
    assert Path(receipt.backup_path).read_bytes() == b"old"
    assert os.stat(backups).st_mode & 0o777 == 0o700


def test_replace_file_rejects_drift_and_leaves_target_unchanged(tmp_path):
    target = tmp_path / "service.conf"; target.write_bytes(b"changed-by-owner")
    with pytest.raises(RuntimeError, match="compare-and-swap"):
        replace_file(target, b"new", expected_sha256=digest(b"old"), backup_dir=tmp_path / "backups")
    assert target.read_bytes() == b"changed-by-owner"


def test_replace_file_rejects_symlink_and_non_regular_targets(tmp_path):
    actual = tmp_path / "real"; actual.write_bytes(b"old")
    link = tmp_path / "linked"; link.symlink_to(actual)
    with pytest.raises(ValueError, match="symlink"):
        replace_file(link, b"new", expected_sha256=digest(b"old"), backup_dir=tmp_path / "backups")


def test_compensation_restores_bytes_and_original_mode_and_is_idempotent(tmp_path):
    from sandbox_graph.environment.file_transaction import compensate_file
    target=tmp_path/'server.conf';target.write_bytes(b'old');target.chmod(0o640)
    receipt=replace_file(target,b'new',expected_sha256=hashlib.sha256(b'old').hexdigest(),backup_dir=tmp_path/'backup',mode=0o600)
    assert compensate_file(receipt,backup_dir=tmp_path/'backup')['status']=='completed'
    assert target.read_bytes()==b'old' and target.stat().st_mode&0o777==0o640
    assert compensate_file(receipt,backup_dir=tmp_path/'backup')['reused']


def test_compensation_preserves_external_changes_and_removes_only_owned_new_file(tmp_path):
    from sandbox_graph.environment.file_transaction import compensate_file
    target=tmp_path/'new.conf'
    receipt=replace_file(target,b'new',expected_sha256=None,backup_dir=tmp_path/'backup')
    target.write_bytes(b'external')
    with pytest.raises(RuntimeError,match='digest'):compensate_file(receipt,backup_dir=tmp_path/'backup')
    assert target.read_bytes()==b'external'
    target.write_bytes(b'new')
    assert compensate_file(receipt,backup_dir=tmp_path/'backup')['status']=='completed'
    assert not target.exists()
    assert compensate_file(receipt,backup_dir=tmp_path/'backup')['reused']
