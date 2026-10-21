"""Read-only, layered DNS/TCP/SSH access checks."""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Any, Protocol

from ..contracts import NodeDeclaration
from ..transport import SSHRequest, SSHTransport


class ReferenceResolver(Protocol):
    def resolve(self, ref: str) -> str: ...


async def _dns_probe(host: str, _port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    rows = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({row[4][0] for row in rows})


async def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    del reader
    writer.close()
    await writer.wait_closed()
    return True


@dataclass(frozen=True)
class AccessResult:
    node_id: str
    network_status: str
    resolved_addresses: tuple[str, ...] = ()
    authentication_status: str = "not_attempted"
    identity_status: str = "unknown"
    execution_status: str = "not_attempted"
    observation: dict[str, Any] | None = None
    failure_code: str | None = None


class AccessProbe:
    """Probe access using resolved references; never sends a mutation action."""

    def __init__(
        self, *, transport: SSHTransport, resolver: ReferenceResolver,
        dns_probe=_dns_probe, tcp_probe=_tcp_probe, timeout_seconds: float = 5.0,
    ):
        if timeout_seconds <= 0:
            raise ValueError("probe timeout must be positive")
        self.transport = transport
        self.resolver = resolver
        self.dns_probe = dns_probe
        self.tcp_probe = tcp_probe
        self.timeout_seconds = timeout_seconds

    async def check(self, node: NodeDeclaration) -> AccessResult:
        try:
            addresses = tuple(await asyncio.wait_for(
                self.dns_probe(node.ssh_address, node.ssh_port), self.timeout_seconds
            ))
        except (OSError, TimeoutError, socket.gaierror):
            return AccessResult(node.node_id, "dns_failed", failure_code="DNS_UNRESOLVED")
        if not addresses:
            return AccessResult(node.node_id, "dns_failed", failure_code="DNS_EMPTY")
        try:
            reachable = await asyncio.wait_for(
                self.tcp_probe(node.ssh_address, node.ssh_port, self.timeout_seconds),
                self.timeout_seconds + 0.25,
            )
        except (OSError, TimeoutError, socket.gaierror):
            reachable = False
        if not reachable:
            return AccessResult(node.node_id, "unreachable", addresses, failure_code="SSH_PORT_UNREACHABLE")
        try:
            known_hosts = self.resolver.resolve(node.known_hosts_ref)
            identity = self.resolver.resolve(node.automation_credential_ref)
        except Exception:
            return AccessResult(node.node_id, "reachable", addresses, failure_code="ACCESS_REFERENCE_UNRESOLVED")
        request = SSHRequest(
            address=node.ssh_address, port=node.ssh_port, user=node.ssh_user,
            known_hosts=known_hosts, identity_file=identity,
            payload={"action": "probe.access.v1", "input": {"read_only": True}},
            timeout_seconds=self.timeout_seconds,
        )
        try:
            result = await self.transport.request(request)
        except PermissionError:
            return AccessResult(node.node_id, "reachable", addresses,
                authentication_status="failed", failure_code="SSH_IDENTITY_FILE_PERMISSIONS")
        except Exception as exc:
            reason = str(exc).lower()
            code = "SSH_HOST_IDENTITY_UNCONFIRMED" if "host key" in reason or "hostkey" in reason else "SSH_AUTH_OR_EXECUTION_FAILED"
            return AccessResult(node.node_id, "reachable", addresses,
                authentication_status="failed", failure_code=code)
        if result.returncode != 0:
            reason = result.stderr[:2048].decode("utf-8", "replace").lower()
            code = "SSH_HOST_IDENTITY_UNCONFIRMED" if "host key" in reason or "hostkey" in reason else "SSH_AUTH_OR_EXECUTION_FAILED"
            return AccessResult(node.node_id, "reachable", addresses,
                authentication_status="failed", failure_code=code)
        try:
            remote = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return AccessResult(node.node_id, "reachable", addresses,
                authentication_status="authenticated", failure_code="REMOTE_IDENTITY_INVALID")
        if not isinstance(remote, dict) or not isinstance(remote.get("machine_id"), str):
            return AccessResult(node.node_id, "reachable", addresses,
                authentication_status="authenticated", failure_code="REMOTE_IDENTITY_MISSING")
        identity_status = "confirmed" if remote["machine_id"] == node.machine_id else "changed"
        if identity_status != "confirmed":
            return AccessResult(node.node_id, "reachable", addresses,
                authentication_status="authenticated", identity_status="changed",
                execution_status="blocked", failure_code="MACHINE_IDENTITY_MISMATCH")
        can_exec = remote.get("can_exec") is True and isinstance(remote.get("uid"), int)
        safe = {key: remote[key] for key in ("machine_id", "boot_id", "uid", "groups", "tools") if key in remote}
        return AccessResult(node.node_id, "reachable", addresses,
            authentication_status="authenticated", identity_status="confirmed",
            execution_status="allowed" if can_exec else "denied", observation=safe,
            failure_code=None if can_exec else "REMOTE_EXECUTION_PERMISSION_DENIED")
