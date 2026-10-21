"""Fixed remote stdio agent; no command/shell action is exposed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Callable, Any

from .file_transaction import replace_file
from .probes.host import collect_host_facts
from .scripts.authorized_key import _line_key_identity, merge_authorized_keys, validate_public_key
from .permits import ExecutionGrantIssuer, GrantLedger, PermitVerifier
from .journal import OperationJournal


class RemoteAgentError(Exception):
    pass


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RemoteAgentError("duplicate protocol key")
        result[key] = value
    return result


def load_execution_grant_verifier(*, home: str | Path, machine_id_file: str | Path,
                                  key_file: str | Path = "/etc/sandbox-graph/execution-grant.key"):
    path = Path(key_file)
    if not path.exists():
        return None
    if path.is_symlink():
        raise RemoteAgentError("execution grant key must not be a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RemoteAgentError("execution grant key ownership or permissions are invalid")
    key = path.read_bytes()
    if len(key) < 32:
        raise RemoteAgentError("execution grant key is invalid")
    try:
        machine_id = Path(machine_id_file).read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RemoteAgentError("machine identity is unavailable") from exc
    ledger = GrantLedger(Path(home) / ".local/state/sandbox-graph/execution-grants.json")
    return PermitVerifier(ExecutionGrantIssuer(key), ledger, machine_id=machine_id)


class RemoteAgent:
    def __init__(self, *, home: str | Path | None = None, proc_root: str | Path = "/proc",
                 sys_root: str | Path = "/sys", dev_root: str | Path = "/dev",
                 machine_id_file: str | Path = "/etc/machine-id",
                 authorize: Callable[[str, dict, dict], bool] | None = None,
                 journal: OperationJournal | None = None):
        self.home = Path(home or Path.home())
        self.proc_root = Path(proc_root)
        self.sys_root = Path(sys_root)
        self.dev_root = Path(dev_root)
        self.machine_id_file = Path(machine_id_file)
        self.authorize = authorize or (lambda _action, _grant, _input: False)
        self.journal = journal

    def dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) - {"action", "input", "grant"}:
            raise RemoteAgentError("request shape is invalid")
        action, data = request.get("action"), request.get("input")
        if not isinstance(action, str) or not isinstance(data, dict):
            raise RemoteAgentError("request shape is invalid")
        if action == "probe.access.v1":
            return self._probe_access(data)
        if action == "probe.host.v1":
            return self._probe_host(data)
        if action == "ssh.authorized_key.inspect.v1":
            return self._inspect_authorized_key(data)
        if action == "operation.journal.accept.v1":
            self._require_grant(action, request.get("grant"), data)
            if self.journal is None or set(data) != {"operation_id", "action", "input"} or not isinstance(data["input"], dict):
                raise RemoteAgentError("operation journal input is invalid")
            return self.journal.accept(operation_id=data["operation_id"], action=data["action"], input_data=data["input"])
        if action == "operation.journal.transition.v1":
            self._require_grant(action, request.get("grant"), data)
            if self.journal is None or set(data) - {"operation_id", "state", "process_identity", "result"} or not {"operation_id", "state"}.issubset(data):
                raise RemoteAgentError("operation journal transition input is invalid")
            return self.journal.transition(data["operation_id"], state=data["state"],
                process_identity=data.get("process_identity"), result=data.get("result"))
        if action == "ssh.authorized_key.ensure.v1":
            self._require_grant(action, request.get("grant"), data)
            return self._ensure_authorized_key(data)
        raise RemoteAgentError("unregistered action")


    def _require_grant(self, action: str, grant: Any, data: dict) -> None:
        if not isinstance(grant, dict) or not self.authorize(action, grant, data):
            raise RemoteAgentError("valid execution grant is required for mutation")

    def _machine_id(self) -> str:
        try:
            value = self.machine_id_file.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise RemoteAgentError("machine identity is unavailable") from exc
        if not value:
            raise RemoteAgentError("machine identity is unavailable")
        return value

    def _boot_id(self) -> str | None:
        for path in (self.proc_root / "sys/kernel/random/boot_id", self.proc_root / "boot-id"):
            try:
                value = path.read_text(encoding="ascii").strip()
            except OSError:
                continue
            if value:
                return value
        return None

    def _probe_access(self, data: dict) -> dict[str, Any]:
        if data != {"read_only": True}:
            raise RemoteAgentError("access probe only supports read-only input")
        return {
            "machine_id": self._machine_id(), "boot_id": self._boot_id(),
            "uid": os.geteuid(), "groups": sorted(set(os.getgroups())),
            "can_exec": True,
            "tools": [name for name in ("systemctl", "python3", "tar", "sha256sum") if shutil.which(name)],
        }

    def _probe_host(self, data: dict) -> dict[str, Any]:
        role = data.get("role")
        if set(data) != {"role"} or not isinstance(role, str):
            raise RemoteAgentError("host probe role is invalid")
        facts = collect_host_facts(proc_root=self.proc_root, sys_root=self.sys_root, dev_root=self.dev_root)
        from dataclasses import asdict
        from .probes.host import assess_role
        return {"facts": asdict(facts), "role": assess_role(role, facts)}

    def _ensure_authorized_key(self, data: dict) -> dict[str, Any]:
        required = {"node_id", "machine_id", "public_key", "expected_fingerprint",
                    "expected_sha256", "preserve_existing", "compare_and_swap"}
        if set(data) != required:
            raise RemoteAgentError("authorized-key input shape is invalid")
        if not isinstance(data["node_id"], str) or data["machine_id"] != self._machine_id():
            raise RemoteAgentError("authorized-key target identity mismatch")
        if data["preserve_existing"] is not True or data["compare_and_swap"] is not True:
            raise RemoteAgentError("authorized-key action requires preserve and compare-and-swap")
        key, fingerprint = validate_public_key(data["public_key"])
        if fingerprint != data["expected_fingerprint"]:
            raise RemoteAgentError("authorized-key fingerprint mismatch")
        expected = data["expected_sha256"]
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
            raise RemoteAgentError("authorized-key expected digest is invalid")
        ssh_dir = self.home / ".ssh"
        if ssh_dir.is_symlink():
            raise RemoteAgentError(".ssh directory must not be a symlink")
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        info = ssh_dir.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise RemoteAgentError(".ssh directory ownership is invalid")
        os.chmod(ssh_dir, 0o700)
        target = ssh_dir / "authorized_keys"
        if target.is_symlink():
            raise RemoteAgentError("authorized_keys must not be a symlink")
        try:
            current = target.read_bytes()
        except FileNotFoundError:
            current = b""
        actual = hashlib.sha256(current).hexdigest() if target.exists() else None
        if actual != expected:
            raise RemoteAgentError("authorized_keys compare-and-swap digest mismatch")
        merged, changed = merge_authorized_keys(current, key)
        if changed:
            backup_dir = self.home / ".local/state/sandbox-graph/private-backups"
            receipt = replace_file(target, merged, expected_sha256=actual, backup_dir=backup_dir, mode=0o600)
            after = receipt.after_sha256
        else:
            if target.exists():
                os.chmod(target, 0o600)
            after = actual
        return {"fingerprint": fingerprint, "changed": changed, "after_sha256": after}

    def _inspect_authorized_key(self, data: dict) -> dict[str, Any]:
        if set(data) != {"machine_id", "public_key_fingerprint"}:
            raise RemoteAgentError("authorized-key inspection input shape is invalid")
        if data["machine_id"] != self._machine_id():
            raise RemoteAgentError("authorized-key target identity mismatch")
        fingerprint = data["public_key_fingerprint"]
        if not isinstance(fingerprint, str) or not fingerprint.startswith("SHA256:"):
            raise RemoteAgentError("authorized-key fingerprint is invalid")
        ssh_dir = self.home / ".ssh"
        target = ssh_dir / "authorized_keys"
        if ssh_dir.is_symlink() or (ssh_dir.exists() and not ssh_dir.is_dir()) or target.is_symlink():
            raise RemoteAgentError("authorized-key path is unsafe")
        try:
            contents = target.read_bytes()
        except FileNotFoundError:
            contents = b""
        from .scripts.authorized_key import validate_public_key
        present = False
        for line in contents.splitlines():
            identity = _line_key_identity(line)
            if identity is None:
                continue
            try:
                _key, existing_fp = validate_public_key(" ".join(identity))
            except ValueError:
                continue
            if existing_fp == fingerprint:
                present = True
                break
        return {"machine_id": self._machine_id(), "present": present,
                "authorized_keys_sha256": hashlib.sha256(contents).hexdigest() if target.exists() else None}


def main() -> int:
    if sys.argv[1:] != ["--stdio-json"]:
        print(json.dumps({"error": "only --stdio-json is supported"}, separators=(",", ":")))
        return 2
    payload = sys.stdin.buffer.read(1_048_577)
    if len(payload) > 1_048_576:
        print(json.dumps({"error": "request exceeds size limit"}, separators=(",", ":")))
        return 2
    try:
        request = json.loads(payload, object_pairs_hook=_unique_pairs)
        home = Path.home()
        machine_id_file = Path("/etc/machine-id")
        verifier = load_execution_grant_verifier(home=home, machine_id_file=machine_id_file)
        machine_id = machine_id_file.read_text(encoding="ascii").strip()
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            boot_id = None
        journal = OperationJournal(home / ".local/state/sandbox-graph/operations.json",
                                   machine_id=machine_id, boot_id=boot_id)
        result = RemoteAgent(home=home, machine_id_file=machine_id_file,
                             authorize=verifier.authorize if verifier else None,
                             journal=journal).dispatch(request)
    except Exception as exc:
        # Do not write tracebacks or request payloads to stderr: requests may carry keys.
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:256]}, separators=(",", ":")))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
