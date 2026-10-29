import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

try:
    from sandbox_bench import adapters
except ImportError:
    adapters = None


def reply(value, headers=None, status=200):
    return SimpleNamespace(body=json.dumps(value).encode(), headers=headers or {},
                           status=status, headers_s=0.01, complete_s=0.02)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(adapters, "adapter implementation is missing")
        self.config = {"product": "agentenv", "api_url": "https://api.example",
                       "data_url": "https://data.example", "api_key_env": "BENCH_TEST_KEY",
                       "source": {"kind": "template", "id": "template-1"},
                       "create_body": {"timeout": 600, "metadata": {"custom": "keep"}}}
        self.env = patch.dict(os.environ, {"BENCH_TEST_KEY": "CONTROL_SECRET"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_create_preserves_fields_metadata_and_timing(self):
        with patch.object(adapters, "request", return_value=reply(
                {"sandboxID": "s-1", "envdAccessToken": "DATA_SECRET"}, status=201)) as req:
            sb = adapters.Adapter(self.config).create("run-1", 5)
        body = json.loads(req.call_args.kwargs["body"])
        self.assertEqual(body, {"templateID": "template-1", "timeout": 600,
                               "metadata": {"custom": "keep", "run_id": "run-1"}})
        self.assertEqual(req.call_args.kwargs["headers"]["X-API-Key"], "CONTROL_SECRET")
        self.assertEqual(sb["sandbox_id"], "s-1")
        self.assertEqual(sb["api_complete_s"], .02)
        self.assertEqual(sb["private"]["envdAccessToken"], "DATA_SECRET")
        self.assertEqual(self.config["create_body"]["metadata"], {"custom": "keep"})

    def test_no_ttl_is_invented_and_cube_image_fails_explicitly(self):
        self.config["create_body"] = {}
        with patch.object(adapters, "request", return_value=reply({"sandboxID": "s"})) as req:
            adapters.Adapter(self.config).create("r", 1)
        self.assertNotIn("timeout", json.loads(req.call_args.kwargs["body"]))
        self.config.update(product="cubesandbox", source={"kind": "image", "id": "image"})
        with self.assertRaises(NotImplementedError):
            adapters.Adapter(self.config).create("r", 1)

    def test_agent_image_uses_cold_endpoint(self):
        self.config["source"] = {"kind": "image", "id": "registry/image@sha256:abc"}
        with patch.object(adapters, "request", return_value=reply({"sandboxID": "s"})) as req:
            adapters.Adapter(self.config).create("r", 1)
        self.assertEqual(req.call_args.args[1], "https://api.example/sandboxes-cold")
        self.assertEqual(json.loads(req.call_args.kwargs["body"])["image"],
                         "registry/image@sha256:abc")

    def test_kuasar_artifact_mapping_rejects_docker_name(self):
        self.config["product"] = "kuasar"
        self.config["source"] = {"kind": "image", "id": "file://base.image@location:images"}
        encoded = "e2b-img-" + base64.urlsafe_b64encode(
            b"file://base.image@location:images").decode().rstrip("=")
        with patch.object(adapters, "request", return_value=reply({"sandboxID": "s"})) as req:
            adapters.Adapter(self.config).create("r", 1)
        self.assertEqual(json.loads(req.call_args.kwargs["body"])["templateID"], encoded)
        self.config["source"]["id"] = encoded
        with patch.object(adapters, "request", return_value=reply({"sandboxID": "s"})) as req:
            adapters.Adapter(self.config).create("r", 1)
        self.assertEqual(json.loads(req.call_args.kwargs["body"])["templateID"], encoded)
        for invalid in ["ubuntu:22.04", "file:///tmp/a.image",
                        "file://../a.image@location:images"]:
            self.config["source"]["id"] = invalid
            with self.assertRaises(ValueError):
                adapters.Adapter(self.config).create("r", 1)

    def test_exec_routes_and_credentials_are_product_specific(self):
        import struct
        def frame(v, flag=0):
            raw = json.dumps(v).encode()
            return bytes([flag]) + struct.pack(">I", len(raw)) + raw
        raw = frame({"event": {"end": {"exitCode": 0, "exited": True}}}) + frame({}, 2)
        sb = {"sandbox_id": "s-1", "private": {"envdAccessToken": "DATA_SECRET",
                                               "domain": "sandbox.example"}}
        for product, expected in [
            ("agentenv", {"x-agentenv-sandbox-id": "s-1", "x-agentenv-target-port": "49983"}),
            ("kuasar", {"E2b-Sandbox-Id": "s-1", "E2b-Sandbox-Port": "49983"}),
            ("cubesandbox", {"Host": "49983-s-1.sandbox.example"})]:
            self.config["product"] = product
            with patch.object(adapters, "request", return_value=SimpleNamespace(
                    body=raw, headers={"content-type": "application/connect+json"})) as req:
                result = adapters.Adapter(self.config).execute(sb, "printf ok", 2)
            self.assertEqual(result["exit_code"], 0)
            headers = req.call_args.kwargs["headers"]
            for name, value in expected.items():
                self.assertEqual(headers[name], value)
            self.assertNotIn("X-API-Key", headers)
            self.assertEqual(headers["X-Access-Token"], "DATA_SECRET")
            self.assertEqual(headers["Authorization"], "Basic cm9vdDo=")
            self.assertEqual(json.loads(req.call_args.kwargs["body"][5:]),
                             {"process": {"cmd": "/bin/sh", "args": ["-c", "printf ok"]},
                              "stdin": False})

    def test_list_filters_local_ownership_and_pages_with_total_deadline(self):
        self.config["product"] = "kuasar"  # server does not filter metadata
        pages = [reply([{"sandboxID": "a", "metadata": {"run_id": "r"}},
                        {"sandboxID": "other", "metadata": {"run_id": "other"}}],
                       {"x-next-token": "next"}),
                 reply([{"sandboxID": "b", "metadata": {"run_id": "r"}}])]
        with patch.object(adapters, "request", side_effect=pages) as req:
            owned = adapters.Adapter(self.config).list_owned("r", 2)
        self.assertEqual([s["sandbox_id"] for s in owned], ["a", "b"])
        query = parse_qs(urlsplit(req.call_args.args[1]).query)
        self.assertEqual(query["nextToken"], ["next"])
        self.assertLessEqual(req.call_args.kwargs["timeout"], 2)

    def test_cube_list_does_not_follow_ignored_cursor(self):
        self.config["product"] = "cubesandbox"
        with patch.object(adapters, "request", return_value=reply(
                [{"sandboxID": "s", "metadata": {"run_id": "r"}}],
                {"x-next-token": "ignored"})) as req:
            self.assertEqual(len(adapters.Adapter(self.config).list_owned("r", 2)), 1)
        self.assertEqual(req.call_count, 1)

    def test_info_refreshes_private_token_and_effective_spec_is_evidence_based(self):
        sb = {"sandbox_id": "s", "private": {}}
        self.config["product"] = "cubesandbox"
        with patch.object(adapters, "request", return_value=reply(
                {"sandboxID": "s", "state": "running", "cpuMilli": 1500,
                 "cpuCount": 1, "memoryMB": 2048, "clientID": "node",
                 "envdAccessToken": "DATA_SECRET"})):
            spec = adapters.Adapter(self.config).effective_spec(sb, 2)
        self.assertEqual(spec, {"vcpus": 1.5, "memory_mib": 2048})
        self.assertEqual(sb["private"]["envdAccessToken"], "DATA_SECRET")
        self.config["product"] = "kuasar"
        with patch.object(adapters, "request", return_value=reply(
                {"cpuCount": 2, "memoryMB": 2048})):
            self.assertEqual(adapters.Adapter(self.config).effective_spec(sb, 2),
                             {"vcpus": None, "memory_mib": None})

    def test_malformed_create_and_missing_credentials_fail_safely(self):
        with patch.object(adapters, "request", return_value=reply(
                {"envdAccessToken": "DATA_SECRET"})):
            with self.assertRaises(ValueError) as caught:
                adapters.Adapter(self.config).create("r", 1)
        self.assertNotIn("DATA_SECRET", str(caught.exception))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                adapters.Adapter(self.config)

    def test_delete_encodes_id_and_uses_control_credentials(self):
        with patch.object(adapters, "request", return_value=reply(None, status=204)) as req:
            adapters.Adapter(self.config).delete({"sandbox_id": "a/b"}, 2)
        self.assertEqual(req.call_args.args[:2],
                         ("DELETE", "https://api.example/sandboxes/a%2Fb"))

    def test_cube_restricted_traffic_token_reaches_proxy_only(self):
        self.config["product"] = "cubesandbox"
        sandbox = {"sandbox_id": "s", "private": {
            "domain": "example.com", "trafficAccessToken": "TRAFFIC_SECRET"}}
        with patch.object(adapters, "request", return_value=SimpleNamespace(
                body=b"", headers={"content-type": "application/connect+json"})) as req:
            with self.assertRaises(adapters.TransportError):
                adapters.Adapter(self.config).execute(sandbox, "true", 1)
        self.assertEqual(req.call_args.kwargs["headers"].get("e2b-traffic-access-token"),
                         "TRAFFIC_SECRET")

    def test_repeated_pagination_cursor_is_error_not_an_infinite_loop(self):
        with patch.object(adapters, "request", return_value=reply([], {"x-next-token": "same"})):
            with self.assertRaises(adapters.TransportError):
                adapters.Adapter(self.config).list_owned("r", 1)


if __name__ == "__main__":
    unittest.main()


class KuasarClusterTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(adapters, "adapter implementation is missing")
        self.secret_hex = "ab" * 32
        self.env = patch.dict(os.environ, {"BENCH_TEST_KEY": "CONTROL", "KUASAR_SECRET": self.secret_hex})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {"product": "kuasar", "api_url": "https://router.example",
                       "data_url": "https://router.example", "api_key_env": "BENCH_TEST_KEY",
                       "source": {"kind": "template", "id": "e2b-img-abc"}, "create_body": {},
                       "kuasar_cluster": {"api_host": "api.router.example", "group": "grp-1",
                                          "domain": "sbx.example", "api_secret_env": "KUASAR_SECRET"}}

    def test_minted_key_matches_audited_wire_format(self):
        import hashlib, hmac, struct
        key = adapters._mint_cluster_key(bytes.fromhex(self.secret_hex), now=1700000000, nonce=b"\x01\x02\x03\x04")
        self.assertTrue(key.startswith("e2b_") and key[4:] == key[4:].lower())
        raw = bytes.fromhex(key[4:])
        self.assertEqual(len(raw), 12 + 4 + 4 + 16)
        secret = bytes.fromhex(self.secret_hex)
        self.assertEqual(raw[:12], hashlib.sha256(secret).digest()[:12])
        self.assertEqual(raw[12:16], struct.pack(">I", 1700000000))
        self.assertEqual(raw[16:20], b"\x01\x02\x03\x04")
        self.assertEqual(raw[20:], hmac.new(secret, raw[:20], hashlib.sha256).digest()[:16])

    def test_cluster_config_requires_fields_and_mints_key(self):
        config = {**self.config, "kuasar_cluster": {"group": "g"}}
        with self.assertRaises(ValueError):
            adapters.Adapter(config)
        adapter = adapters.Adapter(self.config)
        self.assertEqual(adapter.headers["Host"], "api.router.example")
        self.assertEqual(adapter.headers["X-Kuasar-Sandbox-Group"], "grp-1")
        self.assertTrue(adapter.headers["X-API-Key"].startswith("e2b_"))
        self.assertEqual(adapter.headers["X-API-Key"], adapter.headers["X-API-Key"].lower())
        # cluster 仅限 kuasar
        with self.assertRaises(ValueError):
            adapters.Adapter({**self.config, "product": "cubesandbox"})

    def test_cluster_create_records_route_key_and_detail_injects_it(self):
        adapter = adapters.Adapter(self.config)
        with patch.object(adapters, "request", return_value=reply(
                {"sandboxID": "s-1", "routeKey": "rk-1"}, status=201)) as req:
            sb = adapter.create("run-1", 5)
        self.assertEqual(sb["sandbox_id"], "s-1")
        with patch.object(adapters, "request", return_value=reply({"state": "running"})) as req:
            adapter.info(sb, 5)
        self.assertEqual(req.call_args.kwargs["headers"]["X-Kuasar-Route-Key"], "rk-1")

    def test_cluster_detail_without_route_key_is_rejected(self):
        adapter = adapters.Adapter(self.config)
        with self.assertRaises(ValueError):
            adapter.info({"sandbox_id": "unknown"}, 5)

    def test_cluster_execute_forwards_data_plane_headers(self):
        adapter = adapters.Adapter(self.config)
        with patch.object(adapters, "request", return_value=reply(
                {"sandboxID": "s-1", "routeKey": "rk-1", "envdAccessToken": "tok"}, status=201)):
            sb = adapter.create("run-1", 5)
        adapter.execute = adapter.execute  # unchanged; just ensure headers built
        connect_body = b""
        with patch.object(adapters, "request", return_value=reply({"state": "running"})) as req:
            # execute uses ConnectDecoder; a minimal non-connect body still captures headers before decoding
            try:
                adapter.execute(sb, "echo hi", 5)
            except Exception:
                pass
        headers = req.call_args.kwargs["headers"]
        self.assertEqual(headers["Host"], "49983-s-1.sbx.example")
        self.assertEqual(headers["X-Kuasar-Sandbox-Group"], "grp-1")
        self.assertEqual(headers["X-Kuasar-Route-Key"], "rk-1")
        self.assertEqual(headers["X-Access-Token"], "tok")

    def test_cluster_list_owned_uses_session_tracking(self):
        adapter = adapters.Adapter(self.config)
        with patch.object(adapters, "request", return_value=reply(
                {"sandboxID": "s-1", "routeKey": "rk-1"}, status=201)):
            adapter.create("run-1", 5)
        with patch.object(adapters, "request", side_effect=AssertionError("cluster list must not call the API")):
            owned = adapter.list_owned("run-1", 5)
        self.assertEqual([row["sandbox_id"] for row in owned], ["s-1"])
        self.assertEqual(adapter.list_owned("other-run", 5), [])

    def test_cluster_effective_spec_reads_real_limits(self):
        adapter = adapters.Adapter(self.config)
        with patch.object(adapters, "request", return_value=reply(
                {"sandboxID": "s-1", "routeKey": "rk-1"}, status=201)):
            sb = adapter.create("run-1", 5)
        with patch.object(adapters, "request", return_value=reply({"cpuCount": 4, "memoryMB": 8192})):
            self.assertEqual(adapter.effective_spec(sb, 5), {"vcpus": 4, "memory_mib": 8192})

    def test_noncluster_kuasar_still_downgrades_effective_spec(self):
        config = {**self.config}; config.pop("kuasar_cluster")
        adapter = adapters.Adapter(config)
        with patch.object(adapters, "request", return_value=reply({"cpuCount": 4, "memoryMB": 8192})):
            self.assertEqual(adapter.effective_spec({"sandbox_id": "s-1"}, 5), {"vcpus": None, "memory_mib": None})

    def test_route_key_is_persisted_and_survives_restart(self):
        """issue #12：routeKey 必须落 instance vault；进程重启（内存丢失）后仍可删除，不再孤儿化。"""
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        config = {**self.config, "instance_credentials": {
            "path": str(Path(tmp.name) / "private"), "scope": "run-1", "generation": 1}}
        adapter = adapters.Adapter(config)
        with patch.object(adapters, "request", return_value=reply(
                {"sandboxID": "s-1", "routeKey": "rk-1"}, status=201)):
            sandbox = adapter.create("run-1", 5)
        self.assertEqual(adapter._instance_vault.observe_route("s-1").get("routeKey"), "rk-1")
        restarted = adapters.Adapter(config)          # 模拟进程重启
        self.assertEqual(restarted._route_keys, {})
        with patch.object(adapters, "request", return_value=reply({"ok": True})) as req:
            restarted.delete(sandbox, 5)
        self.assertEqual(req.call_args.kwargs["headers"]["X-Kuasar-Route-Key"], "rk-1")

    def test_cluster_config_boolean_is_rejected_clearly(self):
        """issue #9：kuasar_cluster 必须是 mapping；bool 旧写法要给明确错误（不是 E1 里莫名 blocked）。"""
        with self.assertRaises(ValueError) as ctx:
            adapters.Adapter({**self.config, "kuasar_cluster": True})
        self.assertIn("must be a mapping", str(ctx.exception))

    def test_probe_control_plane_reports_unreachable_with_deployment_hint(self):
        """issue #13：控制面不可达要与其它错误分开，并给出 router/sshd/防火墙 的部署指引。"""
        adapter = adapters.Adapter(self.config)
        with patch.object(adapters, "request", side_effect=OSError("connection refused")):
            result = adapter.probe_control_plane(1)
        self.assertFalse(result["reachable"])
        self.assertEqual(result["reason"], "control_plane_unreachable")
        self.assertIn("router", result["hint"])
        with patch.object(adapters, "request", return_value=reply({"data": []})):
            self.assertTrue(adapter.probe_control_plane(1)["reachable"])
