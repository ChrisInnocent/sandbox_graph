"""Audited runtime attribution: Cube shared shim/VMM, Kuasar CH, AgentENV MMDS.

Generation is the observed host/boot/PID incarnation, never an invented upstream
lifecycle version. Unknown/duplicate identities stay gaps. No mutation endpoint.
"""
from dataclasses import asdict,replace
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
from .inventory import collect_inventory,classify_processes,product_rules,InventoryResult,parse_proc_stat


def _flag(argv,name):
    values=[]
    for index,arg in enumerate(argv):
        if arg==name and index+1<len(argv):values.append(argv[index+1])
        elif arg.startswith(name+'='):values.append(arg[len(name)+1:])
    if len(values)!=1:raise ValueError('unique process option required')
    return values[0]


def _beneath(path,root):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts or path.resolve()!=path or not path.is_relative_to(root):
        raise ValueError('runtime path escaped declared root')
    return path


def read_mmds(path, *, timeout_s=1):
    """Only GET /mmds, bounded HTTP parser; never return full metadata publicly."""
    path=Path(path)
    if not stat.S_ISSOCK(path.stat().st_mode):raise ValueError('runtime socket required')
    if type(timeout_s) not in (float,int) or not 0<timeout_s<=1:raise ValueError('bounded MMDS deadline required')
    deadline=time.monotonic()+timeout_s
    client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);client.settimeout(timeout_s)
    class Reader(io.RawIOBase):
        total=0
        def readable(self):return True
        def readinto(self,buffer):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('MMDS total deadline')
            client.settimeout(remaining)
            size=client.recv_into(buffer);self.total+=size
            if self.total>1024*1024+65536:raise ValueError('MMDS wire byte budget')
            return size
    class BoundedSocket:
        def makefile(self,*args,**kwargs):return io.BufferedReader(Reader())
    try:
        client.connect(str(path));client.sendall(b'GET /mmds HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        response=http.client.HTTPResponse(BoundedSocket());response.begin()
        if response.status!=200:raise ValueError('MMDS identity unavailable')
        raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('MMDS byte budget exceeded')
        value=json.loads(raw)
        return {'instanceID':value['instanceID']}
    finally:client.close()


def discover(product,roles,*,proc_root,runtime_root,machine_id,boot_id,cohort_ids,read_mmds=read_mmds):
    root=Path(runtime_root);proc=Path(proc_root)
    if (not root.is_absolute() or root.resolve()!=root or not machine_id or not boot_id
            or len(cohort_ids)>10000 or len(set(cohort_ids))!=len(cohort_ids)
            or any(not isinstance(s,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',s) or s in {'.','..'} for s in cohort_ids)):
        raise ValueError('explicit canonical runtime and finite cohort required')
    rules=product_rules(product,roles)
    facts,limitations=collect_inventory(proc_root=proc,machine_id=machine_id,boot_id=boot_id)
    if len(facts)>100000:raise ValueError('process inventory budget exceeded')
    candidates={};gaps=list(limitations);start=time.monotonic()
    for fact in facts:
        exe=Path(fact.exe or '').name;sid=None;source=None
        try:
            if time.monotonic()-start>10:raise TimeoutError('discovery deadline')
            if product=='cubesandbox' and exe in {'cube-shim','containerd-shim-cube-rs'}:
                sid=_flag(fact.argv,'-id');bundle=_beneath(os.readlink(proc/str(fact.pid)/'cwd'),root)
                for name in ('shim.pid','vmm.pid'):
                    path=_beneath(bundle/name,root)
                    if path.stat().st_size>32 or int(path.read_text())!=fact.pid:raise ValueError('Cube pidfile identity differs')
                source='cube-shim-bundle-pidfiles.v1';component='shim_vmm'
            elif product=='kuasar' and exe=='cloud-hypervisor':
                path=_beneath(_flag(fact.argv,'--api-socket'),root)
                relative=path.relative_to(root).parts
                if len(relative)!=3 or relative[0]!='sandboxes' or relative[2]!='ch.sock':continue
                sid=relative[1];source='kuasar-nodepath-ch-socket.v1';component='vmm'
            elif product=='agentenv' and exe=='firecracker':
                path=_beneath(_flag(fact.argv,'--api-sock'),root)
                if path.name!='firecracker.socket' or Path(os.readlink(proc/str(fact.pid)/'cwd'))!=path.parent:continue
                sid=read_mmds(path)['instanceID'];source='agentenv-firecracker-mmds.v1';component='vmm'
            else:continue
            if sid not in cohort_ids:continue
            # Re-read identity after external/socket/file probes; no PID-reuse attribution.
            current=parse_proc_stat((proc/str(fact.pid)/'stat').read_text())
            if current['pid']!=fact.pid or current['start_ticks']!=fact.start_ticks:raise ValueError('runtime PID reused')
            row={'identity':fact.identity,'pid':fact.pid,'start_ticks':fact.start_ticks,'machine_id':machine_id,
                'boot_id':boot_id,'component':component,'role':'compute','exe':fact.exe,'cgroup':fact.cgroup,
                'cpu_time':{'ticks':fact.cpu_ticks,'ticks_per_second':os.sysconf('SC_CLK_TCK')},'pss_kib':fact.pss_kib,
                'sandbox_id':sid,'generation':hashlib.sha256((product+':'+sid+':'+fact.identity).encode()).hexdigest(),
                'generation_kind':'observed_process_incarnation','attribution_source':source,
                'observation':'complete' if fact.pss_kib is not None else 'partial_pss_missing'}
            candidates.setdefault(sid,[]).append(row)
        except (OSError,ValueError,KeyError,http.client.HTTPException) as exc:
            gaps.append('runtime_identity_unavailable:'+str(fact.pid)+':'+type(exc).__name__)
    ambiguous=[];owned=[]
    for sid,rows in candidates.items():
        if len(rows)!=1:ambiguous.append({'sandbox_id':sid,'pids':[r['pid'] for r in rows]})
        else:owned+=rows
    if product=='kuasar':
        by_pid={fact.pid:fact for fact in facts};ancestors=[]
        for row in owned:
            parent=by_pid[row['pid']].ppid;seen={row['pid']}
            for _ in range(4):
                fact=by_pid.get(parent)
                if not fact or parent in seen:break
                exe=Path(fact.exe or '').name
                if exe!='sandbox-ctl' and not (exe=='node-ctl' and fact.argv[1:2]==('run-sandbox',)):break
                try:current=parse_proc_stat((proc/str(parent)/'stat').read_text())
                except (OSError,ValueError):
                    gaps.append('runtime_parent_unavailable:'+str(parent));break
                if current['pid']!=parent or current['start_ticks']!=fact.start_ticks:
                    gaps.append('runtime_parent_changed:'+str(parent));break
                ancestors.append(row|{'identity':fact.identity,'pid':parent,'start_ticks':fact.start_ticks,
                    'component':'sandbox_runtime','exe':fact.exe,'cgroup':fact.cgroup,'pss_kib':fact.pss_kib,
                    'cpu_time':{'ticks':fact.cpu_ticks,'ticks_per_second':os.sysconf('SC_CLK_TCK')},
                    'attribution_source':'kuasar-runtime-parent-chain.v1'})
                seen.add(parent);parent=fact.ppid
        owned+=ancestors
        duplicates={r['pid'] for r in owned if sum(other['pid']==r['pid'] for other in owned)>1}
        if duplicates:
            ambiguous.append({'reason':'runtime_parent_shared_between_incarnations','pids':sorted(duplicates)})
            owned=[r for r in owned if r['pid'] not in duplicates]
    # Runtime binaries are never silently reclassified as shared management costs.
    runtime_names={'cloud-hypervisor','firecracker','cube-shim','containerd-shim-cube-rs','sandbox-ctl'}
    shared=classify_processes(facts,[r for r in rules if not set(r.executable_names)&runtime_names])
    missing=sorted(set(cohort_ids)-{r['sandbox_id'] for r in owned})
    inventory=InventoryResult(tuple(shared.processes)+tuple(owned),shared.ambiguous+tuple(ambiguous),
        tuple(sorted(set(shared.pss_missing_pids)|{r['pid'] for r in owned if r['pss_kib'] is None})),
        shared.shared_cgroups,shared.container_ids,tuple(sorted(set(gaps))))
    return {'inventory':inventory,'missing_cohort_ids':missing,
        'complete':not (missing or inventory.ambiguous or inventory.pss_missing_pids or inventory.limitations)}


def _refresh_snapshot(*,product,roles,runtime_root,proc_root,machine_id,boot_id,context,output):
    from .inventory import benchmark_snapshot
    from sandbox_bench.runner import atomic_json
    output=Path(output)
    previous=json.loads(output.read_text()) if output.exists() else None
    if previous:
        for key in ('environment_ref','node','inventory_ref','work_id'):
            if previous.get(key)!=context[key]:raise ValueError('snapshot scope changed')
        if previous.get('boot_id')!=boot_id:raise ValueError('snapshot boot changed')
    version=previous['snapshot_version']+1 if previous else 1
    result=discover(product,roles,proc_root=proc_root,runtime_root=runtime_root,machine_id=machine_id,
        boot_id=boot_id,cohort_ids=context['cohort_ids'])
    snapshot=benchmark_snapshot(result['inventory'],**context,boot_id=boot_id,version=version,
        observed_monotonic_s=time.monotonic())
    atomic_json(output,snapshot)
    return snapshot


def refresh_snapshot(**kwargs):
    import fcntl
    output=Path(kwargs['output']).absolute()
    if output.parent.resolve()!=output.parent or output.is_symlink():raise ValueError('canonical inventory output required')
    fd=os.open(str(output)+'.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _refresh_snapshot(**kwargs)
    finally:os.close(fd)
