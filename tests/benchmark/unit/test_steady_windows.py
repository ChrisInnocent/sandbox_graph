import unittest
from sandbox_bench.steady import SteadyConfig, find_steady_window


class SteadyTests(unittest.TestCase):
    def samples(self, values):
        return [dict(t_s=i,value=v,cohort=['a'],clock_domain='n:b') for i,v in enumerate(values)]

    def config(self, **extra):
        return SteadyConfig(**dict(window_s=2,min_samples=3,max_gap_s=1,cohort_stability=True,
                                   metric_bounds=(9,11),slope_bounds=(-.1,.1),max_wait_s=8,**extra))

    def test_btc043_first_qualified_window_not_best_later(self):
        result=find_steady_window(self.samples([20,15,10,10,10,10]),self.config())
        self.assertEqual(result['status'],'steady')
        self.assertEqual((result['start_s'],result['end_s']),(2,4))

    def test_btc043_gap_and_cohort_restart_break_continuity(self):
        rows=self.samples([10,10,10,10,10])
        rows[1]['cohort']=['b']
        self.assertEqual(find_steady_window(rows,self.config())['start_s'],2)
        rows=self.samples([10,10,10])
        rows[2]['t_s']=4
        self.assertEqual(find_steady_window(rows,self.config())['status'],'insufficient_evidence')
        rows=self.samples([20]*10)
        self.assertEqual(find_steady_window(rows,self.config())['status'],'not_steady')
        rows=self.samples([10,10,10])
        rows[1]['clock_domain']='n:restart'
        self.assertEqual(find_steady_window(rows,self.config())['status'],'insufficient_evidence')

    def test_cohort_stability_requires_actual_cohort_evidence(self):
        rows=self.samples([10,10,10])
        for row in rows:
            del row['cohort']
        self.assertEqual(find_steady_window(rows,self.config())['status'],'insufficient_evidence')
