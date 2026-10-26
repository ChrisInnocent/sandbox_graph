"""Crash-consistent journal for idempotent remote actions."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from ..domain.models import canonical_hash


class JournalConflict(RuntimeError):
    pass


class OperationJournal:
    TERMINAL = {"completed", "failed", "cancelled", "unknown"}
    TRANSITIONS = {"accepted": {"started", "cancelled", "unknown"},
                   "started": {"completed", "failed", "cancelled", "unknown"}}

    def __init__(self, path: str | Path, *, machine_id: str, boot_id: str | None):
        self.path = Path(path).absolute()
        self.machine_id = machine_id
        self.boot_id = boot_id
        if not machine_id:
            raise ValueError("machine identity is required")
        if self.path.parent.is_symlink() or self.path.parent.resolve() != self.path.parent:
            raise ValueError("journal directory must not contain symlinks")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _load(self) -> dict[str, Any]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {}
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise JournalConflict("journal path is unsafe")
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid():
            raise JournalConflict("journal ownership or mode is unsafe")
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw, object_pairs_hook=self._unique_pairs)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except Exception as exc:
            raise JournalConflict("journal is corrupt") from exc

    @staticmethod
    def _unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def _locked(self):
        if self.lock_path.is_symlink():
            raise JournalConflict("journal lock is unsafe")
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _save(self, records: dict[str, Any]) -> None:
        payload = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.stage-", dir=self.path.parent)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def get(self, operation_id: str) -> dict[str, Any] | None:
        """Observe the original immutable input and durable transition; no replay."""
        fd=self._locked()
        try:return self._load().get(operation_id)
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)

    def accept(self, *, operation_id: str, action: str, input_data: dict[str, Any]) -> dict[str, Any]:
        if not operation_id or len(operation_id) > 128 or not action.endswith(".v1"):
            raise ValueError("operation or action identity is invalid")
        input_hash = canonical_hash({"action": action, "input": input_data})
        fd = self._locked()
        try:
            records = self._load()
            if operation_id in records:
                record = records[operation_id]
                if record.get("input_hash") != input_hash or record.get("action") != action:
                    raise JournalConflict("operation id input identity conflict")
                return record
            record = {"operation_id": operation_id, "action": action, "input_hash": input_hash,
                      "state": "accepted", "machine_id": self.machine_id, "boot_id": self.boot_id,
                      "accepted_at": time.time(), "process_identity": None, "result": None}
            records[operation_id] = record
            self._save(records)
            return record
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def transition(self, operation_id: str, *, state: str,
                   process_identity: dict[str, Any] | None = None,
                   result: dict[str, Any] | None = None) -> dict[str, Any]:
        if state not in self.TERMINAL | {"started"}:
            raise ValueError("invalid journal state")
        fd = self._locked()
        try:
            records = self._load()
            record = records.get(operation_id)
            if record is None:
                raise JournalConflict("operation was not accepted")
            if state == record["state"]:
                return record
            if state not in self.TRANSITIONS.get(record["state"], set()):
                raise JournalConflict("operation is terminal or transition is invalid")
            record = dict(record)
            record.update(state=state, updated_at=time.time())
            if state == "started":
                if not process_identity or not isinstance(process_identity.get("pid"), int) or not isinstance(process_identity.get("start_ticks"), int):
                    raise ValueError("started state requires PID and process start ticks")
                record["process_identity"] = process_identity
                record["boot_id"] = self.boot_id
            if state in self.TERMINAL:
                record["result"] = result
            records[operation_id] = record
            self._save(records)
            return record
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def bind_execution(self,operation_id,execution):
        """Attach the actual child identity once, before observing command output."""
        fd=self._locked()
        try:
            records=self._load();row=records.get(operation_id)
            if not row or row['state']!='started':raise JournalConflict('execution requires a started original operation')
            if row.get('execution') not in (None,execution):raise JournalConflict('execution identity already bound')
            if not isinstance(execution.get('pid'),int) or not isinstance(execution.get('start_ticks'),int):
                raise ValueError('exact process identity required')
            records[operation_id]=row|{'execution':execution};self._save(records)
        finally:os.close(fd)

    def mark_cancel(self,operation_id,input_hash):
        fd=self._locked()
        try:
            records=self._load();row=records.get(operation_id)
            if not row or row['input_hash']!=input_hash:raise JournalConflict('cancel input identity differs')
            if row['state'] not in {'completed','failed','cancelled'}:
                records[operation_id]=row|{'cancel_requested':True};self._save(records)
        finally:os.close(fd)

    def finish_cancel(self,operation_id,input_hash):
        """Trusted caller holds host lock and has observed original children gone."""
        fd=self._locked()
        try:
            records=self._load();row=records.get(operation_id)
            if not row or row['input_hash']!=input_hash or not row.get('cancel_requested'):
                raise JournalConflict('original cancel intent required')
            if row['state'] in {'completed','failed','cancelled'}:return row
            value=row|{'state':'cancelled','updated_at':time.time(),'result':{'status':'cancelled',
                'processes_stopped':True,'effects':'partial_requires_fresh_environment_check',
                'private_logs':row.get('execution',{}).get('logs',[])}}
            records[operation_id]=value;self._save(records);return value
        finally:os.close(fd)
