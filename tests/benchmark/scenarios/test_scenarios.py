"""Explicit fixture/operation boundaries for runnable base scenarios."""
import importlib.util
from pathlib import Path
import unittest

from sandbox_bench.contracts import CaseContract
from sandbox_bench.scenarios import ScenarioExecutor, ScenarioBlocked
from sandbox_bench.transport import TransportError

spec = importlib.util.spec_from_file_location('scenario_clock', Path(__file__).parents[2] / 'fixtures/benchmark/load_clock.py')
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)
ManualClock = fixture_module.ManualClock


def case(method='B01.TEMPLATE', **extra):
    data = dict(schema_version='case.v1', method_id=method, method_version='1',
        product='agentenv', product_version='synthetic', work_id='work', group_id='group',
        source={'kind':'template','id':'template','digest':'sha256:synthetic',
                'artifact_kind':'sandbox_artifact','launch_path':'cold_boot','producer_step':'pre_existing'},
        spec={'vcpus':2,'memory_bytes':2147483648},
        completion={'endpoint':'STATE_READY','timeout_s':10,'poll_interval_s':1},
        load={'mode':'WINDOW','requests':4,'concurrency':2,'run_timeout_s':20,'max_unknown_external':2},
        cleanup_timeout_s=5, ttl_s=100, cache_profile='declared-warm',
        environment_ref='env',contract_ref='contract',graph_ref='graph',plan_ref='plan',fixture_ref='fixture')
    data.update(extra)
    return CaseContract.model_validate(data)


class Service:
    """Scripted service independently records side effects and exposes state."""
    def __init__(self, clock):
        self.clock = clock
        self.config = {'product':'agentenv','source':{'kind':'template','id':'template'}}
        self.calls = []
        self.states = {}
        self.output = {'stdout': b'OK', 'stderr': b'', 'exit_code':0}
    async def create(self, run_id, timeout):
        sid = 'created-' + str(len(self.states))
        self.calls.append(('create', self.config['source']['id'], self.clock()))
        self.states[sid] = ('starting', self.clock()+2)
        return {'sandbox_id':sid,'private':{}}
    async def inspect(self, handle, timeout):
        self.calls.append(('inspect', handle['sandbox_id'], self.clock()))
        state, ready = self.states.get(handle['sandbox_id'], ('running', 0))
        if state == 'gone':
            raise TransportError('absent', status=404)
        return {'state':'running' if state == 'starting' and self.clock() >= ready else state}
    async def execute(self, handle, command, timeout):
        self.calls.append(('execute', handle['sandbox_id'], self.clock()))
        await self.clock.sleep(1)
        return self.output
    async def pause(self, handle, timeout, memory=True):
        self.calls.append(('pause', handle['sandbox_id'], self.clock()))
        self.states[handle['sandbox_id']] = ('paused', 0)
        return {}
    async def resume(self, handle, timeout, memory=True, ttl=None):
        self.calls.append(('resume', handle['sandbox_id'], self.clock()))
        self.states[handle['sandbox_id']] = ('starting', self.clock()+2)
        return handle
    async def delete(self, handle, timeout):
        self.calls.append(('delete', handle['sandbox_id'], self.clock()))
        self.states[handle['sandbox_id']] = ('gone', 0)
    async def fork(self, handle, count, timeout):
        self.calls.append(('fork', count, self.clock()))
        self.states['child'] = ('starting', self.clock()+2)
        return [{'sandbox':{'sandbox_id':'child','private':{}}}, {'error':{'code':'capacity'}}]
    async def build_image(self, run_id, timeout, poll_interval, on_progress):
        self.calls.append(('build', self.config['source']['id'], self.clock()))
        on_progress({'registration_id':'temporary','build_id':'job','triggered':True})
        await self.clock.sleep(3)
        return {'source_id':'persistent-template','kind':'template'}
    async def snapshot(self, handle, timeout):
        self.calls.append(('snapshot', handle['sandbox_id'], self.clock()))
        return {'source_id':'snapshot','kind':'snapshot','remote_status':'pending'}


class ScenarioTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, contract=None, **kwargs):
        clock = ManualClock()
        service = Service(clock)
        owned = []
        async def on_owned(row):
            owned.append(row)
        if contract is not None and contract.method_id == 'B07.FIRST' and 'first_command_reserver' not in kwargs:
            import tempfile
            from sandbox_bench.fixtures import FirstCommandLedger
            directory=tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            ledger=FirstCommandLedger(Path(directory.name)/'first.json')
            async def reserve(environment_ref,sandbox_id,generation):
                ledger.reserve(sandbox_id,generation)
            kwargs['first_command_reserver']=reserve
        executor = ScenarioExecutor(contract or case(), service, on_owned=on_owned,
                                    clock=clock, sleep=clock.sleep, process_epoch='test', **kwargs)
        return clock, service, owned, executor

    async def setup_target(self, executor, **fixture):
        await executor.setup(targets=[{'sandbox_id':'external','generation':'generation-1','private':{}}],
            fixture={'evidence_ref':'fixture','environment_ref':'env', **fixture})

    async def test_create_state_completion_never_executes_implicit_command(self):
        clock, service, owned, executor = self.engine()
        result = await clock.run(executor.execute(0))
        self.assertEqual(result.status, 'success')
        self.assertEqual(result.value['completion']['latency'], 2)
        self.assertFalse(any(c[0] == 'execute' for c in service.calls))
        self.assertEqual([row['resource_id'] for row in owned], ['created-0'])

    async def test_batch_validation_failure_preserves_state_latency(self):
        completion={'endpoint':'STATE_READY','timeout_s':10,'poll_interval_s':1,
                    'validation_policy':'batch_after','command':'echo OK','expected_stdout':'OK'}
        clock, service, owned, executor = self.engine(case(completion=completion))
        service.output={'stdout':b'BAD','stderr':b'', 'exit_code':1}
        result = await clock.run(executor.execute(0))
        self.assertEqual(clock(), 2)
        validations = await clock.run(executor.validate())
        self.assertFalse(validations[0]['success'])
        self.assertEqual(result.status, 'success')
        self.assertEqual(result.value['completion']['latency'], 2)

    async def test_pause_and_resume_use_readonly_state_targets_and_memory_validation(self):
        clock, service, _, executor = self.engine(case('B03.PAUSE'))
        await self.setup_target(executor)
        result = await clock.run(executor.execute(0))
        self.assertEqual(result.status, 'success')
        self.assertEqual([c[0] for c in service.calls], ['pause','inspect'])
        self.assertEqual(result.value['completion']['polls'][0]['state'], 'paused')
        checks=[]
        async def validate_fixture(phase, handle):
            checks.append((phase,handle['sandbox_id']))
            return False
        clock, service, _, executor = self.engine(case('B04.MEMORY'), fixture_validator=validate_fixture)
        await self.setup_target(executor, memory_challenge_ref='private-memory-nonce')
        result = await clock.run(executor.execute(0))
        self.assertEqual(result.status, 'success')
        self.assertFalse(result.value['validation']['success'])
        self.assertEqual(checks, [('memory_continuity','external')])

    async def test_missing_targets_or_fixture_evidence_blocks_before_side_effects(self):
        clock, service, _, executor = self.engine(case('B04.MEMORY'))
        result = await executor.execute(0)
        self.assertEqual(result.status, 'unsent')
        self.assertEqual(service.calls, [])
        with self.assertRaises(ScenarioBlocked):
            await executor.setup(targets=[{'sandbox_id':'external'}],fixture={})

    async def test_delete_waits_for_404_and_does_not_erase_external_fixture(self):
        clock, service, _, executor = self.engine(case('B06.RUNNING'))
        await self.setup_target(executor)
        result = await clock.run(executor.execute(0))
        self.assertEqual(result.status, 'success')
        self.assertEqual([c[0] for c in service.calls], ['delete','inspect'])
        cleanup = await clock.run(executor.cleanup())
        self.assertEqual(cleanup, [])

    async def test_first_command_requires_eligibility_and_full_exit_output_assertion(self):
        completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                    'command':'echo OK','expected_stdout':'OK'}
        clock, service, _, executor = self.engine(case('B07.FIRST',completion=completion))
        await self.setup_target(executor, first_command_eligible=True)
        result = await clock.run(executor.execute(0))
        self.assertEqual(result.status, 'success')
        self.assertEqual(result.value['completion']['latency'], 1)
        second = await executor.execute(1)
        self.assertEqual(second.status, 'unsent')
        self.assertEqual(len([c for c in service.calls if c[0]=='execute']), 1)

    async def test_native_fork_preserves_partial_failure_and_owns_only_child(self):
        clock, service, owned, executor = self.engine(case('B05.NATIVE', clone_count=2))
        await self.setup_target(executor)
        result = await clock.run(executor.execute(0))
        self.assertEqual(result.status, 'failed')
        self.assertEqual([r['status'] for r in result.value['children']], ['success','failed'])
        self.assertEqual([r['resource_id'] for r in owned], ['child'])
        self.assertEqual(len([c for c in service.calls if c[0]=='fork']), 1)

    async def test_full_image_includes_build_and_uses_persistent_source_without_mutating_adapter(self):
        progress=[]
        clock, service, owned, executor = self.engine(case('B02.FULL',
            source={'kind':'image','id':'image@sha256:fixture','digest':'sha256:fixture'}), on_progress=progress.append)
        result = await clock.run(executor.execute(0))
        self.assertEqual(result.status, 'success')
        self.assertEqual(result.value['completion']['latency'], 5)
        self.assertEqual([c[:2] for c in service.calls if c[0]=='create'], [('create','persistent-template')])
        self.assertEqual(service.config['source']['id'], 'template')
        self.assertEqual(len(progress), 1)

    async def test_snapshot_publication_requires_readiness_evidence_not_acknowledgment(self):
        clock, service, _, executor = self.engine(case('B08.PUBLISHED'))
        await self.setup_target(executor, touched_bytes=4096, touch_evidence_ref='touch')
        result=await executor.execute(0)
        self.assertEqual(result.status,'unsent')
        self.assertEqual(service.calls, [])

    async def test_stream_after_validation_exception_does_not_rewrite_ready_fact(self):
        completion={'endpoint':'STATE_READY','timeout_s':10,'poll_interval_s':1,
                    'validation_policy':'stream_after','command':'echo OK','expected_stdout':'OK'}
        clock, service, _, executor = self.engine(case(completion=completion))
        async def failed_command(*args):
            raise TransportError('data plane unavailable')
        service.execute=failed_command
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.status,'success')
        self.assertFalse(result.value['post_validation']['success'])
        self.assertEqual(result.value['completion']['latency'],2)

    async def test_delete_401_is_not_absence_and_never_retried(self):
        clock, service, _, executor = self.engine(case('B06.RUNNING'))
        await self.setup_target(executor)
        async def denied(*args):
            raise TransportError('denied',status=401)
        service.inspect=denied
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.status,'timeout')
        self.assertEqual(len([c for c in service.calls if c[0]=='delete']),1)

    async def test_published_snapshot_waits_for_remote_state_then_separate_restore_assertion(self):
        seen=[]
        async def validate_fixture(phase, source):
            seen.append(phase)
            return False
        clock, service, _, executor = self.engine(case('B08.PUBLISHED'),fixture_validator=validate_fixture)
        async def source_inspector(source):
            return {'state':'published' if clock()>=3 else 'uploading'}
        executor.source_inspector=source_inspector
        await self.setup_target(executor,touched_bytes=4096,touch_evidence_ref='touch')
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.status,'success')
        self.assertEqual(result.value['completion']['latency'],3)
        self.assertFalse(result.value['validation']['success'])
        self.assertEqual(seen,['snapshot_restore'])

    async def test_prepared_clone_persists_each_child_intent_before_create_and_never_replays(self):
        stages=[]
        async def stage_journal(event):
            stages.append(event.copy())
        async def verify(phase,handle):
            return True
        clock, service, owned, executor = self.engine(case('B05.PREPARED',clone_count=2,
            source={'kind':'snapshot','id':'prepared-snapshot','digest':'sha256:fixture'}),
            stage_journal=stage_journal,fixture_validator=verify)
        await self.setup_target(executor)
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.status,'success')
        self.assertEqual(len(result.value['children']),2)
        self.assertEqual([e['phase'] for e in stages],['intent','receipt','intent','receipt'])
        self.assertEqual([e['child_index'] for e in stages],[0,0,1,1])
        self.assertEqual([c[:2] for c in service.calls if c[0]=='create'],
                         [('create','prepared-snapshot'),('create','prepared-snapshot')])
        again=await clock.run(executor.execute(0))
        self.assertNotEqual(again.status,'success')
        self.assertEqual(len([c for c in service.calls if c[0]=='create']),2)

    async def test_full_clone_counts_capture_and_required_source_resume(self):
        stages=[]
        async def stage_journal(event):stages.append(event.copy())
        async def verify(phase,handle):return True
        clock, service, _, executor=self.engine(case('B05.FULL',clone_count=2,source_must_run=True),
                                                stage_journal=stage_journal,fixture_validator=verify)
        await self.setup_target(executor)
        async def capture(handle,timeout):
            service.calls.append(('snapshot',handle['sandbox_id'],clock()))
            await clock.sleep(2)
            service.states['external']=('paused',0)
            return {'source_id':'captured','kind':'snapshot'}
        service.snapshot=capture
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.status,'success')
        self.assertEqual(result.value['completion']['latency'],6)
        self.assertEqual([c[0] for c in service.calls if c[0] in {'snapshot','create','resume'}],
                         ['snapshot','create','create','resume'])
        self.assertTrue(all(c['validation']['success'] for c in result.value['children']))

    async def test_clone_stage_permission_failure_prevents_unjournaled_child(self):
        async def deny(event):raise RuntimeError('fenced')
        async def verify(phase,handle):return True
        clock, service, _, executor=self.engine(case('B05.PREPARED',clone_count=2,
            source={'kind':'snapshot','id':'prepared','digest':'sha256:fixture'}),
            stage_journal=deny,fixture_validator=verify)
        await self.setup_target(executor)
        result=await clock.run(executor.execute(0))
        self.assertNotEqual(result.status,'success')
        self.assertEqual(service.calls,[])

    async def test_density_member_creation_is_explicit_and_keeps_same_ownership_sink(self):
        from tests.benchmark.scenarios.test_scenario_density import density_case
        clock, service, owned, executor=self.engine(density_case('B10.GROWTH_LIST'))
        result=await clock.run(executor.create_member(0))
        self.assertEqual(result.status,'success')
        self.assertEqual(len(owned),1)
        self.assertEqual(executor.case.method_id,'B10.GROWTH_LIST')

    async def test_native_fork_parent_and_child_completion_exclude_post_validation(self):
        async def verify(phase,handle):
            await clock.sleep(3)
            return True
        clock, service, _, executor=self.engine(case('B05.NATIVE',clone_count=2),fixture_validator=verify)
        await self.setup_target(executor)
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.value['children'][0]['completion']['observed_at'],2)
        self.assertEqual(result.value['completion']['observed_at'],2)
        self.assertIsNone(result.value['completion']['latency'])  # one child failed
        self.assertEqual(clock(),5)
        self.assertTrue(result.value['validation_interference'])

    async def test_expanded_clone_child_latency_starts_at_child_submit_not_capture(self):
        async def stage_journal(event):pass
        async def verify(phase,handle):return True
        clock, service, _, executor=self.engine(case('B05.FULL',clone_count=2),
                                                stage_journal=stage_journal,fixture_validator=verify)
        await self.setup_target(executor)
        async def capture(handle,timeout):
            await clock.sleep(3)
            return {'source_id':'captured','kind':'snapshot'}
        service.snapshot=capture
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.value['completion']['latency'],5)
        self.assertEqual([c['completion']['latency'] for c in result.value['children']],[2,2])

    async def test_expanded_clone_lost_later_response_preserves_earlier_child(self):
        async def stage_journal(event):pass
        async def verify(phase,handle):return True
        clock, service, _, executor=self.engine(case('B05.PREPARED',clone_count=2,
            source={'kind':'snapshot','id':'prepared','digest':'sha256:fixture'}),
            stage_journal=stage_journal,fixture_validator=verify)
        await self.setup_target(executor)
        first_create=service.create
        calls=[]
        async def create(run_id,timeout):
            calls.append(1)
            if len(calls)==2:raise TransportError('response lost')
            return await first_create(run_id,timeout)
        service.create=create
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.status,'unknown')
        self.assertEqual(result.unresolved_external,1)
        self.assertEqual([c['status'] for c in result.value['children']],['success','unknown'])
        self.assertEqual(len(calls),2)

    async def test_two_concurrent_first_requests_cannot_consume_same_eligibility(self):
        import asyncio
        completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                    'command':'echo OK','expected_stdout':'OK'}
        clock, service, _, executor=self.engine(case('B07.FIRST',completion=completion))
        await self.setup_target(executor,first_command_eligible=True)
        async def both():return await asyncio.gather(executor.execute(0),executor.execute(1))
        results=await clock.run(both())
        self.assertEqual(sorted(r.status for r in results),['success','unsent'])
        self.assertEqual(len([c for c in service.calls if c[0]=='execute']),1)

    async def test_native_progress_emits_each_child_before_post_validation(self):
        events=[]
        async def verify(phase,handle):
            await clock.sleep(3)
            return True
        clock, service, _, executor=self.engine(case('B05.NATIVE',clone_count=2),fixture_validator=verify)
        await self.setup_target(executor)
        async def sink(child_index,result,completed_at):
            events.append((child_index,result.status,completed_at,clock()))
        result=await clock.run(executor.execute_with_progress(0,sink))
        self.assertEqual(events,[(1,'failed',0,0),(0,'success',2,2)])
        self.assertEqual(clock(),5)
        self.assertEqual(result.value['completion']['observed_at'],2)

    async def test_expanded_progress_preserves_partial_unknown_child_events(self):
        events=[]
        async def stage_journal(event):pass
        async def verify(phase,handle):return True
        clock, service, _, executor=self.engine(case('B05.PREPARED',clone_count=2,
            source={'kind':'snapshot','id':'prepared','digest':'sha256:fixture'}),
            stage_journal=stage_journal,fixture_validator=verify)
        await self.setup_target(executor)
        async def sink(child_index,result,completed_at):events.append((child_index,result.status,completed_at))
        result=await clock.run(executor.execute_with_progress(0,sink))
        self.assertEqual(events,[(0,'success',2),(1,'success',2)])
        self.assertEqual(result.status,'success')

    async def test_first_requires_durable_authority_and_generation_before_command(self):
        completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                    'command':'echo OK','expected_stdout':'OK'}
        clock,service,_,executor=self.engine(case('B07.FIRST',completion=completion),first_command_reserver=None)
        await self.setup_target(executor,first_command_eligible=True)
        result=await executor.execute(0)
        self.assertEqual(result.status,'unsent')
        self.assertEqual(service.calls,[])
        async def reserve(environment_ref,sandbox_id,generation):self.fail('missing generation cannot reserve')
        executor.first_command_reserver=reserve
        executor.targets[0].pop('generation')
        result=await executor.execute(0)
        self.assertEqual(result.status,'unsent')
        self.assertEqual(service.calls,[])

    async def test_first_claim_survives_new_executor_and_different_work_identity(self):
        import tempfile
        from sandbox_bench.fixtures import FirstCommandLedger
        completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                    'command':'echo OK','expected_stdout':'OK'}
        with tempfile.TemporaryDirectory() as directory:
            async def reserve(environment_ref,sandbox_id,generation):
                self.assertEqual(environment_ref,'env')
                FirstCommandLedger(Path(directory)/'first.json').reserve(sandbox_id,generation)
            clock,service,_,first=self.engine(case('B07.FIRST',completion=completion),first_command_reserver=reserve)
            await self.setup_target(first,first_command_eligible=True)
            result=await clock.run(first.execute(0))
            self.assertEqual(result.status,'success')
            clock2,service2,_,second=self.engine(case('B07.FIRST',completion=completion,work_id='another-work'),
                                                 first_command_reserver=reserve)
            await self.setup_target(second,first_command_eligible=True)
            again=await clock2.run(second.execute(0))
            self.assertEqual(again.status,'unsent')
            self.assertEqual(service2.calls,[])

    async def test_first_authority_latency_is_not_command_service_time(self):
        completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                    'command':'echo OK','expected_stdout':'OK'}
        async def reserve(environment_ref,sandbox_id,generation):
            await clock.sleep(2)
        clock,service,_,executor=self.engine(case('B07.FIRST',completion=completion),first_command_reserver=reserve)
        await self.setup_target(executor,first_command_eligible=True)
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.value['completion']['submitted_at'],2)
        self.assertEqual(result.value['completion']['latency'],1)

    async def test_clone_lost_child_response_still_restores_source_with_compensation_permission(self):
        stages=[]
        async def stage(event):stages.append(event.copy())
        async def verify(phase,handle):return True
        clock,service,_,executor=self.engine(case('B05.FULL',clone_count=2,source_must_run=True),
                                            stage_journal=stage,fixture_validator=verify)
        await self.setup_target(executor)
        async def capture(handle,timeout):
            service.states['external']=('paused',0)
            return {'source_id':'captured','kind':'snapshot'}
        service.snapshot=capture
        first_create=service.create
        calls=[]
        async def create(run_id,timeout):
            calls.append(1)
            if len(calls)==2:raise TransportError('response lost')
            return await first_create(run_id,timeout)
        service.create=create
        result=await clock.run(executor.execute(0))
        self.assertEqual([row['status'] for row in result.value['children']],['success','unknown'])
        self.assertEqual(result.value['source_recovery']['status'],'success')
        resume=[row for row in stages if row['stage']=='source_resume']
        self.assertEqual([row['phase'] for row in resume],['intent','receipt','observed'])
        self.assertTrue(all(row['compensation'] for row in resume))

    async def test_clone_failed_source_recovery_preserves_children_and_reports_residual(self):
        async def stage(event):pass
        async def verify(phase,handle):return True
        clock,service,_,executor=self.engine(case('B05.FULL',clone_count=2,source_must_run=True),
                                            stage_journal=stage,fixture_validator=verify)
        await self.setup_target(executor)
        async def capture(handle,timeout):
            service.states['external']=('paused',0)
            return {'source_id':'captured','kind':'snapshot'}
        async def resume(*args,**kwargs):raise TransportError('resume response lost')
        service.snapshot,service.resume=capture,resume
        result=await clock.run(executor.execute(0))
        self.assertEqual([row['status'] for row in result.value['children']],['success','success'])
        self.assertEqual(result.value['source_recovery']['status'],'unknown')
        self.assertEqual(result.value['source_residual']['sandbox_id'],'external')
        self.assertNotEqual(result.status,'success')

    async def test_clone_capture_timeout_uses_separate_bounded_recovery_window(self):
        stages=[]
        async def stage(event):stages.append(event.copy())
        async def verify(phase,handle):return True
        clock,service,_,executor=self.engine(case('B05.FULL',clone_count=2,source_must_run=True),
                                            stage_journal=stage,fixture_validator=verify)
        await self.setup_target(executor)
        async def capture(handle,timeout):
            service.states['external']=('paused',0)
            await clock.sleep(10)
            raise TimeoutError('capture response lost')
        async def resume(*args,**kwargs):await clock.sleep(100)
        service.snapshot,service.resume=capture,resume
        result=await clock.run(executor.execute(0))
        self.assertEqual(clock(),15)
        self.assertEqual(result.value['source_recovery']['status'],'timeout')
        self.assertTrue(any(row.get('compensation') for row in stages))
        self.assertFalse(any(call[0]=='create' for call in service.calls))

    async def test_cancelled_clone_restores_source_and_persists_recovery_observation(self):
        import asyncio
        stages=[]
        async def stage(event):stages.append(event.copy())
        async def verify(phase,handle):return True
        clock,service,_,executor=self.engine(case('B05.FULL',clone_count=2,source_must_run=True),
                                            stage_journal=stage,fixture_validator=verify)
        await self.setup_target(executor)
        async def capture(handle,timeout):
            service.states['external']=('paused',0)
            await clock.sleep(100)
        service.snapshot=capture
        async def run():
            task=asyncio.create_task(executor.execute(0))
            await clock.sleep(1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        await clock.run(run())
        observed=[row for row in stages if row['phase']=='observed' and row['stage']=='source_resume']
        self.assertEqual(observed[0]['completion']['status'],'success')
        self.assertEqual(observed[0]['sandbox_id'],'external')
        self.assertEqual(clock(),3)

    async def test_native_lost_fork_response_restores_source_and_reports_recovery(self):
        stages=[]
        async def stage(event):stages.append(event.copy())
        clock,service,_,executor=self.engine(case('B05.NATIVE',clone_count=2,source_must_run=True),
                                            stage_journal=stage)
        await self.setup_target(executor)
        async def fork(*args):
            service.states['external']=('paused',0)
            raise TransportError('fork response lost')
        service.fork=fork
        events=[]
        async def sink(index,result,at):events.append((index,result.status,clock()))
        result=await clock.run(executor.execute_with_progress(0,sink))
        self.assertEqual(result.status,'unknown')
        self.assertEqual(result.unresolved_external,2)
        self.assertEqual(result.value['source_recovery']['status'],'success')
        self.assertEqual(stages[-1]['phase'],'observed')
        self.assertEqual(events,[(0,'unknown',0),(1,'unknown',0)])

    async def test_native_cancelled_fork_still_persists_source_recovery(self):
        import asyncio
        stages=[]
        async def stage(event):stages.append(event.copy())
        clock,service,_,executor=self.engine(case('B05.NATIVE',clone_count=2,source_must_run=True),
                                            stage_journal=stage)
        await self.setup_target(executor)
        async def fork(*args):
            service.states['external']=('paused',0)
            await clock.sleep(100)
        service.fork=fork
        async def run():
            task=asyncio.create_task(executor.execute(0))
            await clock.sleep(1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        await clock.run(run())
        self.assertEqual(stages[-1]['completion']['status'],'success')
        self.assertTrue(stages[-1]['compensation'])
        self.assertEqual(clock(),3)

    async def test_command_uses_original_stream_hash_not_lossy_display_text(self):
        import hashlib
        completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                    'command':'printf raw','expected_stdout':'�'}
        clock,service,_,executor=self.engine(case('B07.OUTPUT',completion=completion))
        await self.setup_target(executor)
        service.output={'stdout':'�','stdout_bytes':1,'stdout_sha256':hashlib.sha256(bytes([255])).hexdigest(),
                        'stderr':'','exit_code':0}
        result=await clock.run(executor.execute(0))
        self.assertEqual(result.status,'failed')
        self.assertEqual(result.value['command']['stdout_bytes'],1)
        self.assertEqual(result.value['command']['stdout_sha256'],service.output['stdout_sha256'])

    async def test_snapshot_progress_precedes_independently_bounded_restore(self):
        events=[]
        async def verify(phase,handle):
            self.assertEqual(handle['source_sandbox_id'],'external')
            self.assertEqual(handle['request_index'],0)
            await clock.sleep(3)
            return True
        clock,service,_,executor=self.engine(case('B08.LOCAL',completion={
            'endpoint':'API_COMPLETE','timeout_s':2,'poll_interval_s':1}),
            fixture_validator=verify,fixture_validation_timeout_s=4)
        await self.setup_target(executor,touched_bytes=4096,touch_evidence_ref='touch')
        async def sink(index,result,at):events.append((index,result.status,at,clock()))
        result=await clock.run(executor.execute_with_progress(0,sink))
        self.assertEqual(events,[(0,'success',0,0)])
        self.assertEqual(result.value['completion']['latency'],0)
        self.assertTrue(result.value['validation']['success'])
        self.assertEqual(clock(),3)
