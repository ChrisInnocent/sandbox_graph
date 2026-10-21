"""BTC-016/017/018/020: hand-derived traces reject batching and 201 refill."""
import importlib.util
import json
from pathlib import Path
import unittest

from sandbox_bench.load import LoadEngine, LoadSchedule, OperationResult

FIXTURES = Path(__file__).parents[2] / 'fixtures' / 'benchmark'
spec = importlib.util.spec_from_file_location('load_clock', FIXTURES / 'load_clock.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
ManualClock, ScriptedJournal = fixture.ManualClock, fixture.ScriptedJournal
GOLDEN = json.loads((FIXTURES / 'load_golden.json').read_text())


class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def run_load(self, schedule, durations, statuses=None):
        clock = ManualClock()
        submitted, accepted, completed = [], [], []
        async def operation(index):
            submitted.append((index, clock()))
            accepted.append((index, clock(), 201))
            await clock.sleep(durations[index])
            completed.append((index, clock()))
            return OperationResult(status=(statuses or {}).get(index, 'success'))
        report = await clock.run(LoadEngine(clock=clock, sleep=clock.sleep).run(
            schedule, operation, journal=ScriptedJournal()))
        return report, submitted, completed

    async def test_btc016_window_golden_201_does_not_refill(self):
        report, submitted, completed = await self.run_load(
            LoadSchedule('WINDOW', 5, 2, 20), GOLDEN['service_seconds'])
        self.assertEqual([t for _, t in submitted], [0, 0, 1, 3, 3])
        self.assertEqual(completed, [(0, 1), (1, 3), (2, 3), (3, 4), (4, 4)])
        self.assertEqual(report.max_active_slots, 2)
        self.assertEqual(report.allocated, 5)
        self.assertEqual(report.successes, 5)

    async def test_btc017_batch_golden_has_no_tail_gap(self):
        report, submitted, completed = await self.run_load(
            LoadSchedule('BATCH', 5, 2, 20, batch_size=2, gap=2), GOLDEN['service_seconds'])
        self.assertEqual([t for _, t in submitted], [0, 0, 5, 5, 9])
        self.assertEqual(dict(completed), {0: 1, 1: 3, 2: 7, 3: 6, 4: 10})
        self.assertEqual(report.finished_at, 10)
        self.assertEqual(report.successes / report.wall_seconds, 0.5)

    async def test_btc017_batch_larger_than_concurrency_failure_consumes_budget(self):
        report, submitted, _ = await self.run_load(
            LoadSchedule('BATCH', 5, 2, 20, batch_size=3, gap=2), [1, 3, 2, 1, 1], {0: 'failed'})
        self.assertEqual([t for _, t in submitted], [0, 0, 1, 5, 5])
        self.assertEqual(report.successes, 4)
        self.assertEqual(report.max_active_slots, 2)

    async def test_btc018_open_loop_drop_preserves_arrival_schedule(self):
        report, submitted, _ = await self.run_load(LoadSchedule(
            'OPEN_LOOP', 5, 1, 20, scheduled_at=(0, 1, 2, 3, 4), late_policy='drop'), [3]*5)
        self.assertEqual(submitted, [(0, 0), (3, 3)])
        self.assertEqual(report.dropped, 3)
        self.assertFalse(report.target_rate_achieved)
        self.assertEqual([e.at for e in report.events if e.kind == 'scheduled'], [0, 1, 2, 3, 4])

    async def test_btc018_open_loop_queue_is_bounded(self):
        report, submitted, _ = await self.run_load(LoadSchedule(
            'OPEN_LOOP', 5, 1, 20, scheduled_at=(0, 1, 2, 3, 4), late_policy='queue', max_queue=1), [3]*5)
        self.assertEqual(submitted, [(0, 0), (1, 3), (3, 6)])
        self.assertEqual(report.dropped, 2)
        self.assertEqual(report.max_queued, 1)
        self.assertEqual([e.generator_lag for e in report.events if e.kind == 'submit'], [0, 2, 3])
        self.assertFalse(report.target_rate_achieved)

    async def test_btc020_timeout_stops_at_external_unknown_cap(self):
        report, submitted, _ = await self.run_load(
            LoadSchedule('WINDOW', 5, 1, 1, unknown_limit=1), [10]*5)
        self.assertEqual(submitted, [(0, 0)])
        self.assertEqual(report.unresolved_external, 1)
        self.assertEqual(report.stop_reason, 'unknown_limit')
        self.assertEqual(report.successes, 0)
        self.assertEqual(report.results[0].status, 'timeout')

    async def test_deadline_stops_new_work_and_drains_bounded_active(self):
        report, submitted, _ = await self.run_load(
            LoadSchedule('WINDOW', 5, 1, 10, deadline=2), [3]*5)
        self.assertEqual(submitted, [(0, 0)])
        self.assertEqual(report.finished_at, 3)
        self.assertEqual(report.stop_reason, 'deadline')

    async def test_child_weight_reserves_atomically_and_never_splits(self):
        report, submitted, _ = await self.run_load(LoadSchedule(
            'WINDOW', 3, 3, 20, child_weights=(2, 2, 1), child_budget=5, parent_budget=3), [1, 1, 1])
        self.assertEqual(submitted, [(0, 0), (1, 1), (2, 1)])
        self.assertEqual(report.max_active_slots, 3)
        for kwargs in ({'child_weights': (4,), 'child_budget': 4},
                       {'child_weights': (2,), 'child_budget': 1}):
            with self.assertRaises(ValueError):
                LoadSchedule('WINDOW', 1, 3, 20, **kwargs)

    async def test_no_network_call_after_journal_denies_permission(self):
        clock, calls = ManualClock(), []
        class Denied(ScriptedJournal):
            async def submission_started(self, token):
                raise RuntimeError('fenced')
        async def operation(index):
            calls.append(index)
            return OperationResult('success')
        report = await clock.run(LoadEngine(clock=clock, sleep=clock.sleep).run(
            LoadSchedule('WINDOW', 3, 1, 2), operation, journal=Denied()))
        self.assertEqual(calls, [])
        self.assertEqual(report.stop_reason, 'journal_error')
        self.assertEqual(report.results[0].status, 'unsent')

    async def test_zero_unknown_limit_allows_work_but_stops_on_first_unknown(self):
        report, submitted, _ = await self.run_load(
            LoadSchedule('WINDOW', 5, 1, 1, unknown_limit=0), [10]*5)
        self.assertEqual(submitted, [(0, 0)])
        self.assertEqual(report.stop_reason, 'unknown_limit')

    async def test_unknown_result_cannot_silently_drop_external_weight(self):
        report, submitted, _ = await self.run_load(
            LoadSchedule('WINDOW', 4, 2, 5, child_weights=(2, 2, 2, 2), unknown_limit=2),
            [1]*4, {0: 'unknown'})
        self.assertEqual(submitted, [(0, 0)])
        self.assertEqual(report.unresolved_external, 2)
        self.assertEqual(report.stop_reason, 'unknown_limit')

    async def test_hanging_terminal_journal_write_stops_finite_run(self):
        clock, calls = ManualClock(), []
        class Hanging(ScriptedJournal):
            async def record_terminal(self, token, result):
                await clock.sleep(100)
        async def operation(index):
            calls.append(index)
            return OperationResult('success')
        report = await clock.run(LoadEngine(clock=clock, sleep=clock.sleep).run(
            LoadSchedule('WINDOW', 3, 1, 2), operation, journal=Hanging()))
        self.assertEqual(calls, [0])
        self.assertEqual(report.finished_at, 2)
        self.assertEqual(report.stop_reason, 'journal_error')
        self.assertEqual(report.results[0].status, 'success')

    async def test_denial_keeps_allocated_slot_and_is_never_retried(self):
        clock = ManualClock()
        journal = ScriptedJournal()
        async def deny(token):
            raise RuntimeError('ownership changed')
        journal.submission_started = deny
        async def operation(index):
            self.fail('must not submit without permission')
        report = await clock.run(LoadEngine(clock=clock, sleep=clock.sleep).run(
            LoadSchedule('WINDOW', 1, 1, 2), operation, journal=journal))
        self.assertEqual(report.allocated, 1)
        self.assertEqual(len(journal.reservations), 1)
        self.assertEqual(report.submitted, 0)

    async def test_invalid_load_numbers_rejected_before_work(self):
        for kwargs in ({'request_count': True}, {'concurrency': 0},
                       {'operation_timeout': float('inf')}, {'unknown_limit': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LoadSchedule(**{'mode': 'WINDOW', 'request_count': 1, 'concurrency': 1,
                                'operation_timeout': 2, **kwargs})

    async def test_refill_gap_includes_admission_delay_and_completion_is_not_journal_time(self):
        clock = ManualClock()
        class SlowJournal(ScriptedJournal):
            async def reserve(self, request_index, child_weight):
                await clock.sleep(1)
                return await super().reserve(request_index, child_weight)
            async def record_terminal(self, token, result):
                await clock.sleep(1)
        async def operation(index):
            await clock.sleep(1)
            return OperationResult('success')
        report = await clock.run(LoadEngine(clock=clock, sleep=clock.sleep).run(
            LoadSchedule('WINDOW', 2, 1, 10), operation, journal=SlowJournal()))
        submits = [e for e in report.events if e.kind == 'submit']
        self.assertEqual([e.at for e in submits], [1, 4])
        self.assertEqual(submits[1].refill_gap, 2)
        self.assertEqual([e.completed_at for e in report.events if e.kind == 'completed'], [2, 5])

    async def test_progress_child_release_admits_next_parent_before_first_parent_finishes(self):
        import asyncio
        clock=ManualClock()
        starts=[]
        class ChildJournal(ScriptedJournal):
            async def record_child_terminal(self,token,child_index,result,completed_at):
                return None
        async def operation(index):self.fail('progress operation must be used')
        async def progress(index,sink):
            starts.append((index,clock()))
            async def child(child_index,duration):
                await clock.sleep(duration)
                await sink(child_index,OperationResult('success'),clock())
            await asyncio.gather(child(0,1),child(1,3))
            return OperationResult('success')
        report=await clock.run(LoadEngine(clock=clock,sleep=clock.sleep).run(
            LoadSchedule('WINDOW',2,3,10,child_weights=(2,2),child_budget=4),operation,
            journal=ChildJournal(),progress_operation=progress))
        self.assertEqual(starts,[(0,0),(1,1)])
        self.assertEqual(report.max_active_slots,3)
        self.assertEqual([e.active_slots for e in report.events if e.kind=='completed'],[1,0])
        self.assertEqual(len([e for e in report.events if e.kind=='child_completed']),4)

    async def test_progress_waits_for_durable_child_receipt_and_rejects_duplicate(self):
        clock=ManualClock()
        starts=[]
        class SlowChildJournal(ScriptedJournal):
            async def record_child_terminal(self,token,child_index,result,completed_at):
                await clock.sleep(1)
        async def operation(index):self.fail('progress operation must be used')
        async def progress(index,sink):
            starts.append((index,clock()))
            await clock.sleep(1)
            await sink(0,OperationResult('success'),clock())
            with self.assertRaises(ValueError):
                await sink(0,OperationResult('success'),clock())
            await clock.sleep(2)
            return OperationResult('success')
        report=await clock.run(LoadEngine(clock=clock,sleep=clock.sleep).run(
            LoadSchedule('WINDOW',2,1,10),operation,journal=SlowChildJournal(),progress_operation=progress))
        self.assertEqual(starts,[(0,0),(1,2)])
        self.assertEqual(report.max_active_slots,1)
        self.assertEqual(report.successes,2)

    async def test_progress_unknown_is_counted_once_and_stops_refill_immediately(self):
        clock=ManualClock()
        starts=[]
        class ChildJournal(ScriptedJournal):
            async def record_child_terminal(self,token,child_index,result,completed_at):pass
        async def operation(index):self.fail('progress operation must be used')
        async def progress(index,sink):
            starts.append(index)
            await clock.sleep(1)
            await sink(0,OperationResult('unknown',1),clock())
            await clock.sleep(1)
            await sink(1,OperationResult('success'),clock())
            return OperationResult('failed',1)
        report=await clock.run(LoadEngine(clock=clock,sleep=clock.sleep).run(
            LoadSchedule('WINDOW',3,3,10,child_weights=(2,2,2),unknown_limit=1),operation,
            journal=ChildJournal(),progress_operation=progress))
        self.assertEqual(starts,[0])
        self.assertEqual(report.unresolved_external,1)
        self.assertEqual(report.stop_reason,'unknown_limit')

    async def test_progress_batch_gap_still_waits_for_all_parent_terminals(self):
        clock=ManualClock()
        starts=[]
        class ChildJournal(ScriptedJournal):
            async def record_child_terminal(self,token,child_index,result,completed_at):pass
        async def operation(index):self.fail('progress operation must be used')
        async def progress(index,sink):
            starts.append((index,clock()))
            await clock.sleep(1)
            await sink(0,OperationResult('success'),clock())
            await clock.sleep(2)
            return OperationResult('success')
        report=await clock.run(LoadEngine(clock=clock,sleep=clock.sleep).run(
            LoadSchedule('BATCH',3,1,10,batch_size=2,gap=2),operation,
            journal=ChildJournal(),progress_operation=progress))
        self.assertEqual(starts,[(0,0),(1,1),(2,6)])
        self.assertEqual(report.finished_at,9)

    async def test_progress_integration_persists_real_child_ledger_without_refunding_budget(self):
        import asyncio
        import tempfile
        from sandbox_bench.journal import FileJournal
        clock=ManualClock()
        async def operation(index):self.fail('progress operation must be used')
        async def progress(index,sink):
            async def child(child_index):
                await clock.sleep(child_index+1)
                await sink(child_index,OperationResult('success'),clock())
            await asyncio.gather(child(0),child(1))
            return OperationResult('success')
        with tempfile.TemporaryDirectory() as directory:
            journal=FileJournal(directory,'progress',child_budget=4,parent_budget=2)
            report=await clock.run(LoadEngine(clock=clock,sleep=clock.sleep).run(
                LoadSchedule('WINDOW',2,3,10,child_weights=(2,2)),operation,journal=journal,
                progress_operation=progress))
            reopened=FileJournal(directory,'progress',child_budget=4,parent_budget=2)
            state=reopened.state()
            self.assertEqual(report.successes,2)
            self.assertEqual((state['child_reserved'],state['parent_reserved']),(4,2))
            self.assertEqual([len(row['children']) for row in state['operations'].values()],[2,2])
            self.assertEqual(len([e for e in reopened.collect() if e['kind']=='child_terminal']),4)
            with self.assertRaises(ValueError):await reopened.reserve(2,1)
