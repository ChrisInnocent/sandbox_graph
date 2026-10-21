"""Idempotent SSH public-key provisioning through the fixed remote helper."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from ..contracts import NodeDeclaration
from ..transport import SSHRequest
from ..probes.access import AccessResult


_ALGORITHMS = {
    "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


class Resolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class Transport(Protocol):
    async def request(self, request: SSHRequest): ...


@dataclass(frozen=True)
class KeyProvisionResult:
    changed: bool
    verified: bool
    fingerprint: str


def _ssh_fields(blob: bytes) -> tuple[str, tuple[bytes, ...]]:
    offset = 0
    fields = []
    while offset + 4 <= len(blob):
        size = int.from_bytes(blob[offset:offset + 4], "big")
        offset += 4
        if size == 0 or offset + size > len(blob):
            raise ValueError("invalid SSH public key blob")
        fields.append(blob[offset:offset + size])
        offset += size
    if offset != len(blob) or not fields:
        raise ValueError("invalid SSH public key blob")
    try:
        algorithm = fields[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid SSH public key algorithm") from exc
    if algorithm not in _ALGORITHMS:
        raise ValueError("unsupported SSH public key algorithm")
    expected_fields = {"ssh-ed25519": 2, "ssh-rsa": 3,
        "ecdsa-sha2-nistp256": 3, "ecdsa-sha2-nistp384": 3,
        "ecdsa-sha2-nistp521": 3, "sk-ssh-ed25519@openssh.com": 3,
        "sk-ecdsa-sha2-nistp256@openssh.com": 4}[algorithm]
    if len(fields) != expected_fields:
        raise ValueError("invalid SSH public key fields")
    if algorithm == "ssh-ed25519" and len(fields[1]) != 32:
        raise ValueError("invalid ed25519 public key length")
    return algorithm, tuple(fields)


def validate_public_key(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or "\n" in value or "\r" in value or len(value) > 16384:
        raise ValueError("invalid public key line")
    parts = value.strip().split()
    if len(parts) < 2 or parts[0] not in _ALGORITHMS:
        raise ValueError("invalid public key algorithm or format")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid public key encoding") from exc
    algorithm, _fields = _ssh_fields(blob)
    if algorithm != parts[0]:
        raise ValueError("public key algorithm does not match encoded key")
    canonical = f"{algorithm} {parts[1]}" + (f" {parts[2]}" if len(parts) > 2 else "")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return canonical, fingerprint


def _line_key_identity(line: bytes) -> tuple[str, str] | None:
    try:
        parts = line.decode("utf-8").strip().split()
    except UnicodeDecodeError:
        return None
    for index, algorithm in enumerate(parts[:-1]):
        if algorithm in _ALGORITHMS:
            try:
                key, _fingerprint = validate_public_key(" ".join(parts[index:index + 2]))
            except ValueError:
                return None
            return tuple(key.split()[:2])
    return None


def merge_authorized_keys(current: bytes, public_key: str) -> tuple[bytes, bool]:
    key, _fingerprint = validate_public_key(public_key)
    identity = tuple(key.split()[:2])
    for line in current.splitlines():
        if _line_key_identity(line) == identity:
            return current, False
    separator = b"" if not current or current.endswith(b"\n") else b"\n"
    return current + separator + key.encode("ascii") + b"\n", True


async def ensure_authorized_key(
    node: NodeDeclaration, *, public_key: str, transport: Transport,
    resolver: Resolver, verify_new_connection: Callable[[NodeDeclaration], Awaitable[AccessResult]],
    grant_provider: Callable[..., Awaitable[dict]] | None = None,
) -> KeyProvisionResult:
    if node.initial_credential_ref is None:
        raise ValueError("initial credential is required for authorized-key provisioning")
    key, fingerprint = validate_public_key(public_key)
    try:
        known_hosts = resolver.resolve(node.known_hosts_ref)
        initial_identity = resolver.resolve(node.initial_credential_ref)
    except Exception as exc:
        raise ValueError("initial access reference could not be resolved") from exc
    inspect = await transport.request(SSHRequest(
        address=node.ssh_address, port=node.ssh_port, user=node.ssh_user,
        known_hosts=known_hosts, identity_file=initial_identity,
        payload={"action": "ssh.authorized_key.inspect.v1", "input": {
            "machine_id": node.machine_id, "public_key_fingerprint": fingerprint}},
    ))
    if inspect.returncode != 0:
        raise RuntimeError("remote authorized-key inspection failed")
    try:
        current = json.loads(inspect.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("remote authorized-key inspection receipt is invalid") from exc
    if current.get("machine_id") != node.machine_id or not isinstance(current.get("present"), bool):
        raise RuntimeError("remote authorized-key inspection does not match target identity")
    changed = False
    if not current["present"]:
        if grant_provider is None:
            raise PermissionError("an execution grant is required to add an SSH key")
        expected_hash = current.get("authorized_keys_sha256")
        if expected_hash is not None and (not isinstance(expected_hash, str) or len(expected_hash) != 64):
            raise RuntimeError("remote authorized-key digest is invalid")
        action_input = {"node_id": node.node_id, "machine_id": node.machine_id,
            "public_key": key, "expected_fingerprint": fingerprint,
            "expected_sha256": expected_hash, "preserve_existing": True,
            "compare_and_swap": True}
        grant = await grant_provider(action="ssh.authorized_key.ensure.v1", node=node, input=action_input)
        result = await transport.request(SSHRequest(
            address=node.ssh_address, port=node.ssh_port, user=node.ssh_user,
            known_hosts=known_hosts, identity_file=initial_identity,
            payload={"action": "ssh.authorized_key.ensure.v1", "grant": grant,
                     "input": action_input},
        ))
        if result.returncode != 0:
            raise RuntimeError("remote authorized-key action failed")
        try:
            receipt = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("remote authorized-key receipt is invalid") from exc
        if receipt.get("fingerprint") != fingerprint or not isinstance(receipt.get("changed"), bool):
            raise RuntimeError("remote authorized-key receipt does not match request")
        changed = receipt["changed"]
    access = await verify_new_connection(node)
    if (access.authentication_status != "authenticated" or
            access.identity_status != "confirmed" or access.execution_status != "allowed"):
        raise RuntimeError("new automation connection did not independently verify")
    return KeyProvisionResult(changed=changed, verified=True, fingerprint=fingerprint)
