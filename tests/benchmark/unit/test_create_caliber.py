"""BCT-14：创建时延口径（对齐官方：响应头停表 + 连接复用 + 预热排除）。"""
import http.client
from types import SimpleNamespace

import pytest

from sandbox_bench import adapters
from sandbox_bench.measure import (CREATE_CALIBER_ONE_OFF, CREATE_CALIBER_REUSE, create_sync, warmup)
from sandbox_bench.metrics.latency import summarize_latency
from sandbox_bench.transport import Session, request as http_request


class FakeResponse:
    status = 200
    length = 0
    will_close = False
    def read1(self, n): return b""
    def getheaders(self): return [("Content-Type", "application/json")]


class FakeConnection:
    opened = 0
    def __init__(self, host, port, timeout): FakeConnection.opened += 1; self.sock = None
    def request(self, method, path, body=None, headers=None): pass
    def getresponse(self): return FakeResponse()
    def close(self): pass


class SyncAdapter:
    """同步创建假 adapter：响应头 T1 到达，body 到 T2。"""
    product = "cubesandbox"
    def __init__(self, *, headers_s, complete_s, session=None):
        self.headers_s = headers_s; self.complete_s = complete_s; self.session = session; self.created = 0
    def create(self, run_id, timeout):
        self.created += 1
        return {"sandbox_id": f"s-{self.created}", "api_headers_s": self.headers_s, "api_complete_s": self.complete_s}
    def delete(self, handle, timeout): pass


ENDPOINT = SimpleNamespace(synchronous=True, endpoint_kind="api_complete")


def test_header_stop_records_sync_create_at_response_headers(monkeypatch):
    adapter = SyncAdapter(headers_s=0.010, complete_s=0.055)
    row, _ = create_sync(adapter, "r", 5, 0.1, completion="API_COMPLETE", endpoint=ENDPOINT)
    assert row["status"] == "success"
    assert row["elapsed_s"] == pytest.approx(0.010)          # 官方口径：响应头停表
    assert row["state_ready_s"] == pytest.approx(0.010)
    assert row["body_read_s"] == pytest.approx(0.045)        # 整包读取单列
    assert row["caliber_version"] == CREATE_CALIBER_ONE_OFF  # 无 Session -> one-off 口径


def test_session_reuses_connections_per_host(monkeypatch):
    monkeypatch.setattr(http.client, "HTTPConnection", FakeConnection)
    FakeConnection.opened = 0
    session = Session(max_per_host=2)
    for _ in range(6):
        session.request("GET", "http://host.example/x", timeout=5)
    assert FakeConnection.opened <= 2          # 复用，不每请求新建
    assert session.connections_opened <= 2
    session.close()


def test_one_off_request_opens_a_connection_each_time(monkeypatch):
    monkeypatch.setattr(http.client, "HTTPConnection", FakeConnection)
    FakeConnection.opened = 0
    for _ in range(6):
        http_request("GET", "http://host.example/x", timeout=5)
    assert FakeConnection.opened == 6          # 对照：单次连接，不复用


def test_reuse_caliber_is_used_when_adapter_has_a_session():
    adapter = SyncAdapter(headers_s=0.010, complete_s=0.055, session=Session())
    row, _ = create_sync(adapter, "r", 5, 0.1, completion="API_COMPLETE", endpoint=ENDPOINT)
    assert row["caliber_version"] == CREATE_CALIBER_REUSE


def test_warmup_is_excluded_and_returns_its_count():
    adapter = SyncAdapter(headers_s=0.01, complete_s=0.02, session=Session())
    assert warmup(adapter, 3, "r", 5) == 3
    assert adapter.created == 3
    row, _ = create_sync(adapter, "r", 5, 0.1, completion="API_COMPLETE", endpoint=ENDPOINT)
    assert adapter.created == 4               # 预热 3 次后，正式 1 次；预热不入统计


def test_caliber_version_is_a_comparability_field():
    base = {"status": "success", "elapsed_s": 0.01, "product": "cube", "method": "B01",
            "completion_kind": "API_COMPLETE", "clock_domain": "c", "fixture_id": "f",
            "cache_mode": "cold", "specification": {}, "repeat": 1,
            "start_clock_domain": "c", "end_clock_domain": "c"}
    same = [dict(base, operation_id="1"), dict(base, operation_id="2")]
    summarize_latency(same)   # 同口径（缺省都无版本）可比，不抛
    mixed = [dict(base, operation_id="1", caliber_version=CREATE_CALIBER_ONE_OFF),
             dict(base, operation_id="2", caliber_version=CREATE_CALIBER_REUSE)]
    with pytest.raises(ValueError, match="incomparable caliber_version"):
        summarize_latency(mixed)
