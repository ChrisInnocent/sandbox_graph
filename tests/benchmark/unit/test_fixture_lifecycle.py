import tempfile
from pathlib import Path
import unittest

from tests.benchmark.scenarios.test_scenarios import case, ManualClock, Service
from sandbox_bench.fixture_lifecycle import RestoreVerifier, RestoreBlocked


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    def build(self,*,budget=1,timeout=5,validator=None):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        clock=ManualClock();adapter=Service(clock);owned=[]
        async def on_owned(row):owned.append(row)
        async def valid(handle,source):return True
        options={'case':case('B08.LOCAL',completion={'endpoint':'API_COMPLETE','timeout_s':10,'poll_interval_s':1}),
            'adapter':adapter,'folder':Path(tmp.name)/'restore','max_requests':budget,'timeout_s':timeout,
            'environment':'bound-env','on_owned':on_owned,'validator':validator or valid,
            'can_submit':lambda:True,'clock':clock,'sleep':clock.sleep,'process_epoch':'test'}
        return clock,adapter,owned,options

    def source(self,index=0):
        return {'source_id':'captured-'+str(index),'kind':'snapshot','request_index':index,'source_sandbox_id':'original'}

    async def test_restore_uses_independent_budget_and_caches_completed_validation(self):
        clock,adapter,owned,options=self.build()
        verifier=RestoreVerifier(**options)
        self.assertTrue(await clock.run(verifier.verify(self.source())))
        self.assertEqual(clock(),2)
        self.assertEqual(verifier.journal.state()['child_reserved'],1)
        self.assertEqual(owned[0]['resource_id'],'created-0')
        self.assertEqual(owned[0]['source_id'],'captured-0')
        self.assertTrue(await clock.run(RestoreVerifier(**options).verify(self.source())))
        self.assertEqual(len([row for row in adapter.calls if row[0]=='create']),1)
        with self.assertRaises(RestoreBlocked):
            await clock.run(verifier.verify(self.source(1)))
        self.assertEqual(len([row for row in adapter.calls if row[0]=='create']),1)

    async def test_unknown_create_is_never_reposted_after_new_verifier(self):
        clock,adapter,owned,options=self.build()
        calls=[]
        async def create(*args):
            calls.append(1)
            raise OSError('lost response')
        adapter.create=create
        verifier=RestoreVerifier(**options)
        self.assertFalse(await clock.run(verifier.verify(self.source())))
        with self.assertRaises(RestoreBlocked):
            await clock.run(RestoreVerifier(**options).verify(self.source()))
        self.assertEqual(calls,[1])
        self.assertEqual(owned,[])
        self.assertEqual(next(iter(verifier.journal.state()['operations'].values()))['status'],'unknown')

    async def test_validation_failure_keeps_owned_restore_and_performance_outside_journal(self):
        async def invalid(handle,source):return False
        clock,adapter,owned,options=self.build(validator=invalid)
        verifier=RestoreVerifier(**options)
        self.assertFalse(await clock.run(verifier.verify(self.source())))
        self.assertEqual(owned[0]['resource_id'],'created-0')
        receipt=verifier.journal.state()['receipts']['restore:0']['result']
        self.assertEqual(receipt['status'],'invalid')
        self.assertEqual(receipt['completion']['status'],'success')

    async def test_denied_submission_does_not_create_or_consume_fixture_budget(self):
        clock,adapter,owned,options=self.build()
        options['can_submit']=lambda:False
        verifier=RestoreVerifier(**options)
        with self.assertRaises(RestoreBlocked):await verifier.verify(self.source())
        self.assertEqual(verifier.journal.state()['child_reserved'],0)
        self.assertEqual(adapter.calls,[])
