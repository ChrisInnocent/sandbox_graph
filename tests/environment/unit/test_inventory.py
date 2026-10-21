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
