import json
from pathlib import Path
import pytest


def process(root,pid,exe,argv,*,ticks=100,cgroup='/slice/shared',cwd=None):
    folder=root/str(pid);folder.mkdir(exist_ok=True)
    fields=['S']+['0']*19;fields[19]=str(ticks)
    (folder/'stat').write_text(f'{pid} (process) '+' '.join(fields))
    (folder/'cmdline').write_bytes(b'\0'.join(str(a).encode() for a in argv)+b'\0')
    (folder/'cgroup').write_text('0::'+cgroup)
    (folder/'smaps_rollup').write_text('Pss: 100 kB\n')
    for name,target in [('exe',exe),('cwd',cwd)]:
        if target:
            path=folder/name;path.unlink(missing_ok=True);path.symlink_to(target)


@pytest.mark.parametrize('product',['cubesandbox','kuasar','agentenv'])
def test_product_runtime_discovery_preserves_actual_identity_without_double_count(tmp_path,product):
    from sandbox_graph.environment.probes.discovery import discover
    proc=tmp_path/'proc';proc.mkdir();root=tmp_path/'runtime';root.mkdir()
    if product=='cubesandbox':
        bundle=root/'namespace'/'s1';bundle.mkdir(parents=True)
        (bundle/'shim.pid').write_text('21');(bundle/'vmm.pid').write_text('21')
        process(proc,21,'/bin/containerd-shim-cube-rs',['shim','-namespace','cube','-id','s1'],cwd=bundle)
        roles=['compute']
    elif product=='kuasar':
        folder=root/'sandboxes/s1';folder.mkdir(parents=True)
        process(proc,21,'/bin/cloud-hypervisor',['ch','--api-socket',str(folder/'ch.sock')])
        roles=['runtime']
    else:
        folder=root/'random-workdir';folder.mkdir()
        process(proc,21,'/bin/firecracker',['fc','--api-sock',str(folder/'firecracker.socket')],cwd=folder)
        roles=['server']
    read=lambda path:{'instanceID':'s1','accessTokenHash':'MUST_NOT_EXPORT'}
    value=discover(product,roles,proc_root=proc,runtime_root=root,machine_id='m',boot_id='b',cohort_ids=['s1'],read_mmds=read)
    assert value['complete'] and len(value['inventory'].processes)==1
    row=value['inventory'].processes[0]
    assert row['sandbox_id']=='s1' and row['generation'] and row['identity']=='m:b:21:100'
    assert 'MUST_NOT_EXPORT' not in repr(value)
    # Refresh detects the next process incarnation; a reused ID cannot carry old evidence.
    process(proc,21,row['exe'],['fc','--api-sock',str(folder/'firecracker.socket')] if product=='agentenv' else
        ['ch','--api-socket',str(folder/'ch.sock')] if product=='kuasar' else ['shim','-id','s1'],ticks=200,
        cwd=bundle if product=='cubesandbox' else folder if product=='agentenv' else None)
    newer=discover(product,roles,proc_root=proc,runtime_root=root,machine_id='m',boot_id='b',cohort_ids=['s1'],read_mmds=read)
    assert newer['inventory'].processes[0]['generation']!=row['generation']


def test_missing_or_ambiguous_incarnation_never_becomes_complete_inventory(tmp_path):
    from sandbox_graph.environment.probes.discovery import discover
    root=tmp_path/'runtime';(root/'sandboxes/s1').mkdir(parents=True)
    proc=tmp_path/'proc';proc.mkdir()
    for pid in (21,22):process(proc,pid,'/bin/cloud-hypervisor',['ch','--api-socket',str(root/'sandboxes/s1/ch.sock')])
    result=discover('kuasar',['runtime'],proc_root=proc,runtime_root=root,machine_id='m',boot_id='b',cohort_ids=['s1','s2'])
    assert not result['complete'] and result['inventory'].ambiguous
    assert not result['inventory'].processes and result['missing_cohort_ids']==['s1','s2']


def test_foreign_process_and_cube_stale_pidfile_not_attributed(tmp_path):
    from sandbox_graph.environment.probes.discovery import discover
    root=tmp_path/'runtime';root.mkdir();proc=tmp_path/'proc';proc.mkdir()
    bundle=root/'s1';bundle.mkdir();(bundle/'shim.pid').write_text('999');(bundle/'vmm.pid').write_text('999')
    process(proc,21,'/bin/containerd-shim-cube-rs',['shim','-id','s1'],cwd=bundle)
    result=discover('cubesandbox',['compute'],proc_root=proc,runtime_root=root,machine_id='m',boot_id='b',cohort_ids=['s1'])
    assert not result['complete'] and not result['inventory'].processes


