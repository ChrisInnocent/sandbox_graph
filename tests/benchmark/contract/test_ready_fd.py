"""BCT-10-READY-FD：同机 ready-fd 就绪信号的独立字节流 oracle。

oracle 是手写字面量字节序列，不调用被测函数生成期望值。独立 marker：endpoint。
"""
import os

import pytest

from sandbox_bench.readiness import parse_readiness, read_ready_fd

pytestmark = pytest.mark.endpoint


def observe(payload, timeout=1.0):
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)
        os.close(write_fd)
        return read_ready_fd(read_fd, timeout)
    finally:
        os.close(read_fd)


def test_valid_stream_is_ready():
    obs = observe(b'control_ready\nready\n')
    assert obs.status == 'ready' and obs.events == ('control_ready', 'ready')


def test_missing_control_ready_is_unverified():
    obs = observe(b'ready\n')
    assert obs.status == 'unverified' and obs.reason


def test_overlong_line_is_unverified():
    obs = observe(b'x' * 65 + b'\nready\n')
    assert obs.status == 'unverified'


def test_extra_bytes_after_ready_is_unverified():
    obs = observe(b'control_ready\nready\nextra\n')
    assert obs.status == 'unverified'


def test_missing_terminal_newline_is_unverified():
    obs = observe(b'control_ready\nready')
    assert obs.status == 'unverified'


def test_timeout_is_unverified_and_fd_stays_open():
    read_fd, write_fd = os.pipe()
    try:
        obs = read_ready_fd(read_fd, 0.05)
        assert obs.status == 'unverified' and obs.reason == 'readiness_timeout'
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_parse_readiness_is_independent_of_fd_handling():
    assert parse_readiness(b'control_ready\nready\n', eof=True) == ('control_ready', 'ready')
    with pytest.raises(ValueError):
        parse_readiness(b'control_ready\nready\n', eof=False)
