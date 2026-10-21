"""Persistent spool tests use frozen synthetic proc trees, not host Linux."""
from pathlib import Path
import tempfile
import unittest
from sandbox_bench.sampler import ResourceSampler


class SamplerTests(unittest.TestCase):
    def fixture(self,root):
        proc=root/'proc'
        (proc/'sys/kernel/random').mkdir(parents=True)
        (proc/'sys/kernel/random/boot_id').write_text('b')
        (proc/'42').mkdir()
        (proc/'42/stat').write_text('42 (sample) S '+' '.join(map(str,[1,0,0,0,0,0,0,0,0,0,100,50,0,0,0,0,1,0,7])))
        (proc/'42/smaps_rollup').write_text('Pss: 20 kB\n')
        inv=[{'identity':{'environment':'e','node':'n','boot_id':'b','pid':42,'start_ticks':7},
              'role':'vmm','component':'vm','sandbox_id':'s','generation':'g'}]
        return proc,inv

    def test_samples_survive_reopen_and_reconcile_has_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            sampler=ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r',max_pending=2)
            first=sampler.sample_once()
            self.assertEqual(first['processes'][0]['pss_bytes'],20480)
            self.assertEqual(first['processes'][0]['cpu_seconds'],1.5)
            self.assertGreaterEqual(first['sampler_cpu_seconds'],0)
            sampler.close()
            sampler=ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r',max_pending=2)
            self.assertEqual(sampler.pending()[0]['sample_id'],first['sample_id'])
            received=[]
            def offline(row):
                raise ConnectionError('database unavailable')
            self.assertEqual(sampler.reconcile(offline)['status'],'disconnected')
            self.assertFalse(sampler.safe_to_submit)
            self.assertEqual(len(sampler.pending()),1)
            self.assertEqual(sampler.reconcile(received.append)['acknowledged'],1)
            self.assertTrue(sampler.safe_to_submit)
            self.assertEqual(received[0]['sample_id'],first['sample_id'])
            self.assertEqual(sampler.pending(),[])
            sampler.close()

    def test_bounded_spool_records_drops_and_inventory_identity_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            with ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r',max_pending=1) as sampler:
                (proc/'42/smaps_rollup').unlink()
                first=sampler.sample_once()
                self.assertEqual(first['processes'][0]['status'],'unavailable')
                self.assertIsNone(first['processes'][0]['pss_bytes'])
                sampler.sample_once();sampler.sample_once()
                self.assertEqual(len(sampler.pending()),2) # one sample + one compact gap range
                gap=sampler.pending()[-1]
                self.assertEqual(gap['kind'],'sample_gap')
                self.assertEqual(gap['dropped_count'],2)
                self.assertEqual((gap['seq'],gap['through_seq']),(2,3))
                received=[];sampler.reconcile(received.append)
                self.assertEqual(len(received),2)
                self.assertEqual(sampler.pending(),[])

    def test_changed_expected_boot_never_receives_frozen_process_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            inv[0]['identity']['boot_id']='wrong'
            with ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:wrong',run_id='r') as sampler:
                sample=sampler.sample_once()['processes'][0]
                self.assertEqual(sample['reason'],'inventory_identity_mismatch')
                self.assertIsNone(sample['pss_bytes'])

    def test_interrupted_read_is_recovered_as_a_durable_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            calls=0
            def interrupted_clock():
                nonlocal calls
                calls+=1
                if calls==2:
                    raise RuntimeError('simulated interrupted sample')
                return float(calls)
            with ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r',clock=interrupted_clock) as sampler:
                with self.assertRaises(RuntimeError):
                    sampler.sample_once()
            with ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r') as sampler:
                row=sampler.pending()[0]
                self.assertEqual(row['reason'],'interrupted_sampling')
                self.assertEqual(row['sample_id'],'r:1')
                self.assertEqual(sampler.sample_once()['seq'],2)

    def test_second_sampler_cannot_mutate_same_spool_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            with ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r'):
                with self.assertRaises(BlockingIOError):
                    ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r')

    def test_dedicated_worker_can_sample_reconcile_and_close_main_created_sampler(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            sampler=ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r')
            received=[]
            with ThreadPoolExecutor(max_workers=1) as worker:
                row=worker.submit(sampler.sample_once).result(timeout=3)
                self.assertEqual(row['processes'][0]['pss_bytes'],20480)
                self.assertEqual(worker.submit(sampler.reconcile,received.append).result(timeout=3)['acknowledged'],1)
                self.assertEqual(worker.submit(sampler.pending).result(timeout=3),[])
                worker.submit(sampler.close).result(timeout=3)
            self.assertFalse(sampler.safe_to_submit)

    def test_close_waits_for_inflight_sample_without_closing_its_database(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from unittest.mock import patch
        import sandbox_bench.sampler as module
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            sampler=ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r')
            entered=threading.Event();release=threading.Event();close_attempt=threading.Event()
            original=module.sample_process
            def slow_read(*args,**kwargs):
                entered.set()
                if not release.wait(3):raise TimeoutError('test synchronization')
                return original(*args,**kwargs)
            def close():
                close_attempt.set();sampler.close()
            with patch.object(module,'sample_process',slow_read),ThreadPoolExecutor(max_workers=2) as workers:
                sampled=workers.submit(sampler.sample_once)
                try:
                    self.assertTrue(entered.wait(2))
                    closed=workers.submit(close)
                    self.assertTrue(close_attempt.wait(2))
                    self.assertFalse(closed.done())
                finally:
                    release.set()
                self.assertEqual(sampled.result(timeout=3)['processes'][0]['status'],'valid')
                closed.result(timeout=3)
            self.assertFalse(sampler.safe_to_submit)

    def test_sampler_cpu_overhead_uses_worker_thread_not_whole_process(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            with ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r') as sampler:
                with patch('sandbox_bench.sampler.time.process_time',side_effect=[0,100]),patch('sandbox_bench.sampler.time.thread_time',side_effect=[10,10.25]):
                    self.assertEqual(sampler.sample_once()['sampler_cpu_seconds'],.25)

    def test_dynamic_inventory_adds_new_generation_and_survives_restart(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            (proc/'43').mkdir()
            (proc/'43/stat').write_text((proc/'42/stat').read_text().replace('42 (sample)','43 (sample)').rsplit(' ',1)[0]+' 9')
            (proc/'43/smaps_rollup').write_text('Pss: 30 kB\n')
            added=copy.deepcopy(inv[0]);added.update(sandbox_id='new',generation='g2',roles=['vmm'],components=['vm'])
            added.pop('role');added.pop('component');added['identity'].update(pid=43,start_ticks=9)
            args=dict(proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r')
            with ResourceSampler(inv,root/'spool.db',**args) as sampler:
                first=sampler.sample_once()
                version=sampler.update_inventory(inv+[added],'sha256:snapshot2',cohort_ids=['s','new'])
                row=sampler.sample_once()
                self.assertGreater(version,first['inventory_version'])
                self.assertEqual(row['inventory_source_ref'],'sha256:snapshot2')
                self.assertEqual(row['snapshot_cohort_ids'],['new','s'])
                self.assertEqual([p['pss_bytes'] for p in row['processes']],[20480,30720])
                self.assertEqual(sampler.update_inventory(inv+[added],'sha256:snapshot2',cohort_ids=['s','new']),version)
            with ResourceSampler(inv,root/'spool.db',**args) as sampler:
                self.assertEqual(sampler.sample_once()['inventory_version'],version)
                self.assertEqual(len(sampler.inventory),2)

    def test_invalid_inventory_gap_is_durable_and_reconcile_cannot_clear_guard(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            args=dict(proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r',max_pending=1)
            with ResourceSampler(inv,root/'spool.db',**args) as sampler:
                bad=copy.deepcopy(inv);bad[0]['identity']['boot_id']='foreign'
                with self.assertRaises(ValueError):sampler.update_inventory(bad,'bad',cohort_ids=['s'])
                sampler.invalidate_observation('collector_scope_mismatch')
                sampler.invalidate_observation('collector_scope_mismatch')
                sampler.invalidate_observation('collector_scope_mismatch')
                self.assertFalse(sampler.safe_to_submit)
                self.assertEqual(len(sampler.pending()),2)
                rows=[];sampler.reconcile(rows.append)
                self.assertEqual(sum(r['dropped_count'] for r in rows),3)
                self.assertFalse(sampler.safe_to_submit)
                sampler.update_inventory(inv,'new-proof',cohort_ids=['s'])
                self.assertTrue(sampler.safe_to_submit)
                self.assertEqual(sampler.sample_once()['seq'],4)

    def test_inventory_coverage_and_ownership_are_explicit(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc,inv=self.fixture(root)
            with ResourceSampler(inv,root/'spool.db',proc_root=proc,clock_ticks=100,clock_domain='n:b',run_id='r') as sampler:
                sampler.update_inventory(inv,'subset',cohort_ids=['s','missing'])
                self.assertFalse(sampler.sample_once()['inventory_complete'])
                for mutate in (lambda row:row.pop('generation'),lambda row:row['identity'].update(pid=True),
                               lambda row:row['identity'].update(start_ticks=-1)):
                    bad=copy.deepcopy(inv);mutate(bad[0])
                    with self.assertRaises(ValueError):sampler.update_inventory(bad,'bad')
