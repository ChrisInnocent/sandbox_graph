import unittest
from sandbox_bench.metrics import summarize_quality, summarize_repeats


class QualityTests(unittest.TestCase):
    def test_api_acceptance_completion_and_final_cohort_are_separate(self):
        rows=[{'operation_id':'a','status':'success','api_status':'accepted','sandbox_id':'gone'},
              {'operation_id':'b','status':'timeout','api_status':'accepted','unknown':True},
              {'operation_id':'c','status':'failed','api_status':'error','reason':'quota'},
              {'operation_id':'d','status':'success','api_status':'success','sandbox_id':'alive'}]
        result=summarize_quality(rows,final_cohort=['alive'])
        self.assertEqual(result['n_attempts'],4)
        self.assertEqual(result['api_success_count'],3)
        self.assertEqual(result['completed_success_count'],2)
        self.assertEqual(result['final_cohort_count'],1)
        self.assertEqual(result['unknown_count'],1)
        self.assertEqual(result['failure_reasons'],{'timeout':1,'quota':1})

    def test_repeats_are_stratified_and_raw_pool_is_explicit(self):
        rows=[{'operation_id':'a','status':'success','elapsed_s':1,'method':'WINDOW','repeat':1},
              {'operation_id':'b','status':'success','elapsed_s':9,'method':'WINDOW','repeat':2}]
        result=summarize_repeats(rows)
        self.assertEqual(result['repeats'][1]['latency_s']['p99'],1)
        self.assertEqual(result['repeats'][2]['latency_s']['p99'],9)
        self.assertNotIn('pooled',result)
        result=summarize_repeats(rows,pool_raw=True)
        self.assertAlmostEqual(result['pooled']['latency_s']['p99'],8.92)
        with self.assertRaises(ValueError):
            summarize_repeats([rows[0],dict(rows[1],method='BATCH')],pool_raw=True)

    def test_cross_epoch_success_duration_is_rejected(self):
        from sandbox_bench.metrics import summarize_latency
        with self.assertRaises(ValueError):
            summarize_latency([{'status':'success','elapsed_s':2,'start_clock_domain':'e1','end_clock_domain':'e2'}])
