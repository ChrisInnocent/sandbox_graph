import unittest

from sandbox_bench.measure import create_sync, cleanup_owned, summarize


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class AdapterFixture:
    product = "kuasar"

    def __init__(self, clock):
        self.clock = clock
        self.created = 0
        self.lookups = 0
        self.deleted = []
        self.bad_output = False
        self.never_ready = False
        self.fail_delete = set()

    def create(self, run_id, timeout):
        self.created += 1
        self.clock.sleep(0.1)
        return {"sandbox_id": "s1", "private": {"envdAccessToken": "SECRET"},
                "api_headers_s": 0.05, "api_complete_s": 0.1}

    def info(self, handle, timeout):
        self.lookups += 1
        return {"state": "starting" if self.never_ready or self.lookups < 3 else "running"}

    def execute(self, handle, command, timeout):
        self.clock.sleep(0.2)
        marker = command.split("'")[-2]
        return {"exit_code": 0, "stdout": "wrong" if self.bad_output else marker, "stderr": ""}

    def delete(self, handle, timeout):
        sid = handle["sandbox_id"]
        self.deleted.append(sid)
        if sid in self.fail_delete:
            raise RuntimeError("sensitive response SECRET")


class MeasureTests(unittest.TestCase):
    def test_async_201_waits_without_recreating_and_excludes_secrets(self):
        clock = Clock()
        adapter = AdapterFixture(clock)
        owned = []
        row, handle = create_sync(adapter, "r", 5, .1, clock=clock,
                                  sleep=clock.sleep, on_created=owned.append, completion="EXEC_COMPLETE")
        self.assertEqual(adapter.created, 1)
        self.assertEqual(len(owned), 1)
        self.assertEqual(row["status"], "success")
        self.assertAlmostEqual(row["elapsed_s"], .5)
        self.assertAlmostEqual(row["api_complete_s"], .1)
        self.assertGreaterEqual(adapter.lookups, 3)
        self.assertNotIn("SECRET", str(row))
        self.assertIn("SECRET", str(handle))

    def test_wrong_output_is_failure_not_successful_latency(self):
        clock = Clock()
        adapter = AdapterFixture(clock)
        adapter.bad_output = True
        row, _ = create_sync(adapter, "r", 5, .1, clock=clock, sleep=clock.sleep,
                             completion="EXEC_COMPLETE")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["reason"], "probe_output_mismatch")
        self.assertEqual(summarize([row], 1)["success_count"], 0)

    def test_state_completion_does_not_execute_on_any_product(self):
        for product in ("kuasar", "cubesandbox", "agentenv"):
            clock = Clock()
            adapter = AdapterFixture(clock)
            adapter.product = product
            adapter.execute = lambda *args: self.fail("state completion must not execute")
            row, _ = create_sync(adapter, "r", 5, .1, clock=clock, sleep=clock.sleep)
            self.assertEqual(row["status"], "success")
            self.assertAlmostEqual(row["elapsed_s"], .3)

    def test_total_deadline_includes_async_wait(self):
        clock = Clock()
        adapter = AdapterFixture(clock)
        adapter.never_ready = True
        row, handle = create_sync(adapter, "r", .35, .1, clock=clock, sleep=clock.sleep)
        self.assertEqual(row["status"], "timeout")
        self.assertLessEqual(clock(), .350001)
        self.assertEqual(handle["sandbox_id"], "s1")
        self.assertEqual(adapter.created, 1)

    def test_unknown_create_outcome_is_retained(self):
        clock = Clock()
        adapter = AdapterFixture(clock)
        def uncertain(*args):
            raise TimeoutError("SECRET")
        adapter.create = uncertain
        row, handle = create_sync(adapter, "r", 1, .1, clock=clock, sleep=clock.sleep)
        self.assertIsNone(handle)
        self.assertTrue(row["unknown_create_outcome"])
        self.assertNotIn("SECRET", str(row))

    def test_cleanup_attempts_every_owned_id_and_retains_errors(self):
        adapter = AdapterFixture(Clock())
        adapter.fail_delete.add("a")
        result = cleanup_owned(adapter, [{"sandbox_id": "a"}, {"sandbox_id": "b"},
                                        {"sandbox_id": "b"}], 2)
        self.assertEqual(adapter.deleted, ["a", "b"])
        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(result[1]["status"], "api_deleted")
        self.assertNotIn("SECRET", str(result))

    def test_summary_keeps_failures_and_uses_individual_samples(self):
        rows = [{"status": "success", "elapsed_s": 1},
                {"status": "success", "elapsed_s": 3},
                {"status": "failed", "elapsed_s": 50}]
        got = summarize(rows, 10)
        self.assertEqual(got["success_count"], 2)
        self.assertEqual(got["failed_count"], 1)
        self.assertEqual(got["successful_throughput_per_s"], .2)
        self.assertEqual(got["latency_s"]["p50"], 2)
        self.assertEqual(got["latency_s"]["max"], 3)
        self.assertEqual(summarize([], 1)["latency_s"], None)


if __name__ == "__main__":
    unittest.main()
