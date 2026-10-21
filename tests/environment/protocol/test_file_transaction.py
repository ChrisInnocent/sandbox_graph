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
