"""Local atomic file replacement with digest CAS and private before-images."""
from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileMutationReceipt:
    path: str
    before_sha256: str | None
    after_sha256: str
    backup_path: str | None
    mode: int
    before_mode: int | None = None


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_target(path: Path) -> tuple[bytes | None, os.stat_result | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("refusing to replace symlink target")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("target must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError("target changed while opening")
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks), opened
    finally:
        os.close(fd)


def _private_backup(directory: Path, data: bytes) -> Path:
    if directory.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        os.chmod(directory, 0o700)
    name = f"{_hash(data)}.before"
    target = directory / name
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            raise RuntimeError("private backup identity conflict")
        return target
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    return target


def replace_file(path: str | Path, content: bytes, *, expected_sha256: str | None,
                 backup_dir: str | Path, mode: int | None = None) -> FileMutationReceipt:
    """CAS replace a regular file; an absent file requires expected_sha256=None."""
    target = Path(path).absolute()
    if target.parent.is_symlink() or target.parent.resolve() != target.parent:
        raise ValueError("target parent path contains symlink or traversal")
    lock_path = target.parent / f".{target.name}.sandbox-graph.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        before, info = _read_target(target)
        actual = _hash(before) if before is not None else None
        if actual != expected_sha256:
            raise RuntimeError("compare-and-swap digest mismatch")
        if info is not None and os.geteuid() != 0 and info.st_uid != os.geteuid():
            raise PermissionError("target is owned by another user")
        backup = _private_backup(Path(backup_dir).absolute(), before) if before is not None else None
        final_mode = mode if mode is not None else (stat.S_IMODE(info.st_mode) if info else 0o640)
        if final_mode & ~0o777:
            raise ValueError("invalid target mode")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.stage-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, final_mode)
            if info is not None and os.geteuid() == 0:
                os.fchown(fd, info.st_uid, info.st_gid)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            current, current_info = _read_target(target)
            current_hash = _hash(current) if current is not None else None
            if current_hash != expected_sha256:
                raise RuntimeError("compare-and-swap digest mismatch before replace")
            if info is not None and current_info is not None and (info.st_dev, info.st_ino) != (current_info.st_dev, current_info.st_ino):
                raise RuntimeError("target identity changed before replace")
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return FileMutationReceipt(str(target), actual, _hash(content), str(backup) if backup else None, final_mode,
            stat.S_IMODE(info.st_mode) if info else None)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def compensate_file(receipt:FileMutationReceipt,*,backup_dir):
    """Restore the original bytes only if the installed bytes still match."""
    target=Path(receipt.path);directory=Path(backup_dir).absolute()
    if target.resolve()!=target or directory.resolve()!=directory:raise ValueError('compensation path unsafe')
    if receipt.before_sha256 is not None:
        backup=Path(receipt.backup_path or '')
        if backup.parent!=directory or backup.resolve()!=backup:raise ValueError('before image outside private backup directory')
        data,info=_read_target(backup)
        if data is None or _hash(data)!=receipt.before_sha256 or info.st_mode&0o077:
            raise ValueError('private before image digest/mode invalid')
        current,_=_read_target(target)
        if current is not None and _hash(current)==receipt.before_sha256:
            return {'status':'completed','reused':True,'path':receipt.path,'restored_sha256':receipt.before_sha256}
        if receipt.before_mode is None:raise ValueError('original file mode unavailable')
        restored=replace_file(target,data,expected_sha256=receipt.after_sha256,backup_dir=directory,mode=receipt.before_mode)
        return {'status':'completed','path':receipt.path,'restored_sha256':restored.after_sha256}
    fd=os.open(target.parent/f'.{target.name}.sandbox-graph.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        current,info=_read_target(target)
        if current is None:return {'status':'completed','reused':True,'path':receipt.path,'restored_sha256':None}
        if _hash(current)!=receipt.after_sha256:raise RuntimeError('compensation compare-and-swap digest mismatch')
        if info.st_uid!=os.geteuid() and os.geteuid()!=0:raise PermissionError('target owner differs')
        target.unlink()
        directory_fd=os.open(target.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
        return {'status':'completed','path':receipt.path,'restored_sha256':None}
    finally:os.close(fd)
