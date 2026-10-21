"""BTC-005–007: independent scripted acceptance, poll, execution, and epochs."""
import importlib.util
from pathlib import Path
import unittest

from sandbox_bench.completion import (Acceptance, CompletionObserver, CompletionSpec,
                                     StateObservation, TerminalObservation)
from sandbox_bench.load import LoadEngine, LoadSchedule, OperationResult

spec = importlib.util.spec_from_file_location('completion_clock', Path(__file__).parents[2] / 'fixtures/benchmark/load_clock.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
ManualClock, ScriptedJournal = fixture.ManualClock, fixture.ScriptedJournal


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_btc005_create201_keeps_slot_until_readonly_running(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='epoch-a')
        submissions, inspections, results = [], [], []
        async def operation(index):
            submitted = clock()
            submissions.append(submitted)
            acceptance = Acceptance(True, clock(), 'epoch-a', handle=f'sid-{index}')
            async def inspect(handle):
                inspections.append((handle, clock()))
                return StateObservation('running' if clock() >= submitted + 2 else 'starting', 'epoch-a')
            result = await observer.observe(CompletionSpec('STATE_READY', 5, 1),
                submitted_at=submitted, process_epoch='epoch-a', accepted=acceptance, inspect=inspect)
            results.append(result)
            return OperationResult(result.status, value=result)
        await clock.run(LoadEngine(clock=clock, sleep=clock.sleep).run(
            LoadSchedule('WINDOW', 2, 1, 10), operation, journal=ScriptedJournal()))
        self.assertEqual(submissions, [0, 2])
        self.assertEqual(inspections, [('sid-0', 0), ('sid-0', 1), ('sid-0', 2), ('sid-1', 2), ('sid-1', 3), ('sid-1', 4)])
        self.assertEqual([r.latency for r in results], [2, 2])
        async def failed_command():
            await clock.sleep(3)
            return False
        validation = await clock.run(observer.validate(failed_command))
        self.assertFalse(validation.success)
        self.assertEqual(results[0].status, 'success')
        self.assertEqual(results[0].observed_at, 2)

    async def test_btc006_exec_endpoint_waits_for_actual_execution_assertion(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='a')
        async def execute(handle):
            await clock.sleep(4)
            return TerminalObservation(True, 'a')
        result = await clock.run(observer.observe(CompletionSpec('EXEC_COMPLETE', 10, 1),
            submitted_at=0, process_epoch='a', accepted=Acceptance(True, 0, 'a'), execute=execute))
        self.assertEqual(result.observed_at, 4)
        self.assertEqual(result.latency, 4)
        self.assertEqual(result.status, 'success')

    async def test_api_complete_has_no_implicit_exec_or_state_probe(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='a')
        clock.now = 1
        result = await observer.observe(CompletionSpec('API_COMPLETE', 10, 1),
            submitted_at=0, process_epoch='a', accepted=Acceptance(True, 1, 'a'))
        self.assertEqual((result.status, result.latency), ('success', 1))

    async def test_btc007_unknown_then_terminal_failure_not_success(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='a')
        async def inspect(handle):
            return StateObservation('unknown' if clock() < 2 else 'failed', 'a')
        result = await clock.run(observer.observe(CompletionSpec('STATE_READY', 5, 1),
            submitted_at=0, process_epoch='a', accepted=Acceptance(True, 0, 'a'), inspect=inspect))
        self.assertEqual(result.status, 'failed')
        self.assertIsNone(result.latency)
        self.assertEqual([p.state for p in result.polls], ['unknown', 'unknown', 'failed'])

    async def test_btc007_timeout_late_success_never_changes_original(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='a')
        async def inspect(handle):
            return StateObservation('starting', 'a')
        result = await clock.run(observer.observe(CompletionSpec('STATE_READY', 2, 1),
            submitted_at=0, process_epoch='a', accepted=Acceptance(True, 0, 'a'), inspect=inspect))
        self.assertEqual(result.status, 'timeout')
        self.assertEqual(result.observed_at, 2)
        self.assertIsNone(result.latency)
        clock.now = 5
        late = observer.record_late(result, StateObservation('running', 'a'))
        self.assertEqual((late.status, late.observed_at), ('late_success', 5))
        self.assertEqual(result.status, 'timeout')
        self.assertIsNone(result.latency)

    async def test_btc007_cross_epoch_never_subtracts_monotonic_time(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='new')
        result = await observer.observe(CompletionSpec('API_COMPLETE', 10, 1),
            submitted_at=100, process_epoch='old', accepted=Acceptance(True, 0, 'new'))
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(result.reason, 'cross_epoch')
        self.assertEqual(result.submitted_epoch, 'old')
        self.assertEqual(result.process_epoch, 'new')
        self.assertIsNone(result.latency)

    async def test_btc007_poll_network_delay_is_observation_time_not_internal_time(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='a')
        async def inspect(handle):
            await clock.sleep(3)
            return StateObservation('running', 'a', source_event_time='server-clock:1')
        result = await clock.run(observer.observe(CompletionSpec('STATE_READY', 5, 1),
            submitted_at=0, process_epoch='a', accepted=Acceptance(True, 0, 'a'), inspect=inspect))
        self.assertEqual(result.latency, 3)
        self.assertEqual(result.polls[0].source_event_time, 'server-clock:1')
        self.assertIsNone(result.internal_duration_ns)

    async def test_btc007_hanging_poll_is_bounded_and_deadline_is_not_success(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='a')
        async def inspect(handle):
            await clock.sleep(2)
            return StateObservation('running', 'a')
        result = await clock.run(observer.observe(CompletionSpec('STATE_READY', 2, 1),
            submitted_at=0, process_epoch='a', accepted=Acceptance(True, 0, 'a'), inspect=inspect))
        self.assertEqual(result.status, 'timeout')
        self.assertEqual(result.observed_at, 2)

    async def test_server_terminal_keeps_internal_duration_separate(self):
        clock = ManualClock()
        observer = CompletionObserver(clock=clock, sleep=clock.sleep, process_epoch='a')
        async def server_terminal(handle):
            await clock.sleep(4)
            return TerminalObservation(True, 'a', internal_duration_ns=1500000000, source_event_time='server:2')
        result = await clock.run(observer.observe(CompletionSpec('SERVER_TERMINAL', 10, 1),
            submitted_at=0, process_epoch='a', accepted=Acceptance(True, 0, 'a'), server_terminal=server_terminal))
        self.assertEqual((result.latency, result.internal_duration_ns), (4, 1500000000))
        self.assertEqual(result.source_event_time, 'server:2')