def test_discovery_cli_refreshes_version_and_enforces_scope(tmp_path,capsys):
    from scripts.environment.collect_inventory import main
    proc=tmp_path/'proc';(proc/'sys/kernel/random').mkdir(parents=True)
    (proc/'sys/kernel/random/boot_id').write_text('boot')
    machine=tmp_path/'machine';machine.write_text('m')
    runtime=tmp_path/'runtime';(runtime/'sandboxes/s1').mkdir(parents=True)
    process(proc,21,'/bin/cloud-hypervisor',['ch','--api-socket',str(runtime/'sandboxes/s1/ch.sock')])
    discovery=tmp_path/'discovery.json';discovery.write_text(json.dumps({'product':'kuasar','roles':['runtime'],'runtime_root':str(runtime)}))
    context=tmp_path/'context.json';context.write_text(json.dumps({'environment_ref':'env','node':'n','inventory_ref':'inv','work_id':'w','cohort_ids':['s1']}))
    output=tmp_path/'out.json'
    args=['--discovery',str(discovery),'--snapshot-context',str(context),'--output',str(output),'--proc-root',str(proc),'--machine-id-file',str(machine)]
    assert main(args)==0
    assert main(args)==0
    assert json.loads(output.read_text())['snapshot_version']==2
    bad=json.loads(context.read_text());bad['work_id']='other';context.write_text(json.dumps(bad))
    assert main(args)==2
    assert json.loads(output.read_text())['work_id']=='w'


def test_kuasar_attributes_runtime_parent_to_same_observed_incarnation(tmp_path):
    from sandbox_graph.environment.probes.discovery import discover
    proc=tmp_path/'proc';proc.mkdir();root=tmp_path/'runtime';(root/'sandboxes/s1').mkdir(parents=True)
    process(proc,20,'/bin/sandbox-ctl',['sandbox-ctl','run'])
    process(proc,21,'/bin/cloud-hypervisor',['ch','--api-socket',str(root/'sandboxes/s1/ch.sock')])
    stat_file=proc/'21/stat';parts=stat_file.read_text().split();parts[3]='20';stat_file.write_text(' '.join(parts))
    result=discover('kuasar',['runtime'],proc_root=proc,runtime_root=root,machine_id='m',boot_id='b',cohort_ids=['s1'])
    rows=result['inventory'].processes
    assert {r['pid'] for r in rows}=={20,21}
    assert len({r['generation'] for r in rows})==1
    assert result['complete']


def test_mmds_real_socket_returns_only_identity_and_enforces_total_deadline(tmp_path):
    import socket,threading,time
    from sandbox_graph.environment.probes.discovery import read_mmds
    path=tmp_path/'mmds.sock';server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(path));server.listen()
    body=b'{"instanceID":"s1","secret":"never-export"}'
    def serve():
        peer,_=server.accept()
        with peer:
            assert peer.recv(4096).startswith(b'GET /mmds ')
            peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
    thread=threading.Thread(target=serve);thread.start()
    try:assert read_mmds(path)=={'instanceID':'s1'}
    finally:thread.join(2);server.close()


def test_snapshot_keeps_attribution_provenance(tmp_path):
    from sandbox_graph.environment.probes.discovery import refresh_snapshot
    root=tmp_path/'runtime';(root/'sandboxes/s1').mkdir(parents=True);proc=tmp_path/'proc';proc.mkdir()
    process(proc,21,'/bin/cloud-hypervisor',['ch','--api-socket',str(root/'sandboxes/s1/ch.sock')])
    snapshot=refresh_snapshot(product='kuasar',roles=['runtime'],runtime_root=root,proc_root=proc,
        machine_id='m',boot_id='b',context={'environment_ref':'env','node':'n','inventory_ref':'inv','work_id':'w','cohort_ids':['s1']},output=tmp_path/'inventory.json')
    assert snapshot['processes'][0]['generation_kind']=='observed_process_incarnation'
    assert snapshot['processes'][0]['attribution_source']=='kuasar-nodepath-ch-socket.v1'


def test_mmds_drip_feed_cannot_extend_total_deadline(tmp_path):
    import socket,threading,time
    from sandbox_graph.environment.probes.discovery import read_mmds
    path=tmp_path/'slow.sock';server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(path));server.listen()
    def serve():
        peer,_=server.accept()
        with peer:
            peer.recv(4096)
            try:
                for byte in b'HTTP/1.1 200 OK\r\n':peer.sendall(bytes([byte]));time.sleep(.02)
            except OSError:pass
    thread=threading.Thread(target=serve);thread.start();started=time.monotonic()
    try:
        with pytest.raises(TimeoutError):read_mmds(path,timeout_s=.08)
        assert time.monotonic()-started<.5
    finally:thread.join(2);server.close()
