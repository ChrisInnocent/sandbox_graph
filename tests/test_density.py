import importlib
import json
import unittest


MIB = 1024 * 1024


class DensityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("sandbox_bench.density")

    def project(self, baseline=1000 * MIB, samples=None, requested=10,
                ready=10, reserve=100 * MIB):
        if samples is None:
            samples = [{"mem_available_bytes": 910 * MIB},
                       {"mem_available_bytes": 900 * MIB}]
        return self.module.projection(baseline, samples, requested, ready, reserve)

    def test_legacy_remaining_and_unified_total_are_distinct(self):
        result = self.project()
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["legacy"]["legacy_estimate_remaining"], 90)
        self.assertNotIn("reason", result["legacy"])
        self.assertEqual(result["unified"]["estimated_total"], 90)
        self.assertEqual(result["unified"]["estimated_additional"], 80)
        self.assertEqual(result["unified"]["per_ready_bytes"], 10 * MIB)

    def test_legacy_rounding_does_not_change_unified_cost(self):
        result = self.project(baseline=100 * MIB,
                              samples=[{"mem_available_bytes": 94 * MIB},
                                       {"mem_available_bytes": 94 * MIB}],
                              requested=7, ready=7, reserve=0)
        self.assertEqual(result["legacy"]["per_requested_mib"], 0.9)
        self.assertEqual(result["legacy"]["legacy_estimate_remaining"], 104)
        self.assertEqual(result["unified"]["estimated_total"], 116)

    def test_partial_readiness_retains_only_legacy_estimate(self):
        result = self.project(ready=8)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(result["reason"])
        self.assertNotIn("estimated_total", result["unified"])
        self.assertEqual(result["legacy"]["legacy_estimate_remaining"], 90)
        self.assertNotIn("reason", result["legacy"])

    def test_invalid_inputs_never_emit_unified_capacity(self):
        cases = [
            {"ready": 0}, {"requested": 0, "ready": 0},
            {"requested": True}, {"ready": True}, {"ready": 11},
            {"reserve": -1}, {"reserve": 1000 * MIB},
            {"reserve": float("nan")}, {"baseline": float("nan")},
            {"baseline": 0}, {"samples": []},
            {"samples": [{"mem_available_bytes": 900 * MIB}]},
            {"samples": [{}, {}]},
            {"samples": [{"mem_available_bytes": -1}] * 2},
            {"samples": [{"mem_available_bytes": float("nan")}] * 2},
            {"samples": [{"mem_available_bytes": 1000 * MIB}] * 2},
            {"samples": [{"mem_available_bytes": 1100 * MIB}] * 2},
        ]
        for args in cases:
            with self.subTest(args=args):
                result = self.project(**args)
                self.assertEqual(result["status"], "invalid")
                self.assertTrue(result["reason"])
                self.assertNotIn("estimated_total", result["unified"])
                json.dumps(result, allow_nan=False)

    def test_legacy_threshold_does_not_invalidate_unified(self):
        result = self.project(baseline=100 * MIB,
                              samples=[{"mem_available_bytes": 95 * MIB}] * 2,
                              reserve=0)
        self.assertEqual(result["legacy"]["status"], "invalid")
        self.assertNotIn("legacy_estimate_remaining", result["legacy"])
        self.assertEqual(result["unified"]["estimated_total"], 200)

    def test_legacy_truncates_samples_to_mib_before_computing_cost(self):
        result = self.project(
            baseline=100 * MIB + 943718,
            samples=[{"mem_available_bytes": 94 * MIB + 104858}] * 2,
            reserve=0)
        self.assertEqual(result["legacy"]["per_requested_mib"], 0.6)
        self.assertEqual(result["legacy"]["legacy_estimate_remaining"], 156)
        self.assertEqual(result["unified"]["estimated_total"], 148)

    def test_plateau_uses_listed_growth_and_tracks_ready_losses(self):
        tracker = self.module.GrowthTracker()
        self.assertIsNone(tracker.update(100, 100)["stop_reason"])
        first = tracker.update(109, 95)
        self.assertEqual(first["net_growth"], 9)
        self.assertEqual(first["ready_growth"], -5)
        self.assertEqual(first["lost_ready"], 5)
        self.assertIsNone(first["stop_reason"])
        second = tracker.update(108, 94)
        self.assertEqual(second["net_growth"], -1)
        self.assertEqual(second["stop_reason"], "plateau")

    def test_growth_resets_consecutive_stalls(self):
        tracker = self.module.GrowthTracker()
        for listed in [100, 109, 120, 129]:
            self.assertIsNone(tracker.update(listed, listed)["stop_reason"])
        self.assertEqual(tracker.update(138, 138)["stop_reason"], "plateau")

    def test_no_ready_instance_is_setup_failure(self):
        tracker = self.module.GrowthTracker()
        self.assertIsNone(tracker.update(100, 0)["stop_reason"])
        self.assertEqual(tracker.update(200, 0)["stop_reason"], "setup_failure")
        tracker = self.module.GrowthTracker()
        self.assertIsNone(tracker.update(0, 0)["stop_reason"])
        self.assertEqual(tracker.update(0, 0)["stop_reason"], "setup_failure")

    def test_caps_are_distinct_from_plateau(self):
        tracker = self.module.GrowthTracker(max_instances=100)
        self.assertEqual(tracker.update(100, 100)["stop_reason"], "instance_cap")
        tracker = self.module.GrowthTracker(max_batches=1)
        self.assertEqual(tracker.update(20, 20)["stop_reason"], "batch_cap")

    def test_tracker_rejects_invalid_contract_and_counts(self):
        for args in [{"batch_size": 9}, {"min_growth": 0}, {"stall_limit": 0},
                     {"max_instances": 0}, {"max_batches": -1}, {"batch_size": True}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.module.GrowthTracker(**args)
        tracker = self.module.GrowthTracker()
        for counts in [(-1, 0), (1, -1), (1, 2), (1.5, 1), (True, 1)]:
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                tracker.update(*counts)


if __name__ == "__main__":
    unittest.main()
