"""Single-use HMAC execution grants and durable remote replay/generation ledger."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping

from ..domain.models import canonical_hash
from .contracts import ExecutionGrant


class ExecutionGrantIssuer:
    def __init__(self, signing_key: bytes, *, clock: Callable[[], float] = time.time,
                 max_ttl_seconds: float = 60, max_clock_skew_seconds: float = 1.0):
        if len(signing_key) < 32:
            raise ValueError("execution grant signing key must be at least 32 bytes")
        if not 0 < max_ttl_seconds <= 60 or not 0 <= max_clock_skew_seconds <= 2:
            raise ValueError("execution grant time bounds are invalid")
        self._key = bytes(signing_key)
        self.clock = clock
        self.max_ttl_seconds = max_ttl_seconds
        self.max_clock_skew_seconds = max_clock_skew_seconds

    @staticmethod
    def _signed_payload(grant: ExecutionGrant) -> bytes:
        unsigned = grant.model_dump(mode="json", exclude={"signature"})
        return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    def issue(self, *, operation_id: str, step_id: str, run_id: str, thread_id: str,
              owner_epoch: int, machine_id: str, resource_generations: Mapping[str, int],
              action: str, input_data: dict, ttl_seconds: float = 30) -> ExecutionGrant:
        if ttl_seconds <= 0 or ttl_seconds > self.max_ttl_seconds:
            raise ValueError("execution grant TTL exceeds issuer limit")
        now = self.clock()
        data = {
            "grant_id": secrets.token_urlsafe(18), "operation_id": operation_id,
            "step_id": step_id, "input_hash": canonical_hash({"action": action, "input": input_data}),
            "machine_id": machine_id, "resource_generations": dict(resource_generations),
            "run_id": run_id, "thread_id": thread_id, "owner_epoch": owner_epoch,
            "nonce": secrets.token_urlsafe(32), "not_before": now,
            "not_after": now + ttl_seconds,
            "max_clock_skew_seconds": self.max_clock_skew_seconds, "signature": "0" * 64,
        }
        grant = ExecutionGrant.model_validate(data)
        signature = hmac.new(self._key, self._signed_payload(grant), hashlib.sha256).hexdigest()
        return grant.model_copy(update={"signature": signature})

    def verify(self, grant: ExecutionGrant, *, action: str, input_data: dict,
               machine_id: str) -> bool:
        now = self.clock()
        if grant.machine_id != machine_id or grant.input_hash != canonical_hash({"action": action, "input": input_data}):
            return False
        if now + grant.max_clock_skew_seconds < grant.not_before:
            return False
        if now + grant.max_clock_skew_seconds >= grant.not_after:
            return False
        if grant.not_after - grant.not_before > self.max_ttl_seconds:
            return False
        expected = hmac.new(self._key, self._signed_payload(grant), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, grant.signature)


class GrantLedger:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path).absolute()
        if self.path.parent.is_symlink() or self.path.parent.resolve() != self.path.parent:
            raise ValueError("grant ledger parent contains symlink")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.clock = clock

    def _load(self) -> dict:
        if self.path.is_symlink():
            raise ValueError("grant ledger must not be a symlink")
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return {"schema_version": 1, "nonces": {}, "resource_generations": {}}
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("grant ledger ownership or permissions are invalid")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if set(data) != {"schema_version", "nonces", "resource_generations"} or data["schema_version"] != 1:
            raise ValueError("grant ledger schema is invalid")
        return data

    def _save(self, data: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=".grant-ledger-", dir=self.path.parent)
        tmp = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def consume(self, grant: ExecutionGrant) -> bool:
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = self._load()
            if grant.nonce in state["nonces"]:
                return False
            generations = state["resource_generations"]
            for domain, value in grant.resource_generations.items():
                if value <= generations.get(domain, 0):
                    return False
            now = self.clock()
            nonces = state["nonces"]
            nonces[grant.nonce] = {"grant_id": grant.grant_id, "operation_id": grant.operation_id,
                                   "accepted_at": now, "not_after": grant.not_after}
            state["nonces"] = {nonce: item for nonce, item in nonces.items()
                               if item["not_after"] >= now - 120}
            for domain, value in grant.resource_generations.items():
                generations[domain] = value
            self._save(state)
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class PermitVerifier:
    def __init__(self, issuer: ExecutionGrantIssuer, ledger: GrantLedger, *, machine_id: str):
        self.issuer, self.ledger, self.machine_id = issuer, ledger, machine_id

    def authorize(self, action: str, grant_data: dict, input_data: dict) -> bool:
        try:
            grant = ExecutionGrant.model_validate(grant_data)
            if not self.issuer.verify(grant, action=action, input_data=input_data, machine_id=self.machine_id):
                return False
            return self.ledger.consume(grant)
        except Exception:
            return False


__all__ = ["ExecutionGrantIssuer", "GrantLedger", "PermitVerifier"]
