"""Private local secret store with scoped refs and idempotent creation."""
from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class SecretItem:
    ref: str
    created: bool


def generate_api_secret() -> str:
    return secrets.token_urlsafe(48)


class SecretStore:
    """Filesystem-backed secret store; use shared external storage across hosts."""

    def __init__(self, root: str | Path):
        raw_root = Path(root).absolute()
        if raw_root.is_symlink() or raw_root.parent.resolve() != raw_root.parent:
            raise ValueError("secret root must not be a symlink path")
        raw_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise ValueError("secret root must be a real directory")
        if raw_root.stat().st_uid != os.geteuid():
            raise PermissionError("secret root must be owned by current user")
        os.chmod(raw_root, 0o700)
        self.root = raw_root

    @staticmethod
    def _validate_segment(value: str, name: str) -> str:
        if not _SEGMENT.fullmatch(value) or value in {".", ".."}:
            raise ValueError(f"invalid secret {name}")
        return value

    def _directory(self, scope: str, name: str, *, create: bool) -> Path:
        scope = self._validate_segment(scope, "scope")
        name = self._validate_segment(name, "name")
        scope_dir = self.root / scope
        name_dir = scope_dir / name
        if create:
            scope_dir.mkdir(mode=0o700, exist_ok=True)
            name_dir.mkdir(mode=0o700, exist_ok=True)
        for directory in (scope_dir, name_dir):
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("secret path contains symlink or non-directory")
            if directory.stat().st_uid != os.geteuid():
                raise PermissionError("secret directory must be owned by current user")
            if stat.S_IMODE(directory.stat().st_mode) & 0o077:
                os.chmod(directory, 0o700)
        return name_dir

    def path_for(self, ref: str) -> Path:
        match = re.fullmatch(r"secret://([^/]+)/([^/]+)/([^/]+)", ref)
        if not match:
            raise ValueError("invalid secret reference")
        scope, name, version = match.groups()
        self._validate_segment(version, "version")
        return self._directory(scope, name, create=False) / version

    def _write_new(self, path: Path, value: str) -> None:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 65536:
            raise ValueError("secret value must contain 1..65536 UTF-8 bytes")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            payload = value.encode("utf-8")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def put(self, scope: str, name: str, value: str, *, version: str = "v1") -> SecretItem:
        version = self._validate_segment(version, "version")
        directory = self._directory(scope, name, create=True)
        path = directory / version
        try:
            self._write_new(path, value)
        except FileExistsError as exc:
            raise RuntimeError("secret version already exists; explicit rotation is required") from exc
        return SecretItem(f"secret://{scope}/{name}/{version}", True)

    def create_if_absent(self, scope: str, name: str, factory: Callable[[], str], *, version: str = "v1") -> SecretItem:
        version = self._validate_segment(version, "version")
        directory = self._directory(scope, name, create=True)
        path = directory / version
        lock_path = directory / f".{version}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if path.exists() or path.is_symlink():
                self._read(path)
                return SecretItem(f"secret://{scope}/{name}/{version}", False)
            self._write_new(path, factory())
            return SecretItem(f"secret://{scope}/{name}/{version}", True)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, path: Path) -> str:
        if path.is_symlink():
            raise ValueError("secret value must not be a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise PermissionError("secret file ownership or type is invalid")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError("secret file permissions must be 0600")
            chunks = []
            while chunk := os.read(fd, 8192):
                chunks.append(chunk)
            value = b"".join(chunks)
            if not value or len(value) > 65536:
                raise ValueError("secret file has invalid size")
            return value.decode("utf-8")
        finally:
            os.close(fd)

    def resolve(self, ref: str, *, scope: str) -> str:
        path = self.path_for(ref)
        if ref.split("/")[2] != scope:
            raise PermissionError("secret reference scope mismatch")
        return self._read(path)
