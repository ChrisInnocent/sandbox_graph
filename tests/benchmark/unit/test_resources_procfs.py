"""BTC-037/038/039/044: frozen, synthetic proc trees; no Linux claim."""
import tempfile
import unittest
from pathlib import Path
from sandbox_bench.resources import (parse_proc_stat, parse_pss, parse_cgroup_cpu,
    sample_process, deduplicate_inventory, validate_counter_scopes, guest_net_pss)


def stat(pid=42, start=300, user=125, system=75):
    # Field 3 through 22, independently positioned against proc(5).
    return f'{pid} (name (with) spaces)) S ' + ' '.join(map(str,
        [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, user, system, 0, 0, 0, 0, 1, 0, start]))


class ProcTests(unittest.TestCase):
    def test_btc037_parenthesized_name_and_units(self):
        self.assertEqual(parse_proc_stat(stat()),
                         {'pid': 42, 'ppid': 1, 'utime_ticks': 125,
                          'stime_ticks': 75, 'start_ticks': 300})
        self.assertEqual(parse_pss('Rss: 80 kB\nPss: 12 kB\nPss_Anon: 10 kB\n'), 12288)
        self.assertEqual(parse_cgroup_cpu('usage_usec 3000000\nuser_usec 2000000\nsystem_usec 1000000\n'),
                         {'usage_seconds': 3.0, 'user_seconds': 2.0, 'system_seconds': 1.0})

    def test_btc037_bad_required_fields_are_rejected(self):
        for text in ['Rss: 5 kB', 'Pss: 1 kB\nPss: 2 kB', 'Pss: -1 kB', 'Pss: 1 MB']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_pss(text)
        for text in ['usage_usec 1\nusage_usec 2', 'usage_usec -1', 'user_usec 5']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_cgroup_cpu(text)
        with self.assertRaises(ValueError):
            parse_proc_stat('42 (truncated) S 1')

    def tree(self, root):
        (root / 'sys/kernel/random').mkdir(parents=True)
        (root / 'sys/kernel/random/boot_id').write_text('boot-a\n')
        (root / '42').mkdir()
        (root / '42/stat').write_text(stat())
        (root / '42/smaps_rollup').write_text('Pss: 12 kB\n')

    def test_btc038_snapshot_checks_identity_and_non100_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.tree(root)
            row = sample_process(root, 42, environment='e', node='n', clock_ticks=250)
            self.assertEqual(row['status'], 'valid')
            self.assertEqual(row['cpu_seconds'], .8)
            self.assertEqual(row['pss_bytes'], 12288)
            self.assertEqual(row['identity']['start_ticks'], 300)
            (root / '42/smaps_rollup').unlink()
            missing = sample_process(root, 42, environment='e', node='n', clock_ticks=250)
            self.assertIsNone(missing['pss_bytes'])
            self.assertEqual(missing['status'], 'unavailable')

    def test_btc038_read_race_does_not_attribute_new_pid_to_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.tree(root)
            def reader(path):
                value = path.read_text()
                if path.name == 'smaps_rollup':
                    (root / '42/stat').write_text(stat(start=999))
                return value
            row = sample_process(root, 42, environment='e', node='n', clock_ticks=100, read_text=reader)
            self.assertEqual(row['reason'], 'identity_changed')
            self.assertIsNone(row['pss_bytes'])
            self.assertIsNone(row['cpu_seconds'])

    def test_btc039_inventory_deduplicates_roles_but_keeps_generations(self):
        identity = {'environment':'e', 'node':'n', 'boot_id':'b', 'pid':42, 'start_ticks':3}
        rows = [{'identity':identity, 'role':'vmm', 'component':'vm', 'sandbox_id':'s', 'generation':'g'},
                {'identity':identity, 'role':'shim', 'component':'runtime', 'sandbox_id':'s', 'generation':'g'}]
        unique = deduplicate_inventory(rows)
        self.assertEqual(len(unique), 1)
        self.assertEqual(set(unique[0]['roles']), {'vmm', 'shim'})
        conflicting = [rows[0], dict(rows[1], sandbox_id='another')]
        with self.assertRaises(ValueError):
            deduplicate_inventory(conflicting)
        with self.assertRaises(ValueError):
            validate_counter_scopes([{'kind':'cgroup','domain':'n','path':'/a'},
                                     {'kind':'cgroup','domain':'n','path':'/a/b'}])
        with self.assertRaises(ValueError):
            validate_counter_scopes([{'kind':'cgroup','domain':'n','path':'/a'},
                                     {'kind':'process','domain':'n','cgroup':'/a/b'}])
        self.assertEqual(len(validate_counter_scopes([{'kind':'cgroup','domain':'n','path':'/a'},
                                                    {'kind':'cgroup','domain':'n','path':'/ab'}])), 2)

    def test_btc044_guest_requires_same_domain_and_complete_proof(self):
        proof = {'definition':'measured guest PSS', 'domain':'n:b', 'window':[0,2],
                 'coverage':1.0, 'method':'mapped_pss', 'version':'v1',
                 'source':'raw/guest-1', 'deduction_bytes':20}
        self.assertEqual(guest_net_pss(100, proof, domain='n:b', window=[0,2])['net_pss_bytes'], 80)
        for bad, status in [(None,'unavailable'), (dict(proof, domain='other'),'unavailable'),
                            (dict(proof, deduction_bytes=101),'invalid'),
                            (dict(proof, definition=''),'unavailable'),
                            (dict(proof, coverage=.5),'unavailable')]:
            row = guest_net_pss(100, bad, domain='n:b', window=[0,2])
            self.assertEqual(row['status'], status)
            self.assertEqual(row['raw_pss_bytes'], 100)
            self.assertIsNone(row['net_pss_bytes'])

    def test_scope_validation_rejects_missing_identity_and_noncanonical_singletons(self):
        for scopes in [[{'kind':'process','domain':'n'}],
                       [{'kind':'cgroup','domain':'n','path':'/a/../b'}]]:
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                validate_counter_scopes(scopes)

    def test_thread_inventory_requires_leader_identity_for_pss_dedup(self):
        leader={'environment':'e','node':'n','boot_id':'b','pid':42,'start_ticks':7}
        thread=dict(leader,pid=43,start_ticks=8)
        rows=[{'identity':leader,'role':'vmm','tgid':42},
              {'identity':thread,'role':'thread','tgid':42,'thread_group_identity':leader}]
        self.assertEqual(len(deduplicate_inventory(rows)),1)
        with self.assertRaises(ValueError):
            deduplicate_inventory([{'identity':thread,'tgid':42}])

    @unittest.skipUnless(__import__('os').uname().sysname=='Linux', 'requires actual Linux kernel; frozen-tree coverage is separate')
    def test_btc038_real_linux_own_process_snapshot(self):
        import os
        row=sample_process('/proc',os.getpid(),environment='controlled-test',node='local')
        self.assertEqual(row['status'],'valid')
        self.assertGreater(row['pss_bytes'],0)
        self.assertGreaterEqual(row['cpu_seconds'],0)
        self.assertEqual(row['identity']['pid'],os.getpid())
