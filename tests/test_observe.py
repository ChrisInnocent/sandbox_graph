import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ObserveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("sandbox_bench.observe")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "meminfo").write_text(
            "MemTotal: 2048 kB\nMemFree: 256 kB\nMemAvailable: 1024 kB\n")
        (self.root / "stat").write_text(
            "cpu  100 2 30 400 5 6 7 8 9 10\ncpu0 1 2 3 4\n")
        (self.root / "loadavg").write_text("0.10 0.20 0.30 2/100 123\n")

    def test_sample_uses_available_memory_and_raw_cpu_counters(self):
        result = self.module.sample_proc(str(self.root))
        self.assertEqual(result["mem_available_bytes"], 1048576)
        self.assertEqual(result["mem_total_bytes"], 2097152)
        self.assertEqual(result["cpu_ticks"]["user"], 100)
        self.assertEqual(result["cpu_ticks"]["idle"], 400)
        self.assertEqual(result["loadavg"]["one"], 0.1)
        self.assertGreater(result["time_ns"], 0)
        self.assertTrue(result["host_id"])
        self.assertIn("schema_version", result)

    def test_optional_io_counters_are_preserved(self):
        (self.root / "diskstats").write_text("8 0 sda 1 2 3 4 5 6 7 8 9 10 11\n")
        (self.root / "net").mkdir()
        (self.root / "net/dev").write_text(
            "Inter-| Receive | Transmit\n face |bytes packets\n"
            " eth0: 100 1 0 0 0 0 0 0 200 2 0 0 0 0 0 0\n")
        result = self.module.sample_proc(str(self.root))
        self.assertEqual(result["diskstats"]["sda"]["counters"][0], 1)
        self.assertEqual(result["net_dev"]["eth0"]["rx_bytes"], 100)
        self.assertEqual(result["net_dev"]["eth0"]["tx_bytes"], 200)

    def test_missing_available_never_falls_back_to_free_or_zero(self):
        (self.root / "meminfo").write_text("MemTotal: 2048 kB\nMemFree: 100 kB\n")
        with self.assertRaises(ValueError):
            self.module.sample_proc(str(self.root))

    def test_malformed_required_values_raise(self):
        for filename, content in [
            ("meminfo", "MemTotal: 2 MB\nMemAvailable: 1 MB\n"),
            ("meminfo", "MemTotal: 2 kB\nMemAvailable: 3 kB\n"),
            ("stat", "cpu 1 2 broken 4\n"),
            ("loadavg", "nan 0.2 0.3 1/3 4\n"),
        ]:
            with self.subTest(filename=filename, content=content):
                path = self.root / filename
                old = path.read_text()
                path.write_text(content)
                with self.assertRaises(ValueError):
                    self.module.sample_proc(str(self.root))
                path.write_text(old)

    def test_missing_required_file_raises(self):
        (self.root / "stat").unlink()
        with self.assertRaises((OSError, ValueError)):
            self.module.sample_proc(str(self.root))

    def test_malformed_present_optional_file_is_not_silently_ignored(self):
        (self.root / "diskstats").write_text("8 0 sda malformed\n")
        with self.assertRaises(ValueError):
            self.module.sample_proc(str(self.root))

    def test_optional_io_read_error_keeps_required_sample_and_reports_gap(self):
        (self.root / "diskstats").mkdir()
        result = self.module.sample_proc(str(self.root))
        self.assertEqual(result["mem_available_bytes"], 1048576)
        self.assertNotIn("diskstats", result)
        self.assertEqual(result["optional_errors"]["diskstats"], "IsADirectoryError")

    def test_cli_emits_one_json_and_redacts_error_paths(self):
        command = [sys.executable, "-m", "sandbox_bench.observe",
                   "--proc-root", str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(json.loads(result.stdout)["mem_available_bytes"], 1048576)
        (self.root / "stat").unlink()
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(str(self.root), result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
