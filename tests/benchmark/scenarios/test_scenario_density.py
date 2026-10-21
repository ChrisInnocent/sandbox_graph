"""BTC-031/032/033/034/035/036: actual cohort change, finite budgets and raw resources."""
import unittest
from tests.benchmark.scenarios.test_scenarios import case, ManualClock
from tests.benchmark.scenarios.test_load import ScriptedJournal
from sandbox_bench.load import OperationResult
from sandbox_bench.scenario_density import CohortSnapshot, DensityController


def density_case(method, **kwargs):
    return case(method, resources={'interval_s':1,'window_s':2,'max_wait_s':5,'max_gap_s':2,
                    'min_samples':2,'inventory_ref':'inventory'},
                density={'target_node':'node','initial_count':0,'batch_size':2,'min_growth':2,
                    'stall_limit':2,'max_batches':5,'max_instances':20,'batch_gap_s':1,
                    'reserve_bytes':100*1048576,'slo':{'p95':2}}, **kwargs)


def cohort(ids=(), ready=None, *, complete=True, scope='scope'):
    return CohortSnapshot(tuple(ids), tuple(ids if ready is None else ready), 'node','env',scope,complete)


class DensityTests(unittest.IsolatedAsyncioTestCase):
    async def run_controller(self, contract, snapshots, *, metrics=None, resource_window=True):
        clock=ManualClock()
        snapshots=iter(snapshots)
        async def inspect():return next(snapshots)
        captures=[]
        async def resource():
            started=clock()
            await clock.sleep(.125)
            row={'domain':'node','mem_available_bytes':(1000 if not captures else 800)*1048576,
                 'evidence_ref':f'raw:{len(captures)}','metrics':metrics or {}}
            if resource_window:
                row.update(window=[started,clock()],window_version='manual_resource.v1',window_kind='declared_snapshot')
            captures.append(row)
            return row
        calls=[]
        async def create(index):
            calls.append((index,clock()))
            await clock.sleep(1)
            return OperationResult('success',value={'sandbox_id':f's{index}'})
        report=await clock.run(DensityController(contract,cohort_inspector=inspect,
            resource_snapshot=resource,clock=clock,sleep=clock.sleep).run(create,journal=ScriptedJournal()))
        return report,calls,captures

    async def test_growth_list_uses_net_growth_and_stops_after_two_stalls(self):
        c=density_case('B10.GROWTH_LIST',load={'mode':'WINDOW','requests':10,'concurrency':2,
                       'run_timeout_s':30,'max_unknown_external':2})
        report,calls,_=await self.run_controller(c,[cohort(),cohort(('s0','s1')),
            cohort(('s1','s2')),cohort(('s2','s4'))])
        self.assertEqual(report.stop_reason,'plateau')
        self.assertEqual([row['net_growth'] for row in report.trace],[2,0,0])
        self.assertEqual(len(calls),6)
        self.assertEqual([i for i,_ in calls],list(range(6)))
        self.assertFalse(report.capacity_measured)

    async def test_ready_and_list_profiles_do_not_share_stall_counter(self):
        c=density_case('B10.GROWTH_READY',load={'mode':'WINDOW','requests':10,'concurrency':2,
                       'run_timeout_s':30,'max_unknown_external':2})
        report,calls,_=await self.run_controller(c,[cohort(),cohort(('s0','s1'),('s0',)),
            cohort(('s0','s1','s2','s3'),('s0',))])
        self.assertEqual(report.stop_reason,'plateau')
        self.assertEqual([r['selected_growth'] for r in report.trace],[1,0])
        self.assertEqual([r['net_growth'] for r in report.trace],[2,2])
        self.assertEqual(len(calls),4)

    async def test_recovered_growth_resets_consecutive_stalls(self):
        c=density_case('B10.GROWTH_LIST',load={'mode':'WINDOW','requests':10,'concurrency':2,
                       'run_timeout_s':30,'max_unknown_external':2})
        report,_,_=await self.run_controller(c,[cohort(),cohort(('s0',)),cohort(('s0','s2','s3')),
            cohort(('s0','s2','s3','s4')),cohort(('s0','s2','s3','s4','s6'))])
        self.assertEqual([r['stall_batches'] for r in report.trace],[1,0,1,2])
        self.assertEqual(report.stop_reason,'plateau')

    async def test_incomplete_inventory_or_changed_scope_never_claims_plateau(self):
        c=density_case('B10.GROWTH_LIST')
        report,calls,_=await self.run_controller(c,[cohort(complete=False)])
        self.assertEqual(calls,[])
        self.assertEqual(report.status,'insufficient')
        self.assertEqual(report.stop_reason,'incomplete_inventory')
        report,calls,_=await self.run_controller(c,[cohort(),cohort(('s0','s1'),scope='changed')])
        self.assertEqual(report.status,'invalid')
        self.assertEqual(report.stop_reason,'cohort_scope_changed')

    async def test_fixed_request_counts_are_not_final_cohort_and_resources_coexist(self):
        report,calls,resources=await self.run_controller(density_case('B10.FIXED_REQUESTS.WINDOW'),
            [cohort(),cohort(('s1','s2','s3'))])
        self.assertEqual(report.created_count,4)
        self.assertEqual(len(report.final_cohort.ids),3)
        self.assertEqual(len(resources),2)
        self.assertEqual(report.status,'complete')
        self.assertFalse(report.capacity_measured)

    async def test_projection_uses_fixed_healthy_cohort_and_exact_raw_memory(self):
        report,_,_=await self.run_controller(density_case('B10.PROJECTION'),
            [cohort(),cohort(('s0','s1','s2','s3'))])
        self.assertEqual(report.projection['unified']['estimated_total'],18)
        self.assertEqual(report.projection['unified']['estimated_additional'],14)
        report,_,_=await self.run_controller(density_case('B10.PROJECTION'),
            [cohort(),cohort(('s0','s1','s2','s3'),('s0','s1','s2'))])
        self.assertEqual(report.projection['unified']['status'],'invalid')

    async def test_slo_verify_does_not_create_and_requires_explicit_comparison_direction(self):
        c=density_case('B10.SLO_VERIFY')
        c=c.model_copy(update={'density':c.density.model_copy(update={'initial_count':1})})
        report,calls,_=await self.run_controller(c,[cohort(('existing',))],metrics={'p95':{'value':1,'operator':'<=','evidence_ref':'sample'}})
        self.assertEqual(calls,[])
        self.assertEqual(report.slo['status'],'pass')
        self.assertEqual(report.creation_throughput_status,'not_applicable')
        report,_,_=await self.run_controller(c,[cohort(('existing',))],metrics={'p95':{'value':1,'evidence_ref':'sample'}})
        self.assertEqual(report.slo['status'],'not_evaluated')

    async def test_platform_noise_refuses_nonzero_test_cohort_before_baseline(self):
        c=case('B09.PLATFORM',resources={'interval_s':1,'window_s':2,'max_wait_s':5,
            'max_gap_s':2,'min_samples':2,'inventory_ref':'inventory'})
        report,calls,resources=await self.run_controller(c,[cohort(('unexpected',))])
        self.assertEqual(report.stop_reason,'platform_baseline_not_empty')
        self.assertEqual(calls,[])
        self.assertEqual(resources,[])

    async def test_ready_growth_counts_new_healthy_members_separately_from_net_churn(self):
        c=density_case('B10.GROWTH_READY',load={'mode':'WINDOW','requests':6,'concurrency':2,
                       'run_timeout_s':30,'max_unknown_external':2})
        report,_,_=await self.run_controller(c,[cohort(),cohort(('s0','s1')),
            cohort(('s2','s3')),cohort(('s4','s5'))])
        self.assertEqual([r['selected_growth'] for r in report.trace],[2,2,2])
        self.assertEqual([r['ready_net_growth'] for r in report.trace],[2,0,0])
        self.assertEqual(report.trace[1]['lost_ready_ids'],['s0','s1'])
        self.assertEqual(report.stop_reason,'request_budget')

    async def test_list_growth_compatibility_stop_distinguishes_listed_from_ready(self):
        c=density_case('B10.GROWTH_LIST',load={'mode':'WINDOW','requests':6,'concurrency':2,
                       'run_timeout_s':30,'max_unknown_external':2})
        report,_,_=await self.run_controller(c,[cohort(),cohort(('s0',),()),cohort(('s0',),())])
        self.assertEqual(report.stop_reason,'plateau')
        self.assertEqual(report.normalized_stop_reason,'setup_failure')

    async def test_projection_rejects_nonempty_baseline_before_creating_more(self):
        c=density_case('B10.PROJECTION')
        c=c.model_copy(update={'density':c.density.model_copy(update={'initial_count':1})})
        report,calls,_=await self.run_controller(c,[cohort(('existing',))])
        self.assertEqual(report.stop_reason,'projection_requires_zero_instance_baseline')
        self.assertEqual(calls,[])

    async def test_projection_open_loop_preserves_offered_schedule(self):
        c=density_case('B10.PROJECTION',load={'mode':'OPEN_LOOP','requests':4,'concurrency':1,
            'run_timeout_s':20,'max_unknown_external':2,'arrival_rate':2,'max_queue':0,'late_policy':'drop'})
        report,calls,_=await self.run_controller(c,[cohort(),cohort(('s0','s2'))])
        self.assertEqual(calls,[(0,.125),(2,1.125)])
        self.assertEqual(report.load_reports[0].dropped,2)
        self.assertEqual(report.projection['unified']['status'],'invalid')

    async def test_projection_missing_observation_window_cannot_claim_valid_model(self):
        report,_,_=await self.run_controller(density_case('B10.PROJECTION'),
            [cohort(),cohort(('s0','s1','s2','s3'))],resource_window=False)
        self.assertEqual(report.projection['unified']['status'],'unavailable')

    async def test_idle_noise_preserves_zero_baseline_and_prepared_cohort(self):
        c=case('B09.IDLE',resources={'interval_s':1,'window_s':2,'max_wait_s':5,
            'max_gap_s':2,'min_samples':2,'inventory_ref':'inventory'})
        report,calls,resources=await self.run_controller(c,[cohort(),cohort(('s0','s1','s2','s3'))])
        self.assertEqual(report.status,'complete')
        self.assertEqual(len(resources),2)
        self.assertEqual(report.creation_throughput_status,'not_applicable')
        self.assertEqual(len(calls),4)

    async def test_paused_noise_requires_actual_paused_ids_after_authorized_preparation(self):
        c=case('B09.PAUSED',resources={'interval_s':1,'window_s':2,'max_wait_s':5,
            'max_gap_s':2,'min_samples':2,'inventory_ref':'inventory'})
        clock=ManualClock()
        snapshots=iter([cohort(),CohortSnapshot(('s0','s1','s2','s3'),(),'node','env','scope',True,('s0','s1','s2','s3'))])
        async def inspect():return next(snapshots)
        async def resource():return {'domain':'node','evidence_ref':'raw'}
        async def operation(index):return OperationResult('success',value={'sandbox_id':f's{index}'})
        paused=[]
        async def pause(ids):paused.extend(ids)
        report=await clock.run(DensityController(c,cohort_inspector=inspect,resource_snapshot=resource,
            pause_cohort=pause,clock=clock,sleep=clock.sleep).run(operation,journal=ScriptedJournal()))
        self.assertEqual(report.status,'complete')
        self.assertEqual(paused,['s0','s1','s2','s3'])
        self.assertNotIn('pss_bytes',report.resource_observations[-1])
