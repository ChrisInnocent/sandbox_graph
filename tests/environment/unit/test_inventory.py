from sandbox_graph.environment.probes.inventory import parse_proc_stat, classify_processes, ProcessRule, ProcessFact


def test_proc_stat_parses_pid_reuse_identity_with_parenthesized_command():
    fields=['S']+['0']*19+['12345']
    # fields after comm begin at state (field 3); starttime is field 22, index 19 here.
    fields[19]='98765'
    raw='123 (worker process (child)) '+' '.join(fields)
    parsed=parse_proc_stat(raw)
    assert parsed['pid']==123 and parsed['start_ticks']==98765


def test_process_matching_detects_duplicate_ownership_and_missing_pss():
    rows=(ProcessFact(pid=5,start_ticks=88,comm='cubelet',exe='/usr/bin/cubelet',
        cgroup='/system.slice/docker-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.scope',cpu_ticks=20,pss_kib=400),
        ProcessFact(pid=6,start_ticks=91,comm='cubelet',exe='/usr/bin/cubelet',
        cgroup='/system.slice/docker-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.scope',cpu_ticks=5,pss_kib=None))
    rules=(ProcessRule(component='cubelet',role='compute',executable_names=('cubelet',)),
           ProcessRule(component='service',role='compute',executable_names=('cubelet',)))
    result=classify_processes(rows,rules)
    assert len(result.ambiguous)==2
    assert result.pss_missing_pids==(6,)
    assert len(result.shared_cgroups)==1
    assert result.container_ids==('0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',)
    assert result.processes==()  # ambiguous ownership is never double-counted


def test_inventory_discards_pid_reused_between_pss_and_final_identity(tmp_path,monkeypatch):
    from pathlib import Path
    import sandbox_graph.environment.probes.inventory as module
    folder=tmp_path/'42';folder.mkdir()
    fields=['S']+['0']*19;fields[19]='100'
    stat_file=folder/'stat';stat_file.write_text('42 (worker) '+' '.join(fields))
    (folder/'cgroup').write_text('0::/worker')
    (folder/'exe').symlink_to('/usr/bin/worker')
    def recycled(path):
        fields[19]='200';stat_file.write_text('42 (worker) '+' '.join(fields));return 80
    monkeypatch.setattr(module,'_pss_kib',recycled)
    facts,limitations=module.collect_inventory(proc_root=tmp_path)
    assert facts==() and 'pid_reused:42' in limitations


def test_same_binary_roles_require_exact_argv_and_host_boot_identity():
    fact=ProcessFact(pid=41,start_ticks=9,comm='node-ctl',exe='/usr/bin/node-ctl',
        cgroup='0::/system.slice/node-proxy.service',cpu_ticks=30,pss_kib=100,
        argv=('node-ctl','proxy','--config','/etc/node-ctl/proxy.yaml'),machine_id='machine',boot_id='boot')
    rules=(ProcessRule('conductor','compute',('node-ctl',),argv_prefix=('conductor',)),
           ProcessRule('proxy','management',('node-ctl',),argv_prefix=('proxy',)))
    result=classify_processes([fact],rules)
    assert len(result.processes)==1 and not result.ambiguous
    row=result.processes[0]
    assert row['component']=='proxy' and row['identity']=='machine:boot:41:9'
    assert row['machine_id']=='machine' and row['boot_id']=='boot'
    assert 'argv' not in row


def test_product_inventory_rules_separate_native_kuasar_roles():
    from sandbox_graph.environment.probes.inventory import product_rules
    rules=product_rules('kuasar',('conductor','proxy'))
    assert {(r.component,r.argv_prefix) for r in rules}=={('conductor',('conductor',)),('proxy',('proxy',))}


def test_owned_cgroup_boundary_excludes_same_executable_and_preserves_generation():
    rules=[ProcessRule('vmm','compute',('firecracker',),cgroup_path='/sandbox/owned',sandbox_id='s1',generation='g1')]
    facts=[ProcessFact(pid=i,start_ticks=i,comm='firecracker',exe='/bin/firecracker',cpu_ticks=1,pss_kib=10,
        cgroup='0::'+path,machine_id='machine',boot_id='boot') for i,path in enumerate(
        ('/sandbox/owned','/sandbox/owned/child','/sandbox/owned-other','/unrelated'),start=1)]
    result=classify_processes(facts,rules)
    assert [r['pid'] for r in result.processes]==[1,2]
    assert all(r['sandbox_id']=='s1' and r['generation']=='g1' for r in result.processes)


def test_benchmark_snapshot_keeps_missing_cohort_as_gap_not_fabricated_process():
    from sandbox_graph.environment.probes.inventory import benchmark_snapshot
    fact=ProcessFact(pid=1,start_ticks=2,comm='firecracker',exe='/bin/firecracker',cpu_ticks=1,pss_kib=10,
        cgroup='0::/sandbox/owned',machine_id='machine',boot_id='boot')
    result=classify_processes([fact],[ProcessRule('vmm','compute',('firecracker',),cgroup_path='/sandbox/owned',sandbox_id='s1',generation='g1')])
    value=benchmark_snapshot(result,environment_ref='env',node='n1',boot_id='boot',inventory_ref='inv',
        work_id='w1',cohort_ids=['s1','s2'],version=1,observed_monotonic_s=12.)
    assert value['complete'] is False and value['missing_cohort_ids']==['s2']
    assert value['processes'][0]['identity']=={'environment':'env','node':'n1','boot_id':'boot','pid':1,'start_ticks':2}
