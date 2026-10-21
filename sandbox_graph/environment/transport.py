"""Bounded OpenSSH transport using only a fixed remote JSON-agent command."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REMOTE_AGENT_COMMAND = "sandbox-graph-remote-agent --stdio-json"
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_SAFE_LOCAL_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")


@dataclass(frozen=True)
class SSHRequest:
    address: str
    port: int
    user: str
    known_hosts: Path
    identity_file: Path
    payload: dict[str, Any]
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class SSHResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _validated_host(address: str) -> str:
    try:
        return f"[{ipaddress.IPv6Address(address).compressed}]"
    except ipaddress.AddressValueError:
        if not _DNS_LABEL.fullmatch(address) or address.startswith("-") or ".." in address:
            raise ValueError("invalid host address")
        return address


def _path_argument(path: Path, label: str) -> str:
    resolved = path.expanduser().resolve(strict=True)
    if not _SAFE_LOCAL_PATH.fullmatch(str(resolved)):
        raise ValueError(f"{label} path contains unsupported characters")
    return str(resolved)


def build_ssh_argv(
    *, address: str, port: int, user: str, known_hosts: str | Path,
    identity_file: str | Path, executable: str = "ssh",
) -> list[str]:
    """Construct an argv without a shell, host-key discovery, or user command."""
    host = _validated_host(address)
    if not _USER.fullmatch(user):
        raise ValueError("invalid SSH user")
    if not 1 <= int(port) <= 65535:
        raise ValueError("invalid SSH port")
    known_path = Path(known_hosts)
    key_path = Path(identity_file)
    if known_path.is_symlink() or not known_path.is_file():
        raise ValueError("known_hosts must be a regular file")
    if key_path.is_symlink() or not key_path.is_file():
        raise ValueError("identity file must be a regular file")
    key_mode = stat.S_IMODE(key_path.stat().st_mode)
    if key_mode & 0o077:
        raise PermissionError("identity file permissions must not grant group/other access")
    known = _path_argument(known_path, "known_hosts")
    identity = _path_argument(key_path, "identity")
    return [
        executable,
        "-F", "/dev/null",
        "-p", str(port),
        "-i", identity,
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known}",
        "-o", "ConnectTimeout=10",
        "-o", "ClearAllForwardings=yes",
        "-o", "PermitLocalCommand=no",
        f"{user}@{host}",
        REMOTE_AGENT_COMMAND,
    ]


class SSHTransport:
    def __init__(
        self, *, executable: str = "ssh", max_output_bytes: int = 1_048_576,
        max_input_bytes: int = 1_048_576,
    ):
        if max_output_bytes <= 0 or max_input_bytes <= 0:
            raise ValueError("transport byte budgets must be positive")
        self.executable = executable
        self.max_output_bytes = max_output_bytes
        self.max_input_bytes = max_input_bytes

    async def request(self, request: SSHRequest) -> SSHResult:
        if request.timeout_seconds <= 0:
            raise ValueError("SSH timeout must be positive")
        body = json.dumps(request.payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > self.max_input_bytes:
            raise ValueError("SSH input limit exceeded")
        argv = build_ssh_argv(
            address=request.address, port=request.port, user=request.user,
            known_hosts=request.known_hosts, identity_file=request.identity_file,
            executable=self.executable,
        )
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        )

        async def read_bounded(stream: asyncio.StreamReader) -> bytes:
            data = bytearray()
            while chunk := await stream.read(65536):
                data.extend(chunk)
                if len(data) > self.max_output_bytes:
                    raise ValueError("SSH output limit exceeded")
            return bytes(data)

        async def send_input() -> None:
            assert process.stdin is not None
            process.stdin.write(body)
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()

        assert process.stdout is not None and process.stderr is not None
        tasks = [asyncio.create_task(read_bounded(process.stdout)),
                 asyncio.create_task(read_bounded(process.stderr)),
                 asyncio.create_task(send_input()), asyncio.create_task(process.wait())]
        try:
            stdout, stderr, _, returncode = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=request.timeout_seconds
            )
        except (TimeoutError, ValueError, asyncio.CancelledError):
            for task in tasks:
                task.cancel()
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        return SSHResult(returncode=returncode, stdout=stdout, stderr=stderr)
