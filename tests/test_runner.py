import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandbox_bench.runner import validate_config, run, cleanup_run


def config():
    return {"group_id": "test-only", "product": "agentenv",
            "api_url": "http://localhost:1234", "data_url": "http://localhost:1234",
            "source": {"kind": "template", "id": "fixture"},
            "create_body": {"timeout": 100, "autoPause": False},
            "expected_spec": {"vcpus": 2, "memory_mib": 2048},
            "operation_timeout_s": 2, "run_timeout_s": 10,
            "cleanup_timeout_s": 1, "cleanup_budget_s": 10, "poll_interval_s": .01,
            "requests": 2, "concurrency": 1}


class FakeAdapter:
    product = "agentenv"

    def __init__(self, cfg):
        self.cfg = cfg
        self.count = 0
        self.live = {}

    def create(self, run_id, timeout):
        self.count += 1
        handle = {"sandbox_id": str(self.count), "private": {"envdAccessToken": "SECRET"},
                  "api_headers_s": .001, "api_complete_s": .002}
        self.live[str(self.count)] = handle
        return handle

    def execute(self, handle, command, timeout):
        return {"exit_code": 0, "stdout": command.split("'")[-2], "stderr": ""}

    def effective_spec(self, handle, timeout):
        return {"vcpus": 2, "memory_mib": 2048}

    def delete(self, handle, timeout):
        self.live.pop(handle["sandbox_id"], None)

    def info(self, handle, timeout):
        if handle["sandbox_id"] not in self.live:
            from sandbox_bench.transport import TransportError
            raise TransportError("not found", status=404)
        return {"state": "running"}

    def list_owned(self, run_id, timeout):
        return list(self.live.values())


class RunnerTests(unittest.TestCase):
    def test_ttl_zero_and_short_ttl_are_rejected(self):
        for ttl in (0, 15):
            cfg = config()
            cfg["create_body"]["timeout"] = ttl
            with self.assertRaises(ValueError):
                validate_config(cfg, "create")

    def test_density_needs_placement_and_sampler(self):
        with self.assertRaises(ValueError):
            validate_config(config(), "density-growth")

    def test_nan_or_bool_does_not_pass_numeric_contract(self):
        for value in (float("nan"), True, -1, 0):
            cfg = config()
            cfg["operation_timeout_s"] = value
            with self.assertRaises(ValueError):
                validate_config(cfg, "create")

    def test_run_preserves_evidence_and_cleans_only_owned(self):
        cfg = config()
        adapter = FakeAdapter(cfg)
        with tempfile.TemporaryDirectory() as tmp, patch("sandbox_bench.runner.Adapter", return_value=adapter):
            folder = Path(tmp) / "run"
            result = run(cfg, "create", folder)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["summary"]["success_count"], 2)
            self.assertEqual(adapter.live, {})
            all_text = "".join(p.read_text() for p in folder.iterdir() if p.is_file())
            self.assertNotIn("SECRET", all_text)
            self.assertEqual(len((folder / "operations.jsonl").read_text().splitlines()), 2)
            self.assertTrue((folder / "cleanup.jsonl").exists())
            with self.assertRaises(FileExistsError):
                run(cfg, "create", folder)

    def test_spec_mismatch_is_not_comparable(self):
        cfg = config()
        cfg["expected_spec"]["memory_mib"] = 4096
        with tempfile.TemporaryDirectory() as tmp, patch("sandbox_bench.runner.Adapter", return_value=FakeAdapter(cfg)):
            result = run(cfg, "create", Path(tmp) / "run")
            self.assertEqual(result["data_validity"], "invalid")
            self.assertEqual(result["spec_mismatches"], 2)

    def test_cleanup_rejects_wrong_environment(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp, patch("sandbox_bench.runner.Adapter", return_value=FakeAdapter(cfg)):
            folder = Path(tmp) / "run"
            run(cfg, "create", folder)
            wrong = {**cfg, "api_url": "http://other-host"}
            with self.assertRaises(ValueError):
                cleanup_run(wrong, folder)

    def test_no_successful_spec_evidence_is_unverified(self):
        cfg = config()
        adapter = FakeAdapter(cfg)
        adapter.create = lambda *_: (_ for _ in ()).throw(ConnectionError('response lost'))
        with tempfile.TemporaryDirectory() as tmp, patch("sandbox_bench.runner.Adapter", return_value=adapter):
            result = run(cfg, "create", Path(tmp) / "run")
            self.assertEqual(result["data_validity"], "unverified")


if __name__ == "__main__":
    unittest.main()
