"""Independent BCT-02 numeric expectations; no production-generated oracle."""
import unittest
from sandbox_bench.metrics import summarize_latency, throughput, projection, capture_awk_rounding


class StatisticsTests(unittest.TestCase):
    def test_btc047_quantiles_failures_repeat_and_comparability(self):
        rows = [dict(operation_id=str(i), status='success', elapsed_s=x, method='WINDOW', repeat=1)
                for i, x in enumerate([1,2,3,4])]
        rows.append(dict(operation_id='timeout', status='timeout', elapsed_s=100, method='WINDOW', repeat=1))
        result = summarize_latency(rows)
        self.assertEqual((result['n_success'],result['n_attempts']), (4,5))
        self.assertEqual(result['latency_s']['p50'], 2.5)
        self.assertAlmostEqual(result['latency_s']['p95'], 3.85)
        self.assertAlmostEqual(result['latency_s']['p99'], 3.97)
        self.assertEqual(result['failures'], {'timeout':1})
        self.assertIsNone(summarize_latency(rows[-1:])['latency_s'])
        with self.assertRaises(ValueError):
            summarize_latency(rows + [dict(rows[0], method='BATCH')])
        with self.assertRaises(ValueError):
            summarize_latency(rows + [dict(rows[0], repeat=2)])
        with self.assertRaises(ValueError):
            summarize_latency([dict(rows[0], elapsed_s=-1)])

    def events(self, times):
        return [dict(operation_id=str(i), completed_s=t, clock_domain='host:epoch') for i,t in enumerate(times)]

    def test_btc045_fixed_deadline_golden(self):
        events = self.events([.2,.7,1.2,2.2,2.5])
        result = throughput(events + [events[2]], 0, 2.5, window_kind='fixed_deadline', clock_domain='host:epoch')
        self.assertEqual([b['count'] for b in result['buckets']], [2,1,1])
        self.assertEqual([b['duration_s'] for b in result['buckets']], [1,1,.5])
        self.assertEqual([b['rate_per_s'] for b in result['buckets']], [2,1,2])
        self.assertEqual(result['overall_per_s'], 1.6)
        self.assertEqual(result['deadline_outside'], ['4'])

    def test_btc045_drain_integer_end_and_zero_duration(self):
        result = throughput(self.events([1,2,2]), 0, 2, window_kind='natural_drain', clock_domain='host:epoch')
        self.assertEqual([b['count'] for b in result['buckets']], [0,3])
        self.assertEqual(result['overall_per_s'], 1.5)
        result = throughput(self.events([0]), 0, 0, window_kind='natural_drain', clock_domain='host:epoch')
        self.assertIsNone(result['overall_per_s'])
        self.assertEqual(result['buckets'], [])

    def test_btc045_coverage_and_clock_domains(self):
        result = throughput(self.events([.2,2.2]), 0, 3, window_kind='fixed_deadline', clock_domain='host:epoch', coverage_intervals=[(0,1),(2,3)])
        self.assertEqual([b['count'] for b in result['buckets']], [1,None,1])
        self.assertIsNone(result['overall_per_s'])
        result = throughput([], 0, 2, window_kind='fixed_deadline', clock_domain='host:epoch')
        self.assertEqual([b['count'] for b in result['buckets']], [0,0])
        with self.assertRaises(ValueError):
            throughput([dict(self.events([1])[0],clock_domain='other:epoch')],0,2,window_kind='natural_drain',clock_domain='host:epoch')

    def test_btc046_active_union_limits_numerator(self):
        result = throughput(self.events([.5,1.5,2.5,3.5,4.5]), 0, 5, window_kind='fixed_deadline', clock_domain='host:epoch', active_intervals=[(0,2),(1,3),(4,5)])
        self.assertEqual(result['active_duration_s'], 4)
        self.assertEqual(result['active_count'], 4)
        self.assertEqual(result['active_per_s'], 1)

    def test_btc034_projection_unified_and_locked_awk(self):
        mib=1048576
        rounding = capture_awk_rounding(200,4)
        result = projection(1000*mib,[800*mib],4,4,100*mib, domain='node:boot', sample_domain='node:boot', window=[0,2], compatibility=rounding)
        self.assertEqual(result['compatibility']['estimated_remaining'],16)
        self.assertEqual(result['unified']['estimated_total'],18)
        self.assertEqual(result['unified']['estimated_additional'],14)
        result = projection(1000*mib,[800*mib],4,3,100*mib, domain='n',sample_domain='n',window=[0,2], compatibility=rounding)
        self.assertEqual(result['unified']['status'],'invalid')
        self.assertEqual(result['compatibility']['estimated_remaining'],16)
        self.assertEqual(capture_awk_rounding(1,4)['rounded_mib'], '0.2')
        self.assertEqual(capture_awk_rounding(11,20)['rounded_mib'], '0.6')
        self.assertEqual(capture_awk_rounding(23,20)['rounded_mib'], '1.1')

    def test_btc034_projection_threshold_domain_and_budget_conflict(self):
        mib=1048576
        result=projection(1000*mib,[998*mib],4,4,0,domain='n',sample_domain='n',window=[0,1],compatibility=capture_awk_rounding(2,4))
        self.assertEqual(result['compatibility']['status'],'invalid')
        result=projection(1000*mib,[800*mib],4,4,900*mib,domain='n',sample_domain='n',window=[0,1])
        self.assertEqual(result['unified']['reason'],'model_budget_conflict')
        result=projection(1000*mib,[800*mib],4,4,0,domain='n',sample_domain='other',window=[0,1])
        self.assertEqual(result['unified']['status'],'unavailable')

    def test_late_completions_are_separate_from_on_time_throughput(self):
        events=self.events([.2,.7])
        events[1]['late']=True
        result=throughput(events,0,1,window_kind='fixed_deadline',clock_domain='host:epoch')
        self.assertEqual(result['count'],1)
        self.assertEqual(result['late_operations'],['1'])

    def test_btc034_integer_mib_truncation_and_reference_lock(self):
        mib=1048576
        ref=capture_awk_rounding(200,4)
        result=projection(1000*mib+123,[800*mib+456],4,4,100*mib,
                          domain='n',sample_domain='n',window=[0,2],compatibility=ref)
        self.assertEqual(result['compatibility']['estimated_remaining'],16)
        self.assertEqual(result['compatibility']['baseline_mib'],1000)
        self.assertEqual(result['unified']['cost_numerator'],209714867)
        self.assertEqual(result['unified']['cost_denominator'],4)
        with self.assertRaises(ValueError):
            capture_awk_rounding(200,4,expected_sha256='0'*64)
        result=projection(1000*mib,[1100*mib],4,4,0,domain='n',sample_domain='n',window=[0,2])
        self.assertEqual(result['unified']['reason'],'nonpositive_cost')

    def test_old_projection_records_real_awk_reference_without_python_round(self):
        from sandbox_bench.density import projection as legacy_projection
        mib=1048576
        result=legacy_projection(100*mib,[{'mem_available_bytes':94*mib}]*2,7,7,0)
        self.assertIn('reference',result['legacy'])
        self.assertEqual(result['legacy']['reference']['locale'],'C')
        self.assertEqual(result['legacy']['per_requested_mib'],.9)
        conflict=legacy_projection(1000*mib,[{'mem_available_bytes':800*mib}]*2,4,4,900*mib)
        self.assertEqual(conflict['status'],'invalid')
        self.assertEqual(conflict['unified']['reason'],'model_budget_conflict')

    def test_projection_requires_versioned_real_window(self):
        mib=1048576
        for window in (None,[1,1],[2,1],[0,float('nan')]):
            result=projection(1000*mib,[800*mib],4,4,100*mib,domain='n',sample_domain='n',window=window)
            self.assertEqual(result['unified']['status'],'unavailable')
            self.assertNotIn('estimated_total',result['unified'])
        result=projection(1000*mib,[800*mib],4,4,100*mib,domain='n',sample_domain='n',
                          window=[2,3],window_version='collector.v2')
        self.assertEqual(result['window_version'],'collector.v2')
        self.assertEqual(result['model_version'],'linear_memavailable.v1')
        self.assertFalse(result['measured_capacity'])
