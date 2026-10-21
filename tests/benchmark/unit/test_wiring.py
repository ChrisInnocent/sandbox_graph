"""Configured adapters use explicit local evidence, never a synthetic default."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.benchmark.scenarios.test_scenarios import case
from sandbox_bench.wiring import configured_session, WiringConfig, ProbeClient


def config():
    return {'product':'agentenv','api_url':'http://localhost:12345','data_url':'http://localhost:12346',
            'source':{'kind':'template','id':'template'}}


class WiringTests(unittest.IsolatedAsyncioTestCase):
    def folder(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def observed_config(self,root):
        proc=root/'proc'
        (proc/'sys/kernel/random').mkdir(parents=True)
        (proc/'sys/kernel/random/boot_id').write_text('boot')
        (proc/'42').mkdir()
        (proc/'42/stat').write_text('42 (sample) S '+' '.join(map(str,[1,0,0,0,0,0,0,0,0,0,100,50,0,0,0,0,1,0,7])))
        (proc/'42/smaps_rollup').write_text('Pss: 20 kB\n')
        (proc/'meminfo').write_text('MemTotal: 2048 kB\nMemAvailable: 1024 kB\n')
        (proc/'stat').write_text('cpu 1 2 3 4 5 6 7 8\n')
        (proc/'loadavg').write_text('0 0 0 1/2 42\n')
        inventory={'environment_ref':'env','node':'node','boot_id':'boot','inventory_ref':'inventory',
            'evidence_ref':'operator-process-attribution',
            'processes':[{'identity':{'environment':'env','node':'node','boot_id':'boot','pid':42,'start_ticks':7},
                          'role':'control','component':'controller'}]}
        path=root/'inventory.json';path.write_text(json.dumps(inventory))
        wiring={'version':'wiring.v1','environment_ref':'env',
            'sampler':{'kind':'procfs','inventory_path':str(path),
                'inventory_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'proc_root':str(proc),'clock_ticks':100,'max_pending':8},
            'steady':{'metric':'pss_bytes','minimum':0,'maximum':50000,
                'slope_minimum':-100,'slope_maximum':100,'cohort_stability':False}}
        return {**config(),'wiring':wiring}

    def close_session(self,session):
        if session.sampler:session.sampler.close()
        if session._sampler_pool:session._sampler_pool.shutdown(wait=True)

    async def test_plain_create_needs_no_synthetic_providers(self):
        session=configured_session(case(),config(),self.folder())
        self.assertIsNone(session.sampler)
        self.assertIsNone(session.scenario.fixture_validator)

    def preflight_config(self,contract):
        return {**config(),'create_body':{'timeout':contract.ttl_s},
            'environment_evidence':{'ref':contract.environment_ref,'product_version':contract.product_version},
            'source_evidence':{'source_id':contract.source.id,'source_digest':contract.source.digest,
                'environment_ref':contract.environment_ref,'cache_profile':contract.cache_profile,
                'evidence_ref':'source-proof','effective_spec':contract.spec.model_dump()}}

    async def test_agentenv_execution_requires_exact_deployment_codec_evidence(self):
        completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                    'command':'echo OK','expected_stdout':'OK'}
        contract=case('B07.STEADY',completion=completion)
        proof={'environment_ref':'env','product_version':'synthetic',
               'codec':'connect-json','evidence_ref':'deployment-codec-probe'}
        for evidence in ({}, {**proof,'environment_ref':'other'},
                         {**proof,'product_version':'other'}, {**proof,'evidence_ref':''}):
            with self.subTest(evidence=evidence):
                session=configured_session(contract,{**self.preflight_config(contract),
                    'protocol_evidence':evidence},self.folder())
                result=await session._N01()
                self.assertEqual(result['status'],'blocked')
                self.assertEqual(result['reason'],'agentenv_versioned_codec_evidence_required')

    async def test_agentenv_protobuf_cannot_fall_back_to_json(self):
        contract=case(completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                                 'command':'echo OK','expected_stdout':'OK'})
        session=configured_session(contract,{**self.preflight_config(contract),
            'protocol_evidence':{'environment_ref':'env','product_version':'synthetic',
                'codec':'protobuf','evidence_ref':'deployment-codec-probe'}},self.folder())
        result=await session._N01()
        self.assertEqual(result,{'status':'blocked','reason':'agentenv_protobuf_execution_unsupported'})

    async def test_agentenv_json_proof_covers_execution_and_postvalidation(self):
        for endpoint,policy in (('EXEC_COMPLETE','none'),('STATE_READY','stream_after')):
            with self.subTest(endpoint=endpoint,policy=policy):
                contract=case(completion={'endpoint':endpoint,'timeout_s':10,'poll_interval_s':1,
                    'command':'echo OK','expected_stdout':'OK','validation_policy':policy})
                bound=self.preflight_config(contract)
                if policy!='none':
                    session=configured_session(contract,bound,self.folder())
                    self.assertEqual((await session._N01())['status'],'blocked')
                bound['protocol_evidence']={'environment_ref':'env','product_version':'synthetic',
                    'codec':'connect-json','evidence_ref':'deployment-codec-probe'}
                session=configured_session(contract,bound,self.folder())
                self.assertEqual((await session._N01())['status'],'completed')
        contract=case()
        session=configured_session(contract,self.preflight_config(contract),self.folder())
        self.assertEqual((await session._N01())['status'],'completed')

    async def test_unknown_provider_or_environment_mismatch_rejected(self):
        for wiring in ({'version':'wiring.v1','environment_ref':'env','plugin':'anything'},
                       {'version':'wiring.v1','environment_ref':'other'}):
            with self.assertRaises(ValueError):
                configured_session(case(),{**config(),'wiring':wiring},self.folder())

    async def test_first_requires_explicit_shared_authority_and_blocks_across_sessions(self):
        completion={'endpoint':'EXEC_COMPLETE','timeout_s':10,'poll_interval_s':1,
                    'command':'echo OK','expected_stdout':'OK'}
        contract=case('B07.FIRST',completion=completion)
        with self.assertRaises(ValueError):
            configured_session(contract,config(),self.folder())
        root=self.folder()
        bound={**config(),'wiring':{'version':'wiring.v1','environment_ref':'env',
            'first':{'kind':'local_file','path':str(root/'shared-first.json'),'authority_ref':'local-host-ledger'}}}
        first=configured_session(contract,bound,root/'work-one')
        second=configured_session(contract.model_copy(update={'work_id':'another'}),bound,root/'work-two')
        await first.scenario.first_command_reserver('env','sandbox','generation')
        with self.assertRaises(ValueError):
            await second.scenario.first_command_reserver('env','sandbox','generation')
        with self.assertRaises(ValueError):
            await first.scenario.first_command_reserver('wrong-environment','sandbox','generation-two')

    async def test_resources_require_hashed_inventory_and_explicit_steady_contract(self):
        resources={'interval_s':1,'window_s':2,'max_wait_s':5,'max_gap_s':2,
                   'min_samples':2,'inventory_ref':'inventory'}
        with self.assertRaises(ValueError):
            configured_session(case(resources=resources),config(),self.folder())
        with self.assertRaises(ValueError):
            WiringConfig.model_validate({'version':'wiring.v1','environment_ref':'env',
                'sampler':{'kind':'procfs','inventory_path':'/tmp/inventory','inventory_sha256':'bad'}})

    async def test_probe_client_validates_reply_and_uses_explicit_socket(self):
        import socket
        import threading
        root=self.folder();path=root/'guest.sock'
        server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        server.bind(str(path));server.listen(1)
        def serve():
            with server:
                with server.accept()[0] as client:
                    request=json.loads(client.recv(65536))
                    self.assertEqual(request,{'action':'inspect'})
                    client.sendall(b'{"status":"ok","result":{"nonce_sha256":"resident"}}\n')
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        client=ProbeClient(str(root),timeout_s=1,max_reply_bytes=65536)
        self.assertEqual((await client.request('guest',{'action':'inspect'}))['nonce_sha256'],'resident')
        thread.join(1)
        with self.assertRaises(ValueError):
            await client.request('../escape',{'action':'inspect'})

    async def test_hashed_inventory_collects_real_counters_from_explicit_proc_tree(self):
        from tests.benchmark.scenarios.test_scenario_density import density_case
        root=self.folder();bound=self.observed_config(root)
        bound['wiring']['cohort']={'kind':'owned_api','node':'node','node_field':'placement.node',
            'scope_ref':'owned-work','complete_listing_evidence_ref':'audited-list'}
        session=configured_session(density_case('B10.GROWTH_LIST'),bound,root/'session')
        self.addCleanup(self.close_session,session)
        row=session._capture_sample()
        self.assertEqual(row['processes'][0]['pss_bytes'],20480)
        self.assertEqual(row['processes'][0]['roles'],['control'])
        resource=await session.density_controller.resource_snapshot()
        self.assertEqual(resource['mem_available_bytes'],1048576)
        self.assertTrue(Path(resource['evidence_ref']).exists())
        self.assertEqual(resource['clock_domain'],session.clock_domain)

    async def test_owned_cohort_checks_exact_placement_and_ownership(self):
        from tests.benchmark.scenarios.test_scenario_density import density_case
        root=self.folder();bound=self.observed_config(root)
        bound['wiring']['cohort']={'kind':'owned_api','node':'node','node_field':'placement.node',
            'scope_ref':'owned-work','complete_listing_evidence_ref':'audited-list'}
        session=configured_session(density_case('B10.GROWTH_LIST'),bound,root/'session')
        self.addCleanup(self.close_session,session)
        session.adapter.list_owned=lambda work,timeout:[{'sandbox_id':'observed'}]
        session.adapter.info=lambda handle,timeout:{'placement':{'node':'node'},'metadata':{'run_id':'work'},'state':'running'}
        snapshot=await session.density_controller.cohort_inspector()
        self.assertEqual(snapshot.ready_ids,('observed',))
        session.adapter.info=lambda handle,timeout:{'placement':{'node':'elsewhere'},'metadata':{'run_id':'work'},'state':'running'}
        with self.assertRaises(ValueError):
            await session.density_controller.cohort_inspector()

    async def test_changed_inventory_digest_rejected_before_sampler_or_submission(self):
        from tests.benchmark.scenarios.test_scenario_density import density_case
        root=self.folder();bound=self.observed_config(root)
        bound['wiring']['cohort']={'kind':'owned_api','node':'node','node_field':'node',
            'scope_ref':'owned-work','complete_listing_evidence_ref':'audited-list'}
        Path(bound['wiring']['sampler']['inventory_path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'digest'):
            configured_session(density_case('B10.GROWTH_LIST'),bound,root/'session')
        self.assertFalse((root/'session/sampler.sqlite').exists())

    async def test_kuasar_terminal_reads_correlated_collector_and_accepted_start(self):
        root=self.folder()
        accepted=root/'accepted.json';logs=root/'terminal.jsonl'
        accepted.write_text(json.dumps({'environment_ref':'env','operations':[{
            'sid':'created','run_id':'work','kind':'create','profile':'e2b',
            'accepted_at':'2026-10-21T00:00:00Z','evidence_ref':'acceptance-capture'}]}))
        logs.write_text(json.dumps({'msg':'sandbox launch terminal','sid':'created','run_id':'work',
            'kind':'create','profile':'e2b','result':'success','starting_total_duration':125000000,
            'time':'2026-10-21T00:00:01Z'})+'\n')
        bound={**config(),'product':'kuasar','wiring':{'version':'wiring.v1','environment_ref':'env',
            'terminal':{'kind':'kuasar_jsonl','path':str(logs),'accepted_path':str(accepted),
                'collector_ref':'collector','profile':'e2b','launch_kind':'create','max_bytes':65536}}}
        contract=case(product='kuasar',completion={'endpoint':'SERVER_TERMINAL','timeout_s':10,'poll_interval_s':1})
        session=configured_session(contract,bound,root/'session')
        observed=await session.scenario.terminal_provider({'sandbox_id':'created'})
        self.assertTrue(observed.success)
        self.assertEqual(observed.internal_duration_ns,125000000)
        self.assertEqual(observed.process_epoch,session.process_epoch)

    async def test_live_inventory_snapshot_changes_process_identity_and_rejects_rollback(self):
        import time
        from tests.benchmark.scenarios.test_scenario_density import density_case
        from sandbox_bench.runner import digest
        root=self.folder();bound=self.observed_config(root)
        bound['wiring']['cohort']={'kind':'owned_api','node':'node','node_field':'node',
            'scope_ref':'owned-work','complete_listing_evidence_ref':'audited-list'}
        binding=bound['wiring']['sampler']
        snapshot_path=root/'live-inventory.json'
        binding.update(kind='collector_procfs',snapshot_path=str(snapshot_path))
        live=json.loads(Path(binding['inventory_path']).read_text())
        live.update(snapshot_version=1,complete=True,work_id='work',cohort_ids=[],
                    clock_domain=digest(['env','node','boot']),observed_monotonic_s=time.monotonic())
        snapshot_path.write_text(json.dumps(live))
        session=configured_session(density_case('B10.GROWTH_LIST'),bound,root/'session')
        self.addCleanup(self.close_session,session)
        first=session._capture_sample()
        self.assertEqual(first['snapshot_cohort_ids'],[])
        proc=Path(binding['proc_root'])
        (proc/'43').mkdir()
        (proc/'43/stat').write_text((proc/'42/stat').read_text().replace('42 (sample)','43 (new)'))
        (proc/'43/smaps_rollup').write_text('Pss: 30 kB\n')
        live['processes'].append({'identity':{'environment':'env','node':'node','boot_id':'boot','pid':43,'start_ticks':7},
            'role':'data','component':'vmm','sandbox_id':'new-sandbox','generation':'new-generation'})
        live.update(snapshot_version=2,cohort_ids=['new-sandbox'],observed_monotonic_s=time.monotonic())
        snapshot_path.write_text(json.dumps(live))
        second=session._capture_sample()
        self.assertEqual([p['identity']['pid'] for p in second['processes']],[42,43])
        self.assertEqual(second['snapshot_cohort_ids'],['new-sandbox'])
        self.assertNotEqual(first['inventory_version'],second['inventory_version'])
        live['snapshot_version']=1;snapshot_path.write_text(json.dumps(live))
        with self.assertRaises(ValueError):session._capture_sample()
        self.assertFalse(session.sampler.safe_to_submit)

    async def test_configured_clone_probe_checks_private_mutations_against_siblings(self):
        from unittest.mock import patch
        from sandbox_bench.guest_probe import GuestProbe
        root=self.folder()
        probes={sid:GuestProbe(root/sid,max_bytes=64) for sid in ('source','child','sibling')}
        for probe in probes.values():
            probe.handle({'action':'touch','bytes':64})
            probe.handle({'action':'write_file','name':'baseline','content':'shared content'})
        for sid in ('child','sibling'):probes[sid].nonce=probes['source'].nonce
        proof={'environment_ref':'env','fixture_ref':'fixture','evidence_ref':'guest-baseline',
               'sandboxes':{'source':probes['source'].inspect()}}
        before=root/'before.json';before.write_text(json.dumps(proof))
        bound={**config(),'wiring':{'version':'wiring.v1','environment_ref':'env',
            'probe':{'kind':'unix_socket','socket_directory':str(root),'before_path':str(before),
                     'before_sha256':hashlib.sha256(before.read_bytes()).hexdigest(),'timeout_s':1,'max_reply_bytes':65536}}}
        session=configured_session(case('B05.FULL',clone_count=2),bound,root/'session')
        async def request(client,sid,action):return probes[sid].handle(action)
        handle={'sandbox_id':'child','source_sandbox_id':'source','sibling_ids':['sibling']}
        with patch.object(ProbeClient,'request',request):
            self.assertTrue(await session.scenario.fixture_validator('clone_isolation',handle))
            for path in (root/'child').glob('clone-*'):path.unlink()
            probes['child'].pages=probes['source'].pages
            self.assertFalse(await session.scenario.fixture_validator('clone_isolation',handle))

    async def test_steady_window_excludes_preload_samples_and_keeps_new_interval(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from tests.benchmark.scenarios.test_scenarios import ManualClock
        root=self.folder();bound=self.observed_config(root)
        resources={'interval_s':1,'window_s':2,'max_wait_s':5,'max_gap_s':2,
                   'min_samples':2,'inventory_ref':'inventory'}
        session=configured_session(case(resources=resources),bound,root/'session')
        self.addCleanup(self.close_session,session)
        samples=root/'session/resource_samples';samples.mkdir()
        def record(at):
            (samples/f'{at}.json').write_text(json.dumps({'t_s':at,'clock_domain':session.clock_domain,
                'processes':[{'status':'valid','pss_bytes':100}]}))
        for at in (7,8,9):record(at)
        clock=ManualClock();clock.now=10
        with patch('sandbox_bench.wiring.time',SimpleNamespace(monotonic=clock)), \
             patch('sandbox_bench.wiring.asyncio',SimpleNamespace(sleep=clock.sleep)):
            result=await clock.run(session.steady_check(session.sampler,session.case.resources))
        self.assertEqual(result['status'],'blocked')
        clock=ManualClock();clock.now=20
        async def produce():
            for _ in range(3):
                await clock.sleep(1)
                record(clock())
        async def both():
            values=await asyncio.gather(session.steady_check(session.sampler,session.case.resources),produce())
            return values[0]
        with patch('sandbox_bench.wiring.time',SimpleNamespace(monotonic=clock)), \
             patch('sandbox_bench.wiring.asyncio',SimpleNamespace(sleep=clock.sleep)):
            result=await clock.run(both())
        self.assertEqual(result['status'],'completed')
        self.assertGreaterEqual(result['steady']['start_s'],20)
        self.assertEqual(result['steady']['not_before_s'],20)
        self.assertEqual(result['steady']['process_epoch'],session.process_epoch)
        self.assertEqual(result['steady']['scope'],'inventory:inventory')

    async def test_session_snapshot_uses_progress_and_adds_only_explicit_restore_drain_budget(self):
        from unittest.mock import patch
        from sandbox_bench.session import BenchSession
        from sandbox_bench.load import LoadReport
        class Scenario:
            async def setup(self,**kwargs):pass
            async def execute_with_progress(self,index,sink):raise AssertionError('fake engine must not submit')
            async def execute(self,index):raise AssertionError('fake engine must not submit')
        seen={}
        class Engine:
            async def run(self,schedule,operation,**kwargs):
                seen.update(schedule=schedule,**kwargs)
                return LoadReport(0,1)
        contract=case('B08.LOCAL',completion={'endpoint':'API_COMPLETE','timeout_s':10,'poll_interval_s':1})
        scenario=Scenario()
        session=BenchSession(contract,{**config(),'wiring':{'restore':{'timeout_s':3}}},self.folder(),scenario=scenario)
        with patch('sandbox_bench.session.LoadEngine',Engine):
            await session._N05()
        self.assertEqual(seen['progress_operation'],scenario.execute_with_progress)
        self.assertEqual(seen['schedule'].operation_timeout,13)
        self.assertEqual(contract.completion.timeout_s,10)

    async def test_configured_snapshot_restore_has_separate_journal_and_owned_cleanup_ids(self):
        import itertools
        from unittest.mock import patch
        root=self.folder();before=root/'before.json'
        baseline={'files':{'baseline':'file-sha'},'nonce_sha256':'resident','memory_sha256':'memory','allocated_bytes':4096}
        proof={'environment_ref':'env','fixture_ref':'fixture','evidence_ref':'guest-proof','sandboxes':{'external':baseline}}
        before.write_text(json.dumps(proof))
        bound={**config(),'targets':[{'sandbox_id':'external'}],
            'fixture_input':{'environment_ref':'env','evidence_ref':'fixture','touched_bytes':4096,'touch_evidence_ref':'touch'},
            'wiring':{'version':'wiring.v1','environment_ref':'env',
                'probe':{'kind':'unix_socket','socket_directory':str(root),'before_path':str(before),
                    'before_sha256':hashlib.sha256(before.read_bytes()).hexdigest(),'timeout_s':1,'max_reply_bytes':65536},
                'restore':{'kind':'budgeted_create','max_requests':4,'timeout_s':3,'validation_mode':'memory_and_disk'}}}
        contract=case('B08.LOCAL',completion={'endpoint':'API_COMPLETE','timeout_s':10,'poll_interval_s':1})
        session=configured_session(contract,bound,root/'session')
        snapshots=itertools.count();restores=itertools.count()
        session.adapter.snapshot=lambda handle,timeout:{'source_id':'captured-'+str(next(snapshots)),'kind':'snapshot'}
        session.adapter.create=lambda work,timeout:{'sandbox_id':'restored-'+str(next(restores))}
        session.adapter.inspect=lambda handle,timeout:{'state':'running'}
        async def request(client,sid,action):return baseline.copy()
        with patch.object(ProbeClient,'request',request):
            result=await session._N05()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(session.journal.state()['child_reserved'],4)
        fixture=session.fixture_restore.journal.state()
        self.assertEqual(fixture['child_reserved'],4)
        owned={r['resource_id'] for r in session.journal.owned(session.environment) if r['kind']=='sandbox'}
        self.assertEqual(owned,{'restored-0','restored-1','restored-2','restored-3'})
        report=json.loads((root/'session/load.json').read_text())
        self.assertEqual(len([event for event in report['events'] if event['kind']=='child_completed']),4)
        for index,parent in report['results'].items():
            verification=fixture['receipts']['restore:'+index]['result']
            self.assertLessEqual(parent['value']['completion']['observed_at'],verification['started_s'])
            self.assertTrue(parent['value']['validation']['success'])
        reopened=configured_session(contract,bound,root/'session')
        self.assertEqual({r['resource_id'] for r in reopened.journal.owned(reopened.environment) if r['kind']=='sandbox'},owned)
