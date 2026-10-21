import unittest
from sandbox_bench.metrics import cpu_intervals, cpu_summary, resource_mean, cohort_pss


class ResourceMetricsTests(unittest.TestCase):
    def samples(self):
        return [dict(t_s=t,cpu_seconds=c,identity='pid42:start7',clock_domain='n:boot',component='a')
                for t,c in [(0,0),(2,3),(5,9)]]

    def test_btc040_cpu_uses_actual_elapsed_and_segments_resets(self):
        intervals=cpu_intervals(self.samples())
        self.assertEqual([i['cores'] for i in intervals],[1.5,2])
        result=cpu_summary(intervals,0,5)
        self.assertEqual(result['cpu_seconds'],9)
        self.assertEqual(result['mean_cores'],1.8)
        self.assertEqual(result['sampled_peak_cores'],2)
        samples=self.samples()
        samples[2]['identity']='pid42:start8'
        self.assertEqual(len(cpu_intervals(samples)),1)
        samples[2]['identity']='pid42:start7'
        samples[2]['cpu_seconds']=1
        self.assertEqual(len(cpu_intervals(samples)),1)

    def test_btc041_sum_before_peak_not_sum_peaks(self):
        rows=[dict(start_s=s,end_s=s+1,cores=c,cpu_seconds=c,component=k,clock_domain='n:boot')
              for k,values in [('a',[8,1]),('b',[1,8])] for s,c in enumerate(values)]
        result=cpu_summary(rows,0,2)
        self.assertEqual(result['sampled_peak_cores'],9)
        self.assertEqual(result['component_peaks'],{'a':8,'b':8})
        rows[-1]['clock_domain']='other:boot'
        result=cpu_summary(rows,0,2)
        self.assertIsNone(result['sampled_peak_cores'])
        self.assertEqual(result['reason'],'unaligned_clock_domains')

    def test_btc040_missing_endpoint_and_domain_gap_not_zero(self):
        result=cpu_summary(cpu_intervals(self.samples()[:2]),0,5)
        self.assertEqual(result['coverage'],.4)
        self.assertIsNone(result['mean_cores'])
        self.assertEqual(result['covered_mean_cores'],1.5)

    def test_btc046_time_weighted_pss_never_bridges_gap(self):
        rows=[{'t_s':0,'value':10,'clock_domain':'n:b','cohort':['a']},
              {'t_s':1,'value':20,'clock_domain':'n:b','cohort':['a']},
              {'t_s':4,'value':30,'clock_domain':'n:b','cohort':['a']}]
        result=resource_mean(rows,0,4,max_gap_s=3)
        self.assertEqual(result['mean'],17.5)
        result=resource_mean(rows,0,4,max_gap_s=2)
        self.assertEqual(result['coverage'],.25)
        self.assertIsNone(result['mean'])
        self.assertEqual(result['covered_mean'],10)
        rows[1]['cohort']=['b']
        self.assertIsNone(resource_mean(rows,0,4,max_gap_s=3)['mean'])

    def test_btc042_cohort_is_actual_set_and_subset_denominator(self):
        mib=1048576
        result=cohort_pss(['a','b'],{'a':20*mib,'b':30*mib},shared_bytes=10*mib)
        self.assertEqual(result['direct_mean_bytes'],25*mib)
        self.assertEqual(result['with_shared_mean_bytes'],30*mib)
        result=cohort_pss(['a','b'],{'a':20*mib},shared_bytes=10*mib)
        self.assertEqual(result['coverage'],.5)
        self.assertEqual(result['observed_mean_bytes'],20*mib)
        self.assertIsNone(result['direct_mean_bytes'])
        self.assertIsNone(result['with_shared_mean_bytes'])

    def test_missing_entire_component_cannot_look_like_complete_total(self):
        intervals=cpu_intervals(self.samples())
        result=cpu_summary(intervals,0,5,expected_components=['a','b'])
        self.assertEqual(result['coverage'],0)
        self.assertIsNone(result['sampled_peak_cores'])
        self.assertEqual(result['component_cpu_seconds'],{'a':9})

    def test_cpu_seconds_retains_observed_components_with_partial_overlap(self):
        rows=[dict(start_s=0,end_s=2,cores=2,cpu_seconds=4,component='a',clock_domain='n'),
              dict(start_s=1,end_s=2,cores=3,cpu_seconds=3,component='b',clock_domain='n')]
        result=cpu_summary(rows,0,2)
        self.assertEqual(result['coverage'],.5)
        self.assertEqual(result['component_cpu_seconds'],{'a':4,'b':3})
        self.assertEqual(result['aligned_cpu_seconds'],5)
        self.assertEqual(result['cpu_seconds'],7)
        self.assertIsNone(result['mean_cores'])

    def test_counter_discontinuity_reasons_are_explicit(self):
        from sandbox_bench.metrics import cpu_segments
        rows=self.samples()
        rows[1]['cpu_seconds']=None
        result=cpu_segments(rows)
        self.assertEqual(result['intervals'],[])
        self.assertEqual([g['reason'] for g in result['gaps']],['missing_endpoint','missing_endpoint'])
        rows=self.samples()
        rows[1]['cpu_seconds']=-1
        with self.assertRaises(ValueError):
            cpu_segments(rows)
        rows=self.samples()
        rows[2]['cpu_seconds']=1
        result=cpu_segments(rows)
        self.assertEqual(result['gaps'][0]['reason'],'counter_reset')
