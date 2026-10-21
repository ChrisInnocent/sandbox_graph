"""同机 CLI 注入就绪信号的白盒读取（BCD-13 §13.4／§13.5；对齐 Kuasar ready.sock）。

协议是封闭的两行：`control_ready` → `ready` → EOF，行上限 64 字节。任何协议错误、
超时或多余数据都标记为 `unverified`，绝不补零或降级成黑盒端点。该模块只读一个已注入
的 fd，不触发任何创建副作用。
"""
from __future__ import annotations

import os
import select
import time
from dataclasses import dataclass

READINESS_LINE_LIMIT = 64
_READINESS_EVENTS = ('control_ready', 'ready')


@dataclass(frozen=True)
class ReadinessObservation:
    events: tuple[str, ...]
    status: str          # 'ready' | 'unverified'
    reason: str | None
    elapsed_s: float | None


def parse_readiness(data: bytes, *, eof: bool) -> tuple[str, ...]:
    """Parse the closed control_ready -> ready -> EOF wire; raise ValueError on violation.

    Kept independent of fd handling so a byte-stream oracle can verify it directly.
    """
    if not eof:
        raise ValueError('readiness stream not closed')
    if not data.endswith(b'\n'):
        raise ValueError('readiness stream missing terminal newline')
    lines = data.split(b'\n')[:-1]  # drop the empty element after the final newline
    if len(lines) != len(_READINESS_EVENTS):
        raise ValueError('readiness stream must be exactly control_ready then ready')
    events = []
    for index, (raw, want) in enumerate(zip(lines, _READINESS_EVENTS)):
        if len(raw) > READINESS_LINE_LIMIT:
            raise ValueError(f'readiness line {index + 1} exceeds {READINESS_LINE_LIMIT} bytes')
        got = raw.decode('utf-8', errors='replace')
        if got != want:
            raise ValueError(f'readiness event {index + 1} = {got!r}, want {want!r}')
        events.append(got)
    return tuple(events)


def read_ready_fd(fd: int, timeout: float) -> ReadinessObservation:
    """Read one injected ready descriptor to EOF within `timeout` seconds.

    The descriptor is owned by the caller; this function does not close it.
    """
    if fd < 3:
        raise ValueError('ready fd must be at least 3')
    if timeout <= 0:
        raise ValueError('readiness timeout must be positive')
    started = time.monotonic()
    deadline = started + timeout
    buffer = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ReadinessObservation((), 'unverified', 'readiness_timeout',
                                        time.monotonic() - started)
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if chunk == b'':
            break
        buffer += chunk
    elapsed = time.monotonic() - started
    try:
        events = parse_readiness(bytes(buffer), eof=True)
    except ValueError as exc:
        return ReadinessObservation((), 'unverified', str(exc), elapsed)
    return ReadinessObservation(events, 'ready', None, elapsed)
