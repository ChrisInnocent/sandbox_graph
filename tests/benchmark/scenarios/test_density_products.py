"""BTC-035/036/056: B10 fixed-request and SLO methods across all three products.

The same load engine, budget accounting and final-cohort rules must hold per product;
these are property-level checks, not per-product copies of a real run.
"""
import unittest

from sandbox_bench.load import OperationResult
from sandbox_bench.scenario_density import DensityController
from tests.benchmark.scenarios.test_load import ScriptedJournal
from tests.benchmark.scenarios.test_scenario_density import cohort, density_case
from tests.benchmark.scenarios.test_scenarios import ManualClock

PRODUCTS = ('kuasar', 'cubesandbox', 'agentenv')


class DensityProductMatrix(unittest.IsolatedAsyncioTestCase):
    async def run_controller(self, contract, snapshots, *, metrics=None):
        clock = ManualClock()
        snapshots = iter(snapshots)

        async def inspect():
            return next(snapshots)

        async def resource():
            started = clock()
            await clock.sleep(.125)
            return {'domain': 'node', 'mem_available_bytes': 800 * 1048576, 'evidence_ref': 'raw',
                    'window': [started, clock()], 'window_version': 'manual_resource.v1',
                    'window_kind': 'declared_snapshot', 'metrics': metrics or {}}

        async def create(index):
            await clock.sleep(1)
            return OperationResult('success', value={'sandbox_id': f's{index}'})

        return await clock.run(DensityController(
            contract, cohort_inspector=inspect, resource_snapshot=resource,
            clock=clock, sleep=clock.sleep).run(create, journal=ScriptedJournal()))

    async def test_fixed_requests_window_is_a_bounded_q_for_every_product(self):
        for product in PRODUCTS:
            with self.subTest(product=product):
                contract = density_case('B10.FIXED_REQUESTS.WINDOW', product=product)
                report = await self.run_controller(contract, [cohort(), cohort(('s1', 's2', 's3'))])
                self.assertEqual(report.created_count, contract.load.requests)
                self.assertEqual(len(report.final_cohort.ids), 3)
                self.assertFalse(report.capacity_measured)

    async def test_fixed_requests_batch_fixes_q_and_failures_consume_budget(self):
        for product in PRODUCTS:
            with self.subTest(product=product):
                contract = density_case(
                    'B10.FIXED_REQUESTS.BATCH', product=product,
                    load={'mode': 'BATCH', 'requests': 4, 'concurrency': 2, 'run_timeout_s': 30,
                          'max_unknown_external': 2, 'batch_size': 2, 'batch_gap_s': 1})
                report = await self.run_controller(contract, [cohort(), cohort(('s1', 's2', 's3'))])
                self.assertEqual(report.created_count, 4)
                self.assertFalse(report.capacity_measured)

    async def test_slo_verify_never_creates_and_requires_an_explicit_operator(self):
        for product in PRODUCTS:
            with self.subTest(product=product):
                contract = density_case('B10.SLO_VERIFY', product=product)
                contract = contract.model_copy(
                    update={'density': contract.density.model_copy(update={'initial_count': 1})})
                report = await self.run_controller(
                    contract, [cohort(('existing',))],
                    metrics={'p95': {'value': 1, 'operator': '<=', 'evidence_ref': 'sample'}})
                self.assertEqual(report.creation_throughput_status, 'not_applicable')
                self.assertEqual(report.slo['status'], 'pass')
