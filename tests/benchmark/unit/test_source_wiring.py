import json
from pathlib import Path
import tempfile
import time
import unittest

from sandbox_bench.source_wiring import ConfiguredSourceLifecycle
from sandbox_bench.session import BenchSession
from tests.benchmark.scenarios.test_scenarios import case
from tests.benchmark.unit.test_wiring import config


class SourceWiringTests(unittest.IsolatedAsyncioTestCase):
    def setup_provider(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        session=BenchSession(case(),config(),root/'work')
        binding={'kind':'local_ledger_collector','ledger_path':str(root/'shared.json'),
            'collector_path':str(root/'collector.json'),'authority_ref':'authority',
            'max_bytes':65536,'max_age_s':10,'timeout_s':1,
            'delete_capabilities':[{'environment_ref':'env','product':'agentenv','product_version':'synthetic',
                'source_kind':'template','endpoint':'/templates/{source_id}',
                'evidence_ref':'audited-template-delete'}]}
        provider=ConfiguredSourceLifecycle(session,binding)
        session.source_lifecycle=provider
        self.root=root;self.session=session;self.binding=binding;self.provider=provider
        return provider

    def publish(self,sources,consumers=None):
        (self.root/'collector.json').write_text(json.dumps({'environment_ref':'env','product':'agentenv',
            'product_version':'synthetic','authority_ref':'authority','sources':sources,
            'consumers':consumers or {}}))

    def proof(self,sid,exists=True):
        return {'source_id':sid,'environment_ref':'env','kind':'template','complete':True,
            'exists':exists,'consumer_refs':[],'evidence_ref':'observation:'+str(time.time_ns()),
            'observed_wall_s':time.time()}

    async def test_unknown_consumer_survives_reopen_and_delete_ack_does_not_release(self):
        provider=self.setup_provider()
        await provider.own_source('built','template',0)
        reference=await provider.pin({'source_id':'built','kind':'template'},'request:0')
        self.publish({'built':self.proof('built')})
        reopened=ConfiguredSourceLifecycle(self.session,self.binding)
        result=await reopened.cleanup('built',1)
        self.assertEqual(result['status'],'retained')
        self.assertEqual(result['reason'],'consumer_reference_active')
        self.assertEqual(reopened.ledger.snapshot()['sources']['built']['consumers'][reference]['status'],'active')
        await reopened.bind({'source_id':'built','kind':'template'},'child',reference)
        result=await reopened.cleanup('built',1)
        self.assertEqual(result['status'],'retained')

    async def test_known_settled_absence_releases_then_independent_source_absence(self):
        provider=self.setup_provider()
        await provider.own_source('built','template',0)
        reference=await provider.pin({'source_id':'built','kind':'template'},'request:0')
        await provider.bind({'source_id':'built','kind':'template'},'child',reference)
        consumer={'source_id':'built','sandbox_id':'child','operation_settled':True,
            'sandbox_absent':True,'evidence_ref':'independent-child-absence','observed_wall_s':time.time()}
        self.publish({'built':self.proof('built')},{reference:consumer})
        calls=[]
        def delete(sid,timeout):
            calls.append(sid)
        original_document=provider._document
        def collector():
            if calls:self.publish({'built':self.proof('built',False)},{reference:consumer})
            return original_document()
        provider._document=collector
        self.session.adapter.delete_source=delete
        result=await provider.cleanup('built',1)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(calls,['built'])
        self.assertEqual((await provider.cleanup('built',1))['status'],'complete')
        self.assertEqual(calls,['built'])

    async def test_snapshot_or_missing_capability_is_retained_without_delete(self):
        provider=self.setup_provider()
        await provider.own_source('captured','snapshot',0)
        self.publish({'captured':{**self.proof('captured'),'kind':'snapshot'}})
        self.session.adapter.delete_source=lambda *_:self.fail('unproved delete endpoint used')
        result=await provider.cleanup('captured',1)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'source_delete_capability_unverified')

    async def test_lost_source_delete_response_is_not_reposted(self):
        provider=self.setup_provider()
        await provider.own_source('built','template',0)
        self.publish({'built':self.proof('built')})
        calls=[]
        def delete(sid,timeout):
            calls.append(sid)
            raise TimeoutError()
        self.session.adapter.delete_source=delete
        self.assertEqual((await provider.cleanup('built',1))['status'],'unknown')
        reopened=ConfiguredSourceLifecycle(self.session,self.binding)
        self.assertEqual((await reopened.cleanup('built',1))['status'],'unknown')
        self.assertEqual(calls,['built'])

    async def test_borrowed_source_retained_and_scope_mismatch_rejected(self):
        provider=self.setup_provider()
        source={'source_id':'template','kind':'template'}
        await provider.pin(source,'request:0')
        result=await provider.cleanup('template',1)
        self.assertEqual(result['status'],'retained')
        self.assertEqual(result['reason'],'borrowed_source')
        await provider.own_source('built','template',1)
        self.publish({'built':self.proof('built')})
        value=json.loads((self.root/'collector.json').read_text());value['product_version']='foreign'
        (self.root/'collector.json').write_text(json.dumps(value))
        self.assertEqual((await provider.cleanup('built',1))['status'],'blocked')

    async def test_late_shared_ledger_change_rejects_old_seal(self):
        provider=self.setup_provider()
        await provider.own_source('built','template',0)
        await self.session._N10()
        provider.ledger.pin('built','late-unknown-consumer')
        with self.assertRaisesRegex(ValueError,'seal inputs changed'):
            await self.session._N10()

    async def test_session_cleanup_orders_sandbox_before_source_without_ack_release(self):
        provider=self.setup_provider()
        await self.session._owned({'kind':'source','resource_id':'built','source_kind':'template','request_index':0})
        reference=await provider.pin({'source_id':'built','kind':'template'},'request:0')
        await provider.bind({'source_id':'built','kind':'template'},'child',reference)
        await self.session._owned({'kind':'sandbox','resource_id':'child','source_id':'built','consumer_ref':reference})
        self.publish({'built':self.proof('built')})
        calls=[]
        self.session.adapter.delete=lambda handle,timeout:calls.append(('sandbox',handle['sandbox_id']))
        self.session.adapter.delete_source=lambda sid,timeout:calls.append(('source',sid))
        result=await self.session._N09()
        self.assertEqual(calls,[('sandbox','child')])
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['cleanup'][1]['reason'],'consumer_reference_active')

    async def test_fixture_unknown_is_quarantined_without_main_operation(self):
        from sandbox_bench.fixture_lifecycle import RestoreVerifier
        from sandbox_bench.load import OperationResult
        self.setup_provider()
        async def owned(row):await self.session._owned(row)
        async def validator(*args):return True
        verifier=RestoreVerifier(case=self.session.case,adapter=self.session.adapter,
            folder=self.session.folder/'fixture_restore',max_requests=1,timeout_s=1,
            environment=self.session.environment,on_owned=owned,validator=validator,
            can_submit=lambda:True,process_epoch='test')
        self.session.fixture_restore=verifier
        token=await verifier.journal.reserve(0,1)
        verifier.journal.audit('restore_intent',operation_id=token,source_id='snap',request_index=0,source_kind='snapshot')
        await verifier.journal.submission_started(token)
        await verifier.journal.record_terminal(token,OperationResult('unknown',1))
        calls=[]
        def listing(work,timeout):
            calls.append(work)
            return [{'sandbox_id':sid,'private':{'metadata':{
                'run_id':work,'benchmark_fixture_role':'snapshot_restore',
                'parent_source_id':source,'benchmark_fixture_operation':token}}}
                for sid,source in [('found','snap'),('foreign-source','other')]]
        self.session.adapter.list_owned=listing
        result=await self.session.reconcile(discover=True)
        self.assertEqual(calls,['work'])
        self.assertEqual(result['status'],'unknown')
        self.assertEqual([r['resource_id'] for r in result['resources']],['found'])
        self.assertIn('fixture_restore_unknown:'+token,result['gaps'])
        self.assertIn('fixture_restore_unknown:'+token,self.session.records()['evidence_gaps'])
        self.session.adapter.delete=lambda *_:None
        self.assertEqual((await self.session._N09())['status'],'incomplete')
        self.assertEqual(verifier.journal.state()['operations'][token]['status'],'unknown')

    async def test_static_wiring_registers_hooks_and_recovers_owned_source(self):
        from sandbox_bench.wiring import configured_session
        self.setup_provider()
        bound={**config(),'wiring':{'version':'wiring.v1','environment_ref':'env',
            'source_cleanup':self.binding}}
        folder=self.root/'configured'
        first=configured_session(case(),bound,folder)
        await first._owned({'kind':'source','resource_id':'new','source_kind':'template','request_index':0})
        reference=await first.scenario.source_pin({'source_id':'new','kind':'template'},'request:0')
        await first.scenario.source_bind({'source_id':'new','kind':'template'},'child',reference)
        second=configured_session(case(),bound,folder)
        self.assertEqual(second.source_lifecycle.residuals()[0]['consumer_ref'],reference)
        self.assertEqual(second.source_lifecycle.ledger.snapshot()['sources']['new']['identity']['owner_work'],'work')

    async def test_borrowed_consumer_requires_proof_then_releases_without_source_delete(self):
        provider=self.setup_provider()
        source={'source_id':'template','kind':'template'}
        reference=await provider.pin(source,'request:0')
        await provider.bind(source,'child',reference)
        await self.session._owned({'kind':'sandbox','resource_id':'child'})
        self.session.adapter.delete=lambda *_:None
        self.session.adapter.delete_source=lambda *_:self.fail('borrowed source deleted')
        self.publish({'template':self.proof('template')})
        result=await self.session._N09()
        self.assertEqual(result['status'],'incomplete')
        self.assertIn('source_consumer_pending:'+reference,result['gaps'])
        self.publish({'template':self.proof('template')},{reference:{
            'source_id':'template','sandbox_id':'child','operation_settled':True,'sandbox_absent':True,
            'evidence_ref':'independent-absence','observed_wall_s':time.time()}})
        self.assertEqual((await self.session._N09())['status'],'completed')
        self.assertEqual(provider.residuals(),[])
        self.assertEqual(provider.ledger.snapshot()['sources']['template']['identity']['borrowed'],True)

    async def test_other_work_can_pin_owned_source_without_claiming_ownership(self):
        from sandbox_bench.wiring import configured_session
        provider=self.setup_provider()
        await provider.own_source('template','template',0)
        bound={**config(),'wiring':{'version':'wiring.v1','environment_ref':'env','source_cleanup':self.binding}}
        other=configured_session(case(work_id='consumer-work'),bound,self.root/'other')
        reference=await other.scenario.source_pin({'source_id':'template','kind':'template'},'request:0')
        self.assertEqual(provider.ledger.snapshot()['sources']['template']['identity']['owner_work'],'work')
        self.assertIn(reference,provider.ledger.snapshot()['sources']['template']['consumers'])
        self.publish({'template':self.proof('template')})
        self.assertEqual((await provider.cleanup('template',1))['reason'],'consumer_reference_active')

    async def test_initial_collector_read_has_total_cleanup_deadline(self):
        import asyncio
        provider=self.setup_provider()
        await provider.own_source('built','template',0)
        original=provider._document
        def slow():
            time.sleep(.08)
            return original()
        provider._document=slow
        began=time.monotonic()
        result=await provider.cleanup('built',.01)
        self.assertLess(time.monotonic()-began,.06)
        self.assertEqual(result['status'],'blocked')
        self.assertIsNone(provider.ledger.snapshot()['sources']['built']['cleanup'])
        await asyncio.sleep(.1)

    async def test_unknown_restore_cannot_release_pin_with_collector_absence(self):
        from sandbox_bench.fixture_lifecycle import RestoreVerifier
        from sandbox_bench.load import OperationResult
        provider=self.setup_provider()
        await provider.own_source('built','template',0)
        async def no_op(*args):return True
        verifier=RestoreVerifier(case=self.session.case,adapter=self.session.adapter,
            folder=self.session.folder/'fixture_restore',max_requests=1,timeout_s=1,
            environment=self.session.environment,on_owned=no_op,validator=no_op,
            can_submit=lambda:True,process_epoch='test')
        self.session.fixture_restore=verifier
        token=await verifier.journal.reserve(0,1)
        await verifier.journal.submission_started(token)
        await verifier.journal.record_terminal(token,OperationResult('unknown',1))
        source={'source_id':'built','kind':'template'}
        reference=await provider.pin(source,'restore:'+token)
        await provider.bind(source,'child',reference)
        self.publish({'built':self.proof('built')},{reference:{
            'source_id':'built','sandbox_id':'child','operation_settled':True,'sandbox_absent':True,
            'evidence_ref':'collector-absence','observed_wall_s':time.time()}})
        self.assertEqual((await provider.cleanup('built',1))['reason'],'consumer_reference_active')

    async def test_scenario_pins_before_lost_create_and_reentry_cannot_repost(self):
        from sandbox_bench.scenarios import ScenarioExecutor
        from tests.benchmark.scenarios.test_scenarios import ManualClock,Service
        provider=self.setup_provider();clock=ManualClock();adapter=Service(clock);calls=[]
        async def lost(*args):
            calls.append(1)
            self.assertEqual(len(provider.residuals()),1)
            raise OSError('lost response')
        adapter.create=lost
        scenario=ScenarioExecutor(case(),adapter,on_owned=self.session._owned,
            source_pin=provider.pin,source_bind=provider.bind,clock=clock,sleep=clock.sleep,process_epoch='test')
        result=await clock.run(scenario.execute(0))
        self.assertEqual(result.status,'unknown')
        reopened=ConfiguredSourceLifecycle(self.session,self.binding)
        scenario.source_pin=reopened.pin
        await clock.run(scenario.execute(0))
        self.assertEqual(calls,[1])
        self.assertEqual(len(reopened.residuals()),1)
