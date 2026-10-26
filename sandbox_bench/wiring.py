"""Static, explicit local provider wiring; never imports configured Python code.

Configuration binds evidence identities and physical observation scope. Paths refer
only to deliberately supplied inventories, collectors and guest probe forwards.
These bindings establish neither a remote deployment nor physical cleanup proof.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import re
import socket
import time
from typing import Literal

from pydantic import Field, model_validator
from .contracts import StrictModel, Positive, Count
from .completion import TerminalObservation
from .fixtures import FirstCommandLedger, verify_continuity
from .logs import AcceptedAtEvidence, LogIdentity, TerminalLogParser
from .runner import atomic_json, digest
from .sampler import ResourceSampler
from .scenario_density import CohortSnapshot, DensityController
from .steady import SteadyConfig, find_steady_window


class DiscoveryBinding(StrictModel):
    product: Literal['cubesandbox','kuasar','agentenv']
    roles: list[str] = Field(min_length=1)
    runtime_root: str
    machine_id_file: str

    @model_validator(mode='after')
    def registered(self):
        from sandbox_graph.environment.probes.inventory import product_rules
        product_rules(self.product,self.roles)
        for value in (self.runtime_root,self.machine_id_file):
            if not Path(value).is_absolute() or Path(value).resolve()!=Path(value):
                raise ValueError('canonical local discovery paths required')
        return self


class ProcfsBinding(StrictModel):
    kind: Literal['procfs','collector_procfs']
    inventory_path: str
    inventory_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    proc_root: str
    clock_ticks: Count
    max_pending: Count
    snapshot_path: str | None = None
    discovery: DiscoveryBinding | None = None

    @model_validator(mode='after')
    def dynamic_path(self):
        if (self.kind=='collector_procfs') != (self.snapshot_path is not None):
            raise ValueError('collector procfs requires exactly one explicit snapshot path')
        if self.discovery is not None and self.kind!='collector_procfs':raise ValueError('discovery requires collector_procfs')
        return self


class SteadyBinding(StrictModel):
    metric: Literal['pss_bytes']
    minimum: float
    maximum: float
    slope_minimum: float
    slope_maximum: float
    cohort_stability: bool


class CohortBinding(StrictModel):
    kind: Literal['owned_api']
    node: str = Field(min_length=1)
    node_field: str = Field(pattern=r'^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$')
    scope_ref: str = Field(min_length=1)
    complete_listing_evidence_ref: str = Field(min_length=1)


class FirstBinding(StrictModel):
    kind: Literal['local_file']
    path: str
    authority_ref: str = Field(min_length=1)


class ProbeBinding(StrictModel):
    kind: Literal['unix_socket']
    socket_directory: str
    before_path: str
    before_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    timeout_s: Positive
    max_reply_bytes: Count


class TerminalBinding(StrictModel):
    kind: Literal['kuasar_jsonl']
    path: str
    accepted_path: str
    collector_ref: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    launch_kind: Literal['create','resume']
    max_bytes: Count


class SourceBinding(StrictModel):
    kind: Literal['collector_json']
    path: str
    collector_ref: str = Field(min_length=1)
    max_bytes: Count


class RestoreBinding(StrictModel):
    kind: Literal['budgeted_create']
    max_requests: Count
    timeout_s: Positive
    validation_mode: Literal['disk','memory_and_disk']

class SourceDeleteCapability(StrictModel):
    environment_ref: str = Field(min_length=1)
    product: Literal['agentenv','cubesandbox','kuasar']
    product_version: str = Field(min_length=1)
    source_kind: Literal['template']
    endpoint: Literal['/templates/{source_id}']
    evidence_ref: str = Field(min_length=1)


class SourceCleanupBinding(StrictModel):
    kind: Literal['local_ledger_collector']
    ledger_path: str
    collector_path: str
    authority_ref: str = Field(min_length=1)
    max_bytes: Count
    max_age_s: Positive
    timeout_s: Positive
    delete_capabilities: list[SourceDeleteCapability]


class WiringConfig(StrictModel):
    version: Literal['wiring.v1']
    environment_ref: str = Field(min_length=1)
    sampler: ProcfsBinding | None = None
    steady: SteadyBinding | None = None
    cohort: CohortBinding | None = None
    first: FirstBinding | None = None
    probe: ProbeBinding | None = None
    terminal: TerminalBinding | None = None
    source: SourceBinding | None = None
    slo: SourceBinding | None = None
    restore: RestoreBinding | None = None
    source_cleanup: SourceCleanupBinding | None = None
    sched_switch: bool = False

    @model_validator(mode='after')
    def absolute_paths(self):
        for binding in (self.sampler,self.first,self.probe,self.terminal,self.source,self.slo,self.source_cleanup):
            if binding is None:continue
            for key,value in binding.model_dump().items():
                if value is not None and key in {'path','accepted_path','inventory_path','before_path','socket_directory','proc_root','snapshot_path','ledger_path','collector_path'}:
                    if not Path(value).is_absolute():raise ValueError('provider paths must be absolute')
        if (self.sampler is None)!=(self.steady is None):
            raise ValueError('sampler and explicit steady contract must be configured together')
        if self.sched_switch and (self.sampler is None or self.sampler.proc_root!='/proc'):
            raise ValueError('sched_switch requires local /proc inventory and sampler')
        return self


def _read(path, max_bytes=8*1024*1024):
    with Path(path).open('rb') as stream:
        raw=stream.read(max_bytes+1)
    if len(raw)>max_bytes:raise ValueError('evidence file exceeds configured bound')
    return raw


def _frozen_json(path, sha):
    raw=_read(path)
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('frozen evidence digest mismatch')
    return json.loads(raw)


def _scoped_document(path,environment_ref,max_bytes=8*1024*1024):
    raw=_read(path,max_bytes)
    value=json.loads(raw)
    if not isinstance(value,dict) or value.get('environment_ref')!=environment_ref:
        raise ValueError('collector environment mismatch')
    return value,hashlib.sha256(raw).hexdigest()


class ProbeClient:
    def __init__(self,socket_directory,*,timeout_s,max_reply_bytes):
        self.root=Path(socket_directory)
        self.timeout_s=timeout_s;self.max_reply_bytes=max_reply_bytes

    async def request(self,sandbox_id,request):
        if not isinstance(sandbox_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',sandbox_id):
            raise ValueError('unsafe guest probe identity')
        def call():
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                deadline=time.monotonic()+self.timeout_s
                client.settimeout(self.timeout_s)
                client.connect(str(self.root/(sandbox_id+'.sock')))
                client.sendall(json.dumps(request).encode()+b'\n')
                raw=bytearray()
                while b'\n' not in raw:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('probe deadline')
                    client.settimeout(remaining)
                    block=client.recv(min(4096,self.max_reply_bytes+1-len(raw)))
                    if not block:raise ValueError('incomplete guest probe reply')
                    raw.extend(block)
                    if len(raw)>self.max_reply_bytes:raise ValueError('guest probe reply exceeds bound')
                row=json.loads(raw)
                if row.get('status')!='ok' or not isinstance(row.get('result'),dict):
                    raise ValueError('guest probe rejected request')
                return row['result']
        return await asyncio.to_thread(call)


class CollectorResourceSampler(ResourceSampler):
    """Refresh only collector-specified process identities before each sample."""
    def __init__(self,*args,collector_binding,case,inventory,**kwargs):
        self.collector_binding=collector_binding;self.case=case;self.inventory_binding=inventory
        self.refresh_provider=None
        self.collector_state_path=Path(args[1]).with_suffix('.collector.json')
        super().__init__(*args,**kwargs)

    def sample_once(self):
        try:
            binding=self.collector_binding
            if binding.discovery is not None:
                if self.refresh_provider is None:raise ValueError('scoped discovery provider missing')
                self.refresh_provider()
            doc,sha=_scoped_document(binding.snapshot_path,self.case.environment_ref)
            for key in ('node','boot_id','inventory_ref'):
                if doc.get(key)!=self.inventory_binding[key]:raise ValueError('dynamic inventory scope mismatch')
            version=doc.get('snapshot_version');stamp=doc.get('observed_monotonic_s')
            if (doc.get('complete') is not True or doc.get('work_id')!=self.case.work_id
                    or doc.get('clock_domain')!=self.clock_domain or type(version) is not int or version<1
                    or type(stamp) not in (float,int) or not 0<=time.monotonic()-stamp<=self.case.resources.max_gap_s):
                raise ValueError('incomplete, stale or unbound dynamic inventory')
            cohort=doc.get('cohort_ids')
            if not isinstance(cohort,list) or len(set(cohort))!=len(cohort) or any(not isinstance(sid,str) or not sid for sid in cohort):
                raise ValueError('explicit current cohort identifiers required')
            if self.collector_state_path.exists():
                previous=json.loads(self.collector_state_path.read_text())
                if version<previous['version'] or (version==previous['version'] and sha!=previous['sha256']):
                    raise ValueError('collector snapshot version rollback or conflict')
            self.update_inventory(doc['processes'],source_ref='sha256:'+sha,cohort_ids=cohort)
            atomic_json(self.collector_state_path,{'version':version,'sha256':sha})
            atomic_json(self.collector_state_path.parent/'provider_evidence'/('inventory-'+sha+'.json'),doc)
            sample=super().sample_once()
            if sample.get('inventory_complete') is False:
                self.invalidate_observation('dynamic_inventory_missing_cohort_process')
            return sample
        except Exception:
            self.invalidate_observation('dynamic_inventory_provider_failed')
            raise


def configured_session(case,config,folder):
    """Create a BenchSession using only registered provider kinds and frozen config."""
    from .session import BenchSession
    folder=Path(folder)
    raw=config.get('wiring')
    wiring=WiringConfig.model_validate(raw) if raw is not None else None
    if wiring is not None and wiring.environment_ref!=case.environment_ref:
        raise ValueError('wiring environment differs from case')
    if case.resources is not None and (wiring is None or wiring.sampler is None):
        raise ValueError('resource contract requires explicitly configured sampler and steady bounds')
    if case.method_id.startswith(('B09.','B10.')) and (wiring is None or wiring.cohort is None):
        raise ValueError('density/noise requires configured owned cohort provider')
    if case.method_id=='B07.FIRST' and (wiring is None or wiring.first is None):
        raise ValueError('FIRST requires explicit shared authority')
    if case.completion.endpoint=='SERVER_TERMINAL' and (wiring is None or wiring.terminal is None):
        raise ValueError('server terminal requires correlated collector binding')
    if case.method_id.startswith(('B04.','B05.FULL','B05.PREPARED','B08.')) and (wiring is None or wiring.probe is None):
        raise ValueError('scenario requires configured guest fixture proof provider')
    if case.method_id.startswith('B08.') and (wiring is None or wiring.restore is None):
        raise ValueError('snapshot restore requires an explicit independent fixture budget')
    if case.method_id=='B08.PUBLISHED' and (wiring is None or wiring.source is None):
        raise ValueError('snapshot publication requires an explicit source observer')
    if wiring is None:return BenchSession(case,config,folder)
    if wiring.sampler and wiring.sampler.discovery and (wiring.cohort is None or wiring.sampler.discovery.product!=case.product):
        raise ValueError('runtime discovery requires matching product and scoped cohort')
    if wiring.sampler and case.resources is None:
        raise ValueError('sampler requires a case resource contract')
    if wiring.cohort and not case.method_id.startswith(('B09.','B10.')):
        raise ValueError('owned cohort provider requires a density/noise case')
    if case.method_id=='B10.SLO_VERIFY' and wiring.slo is None:
        raise ValueError('SLO requires a scoped live metric collector')
    if wiring.terminal:
        expected_kind='resume' if case.method_id.startswith('B04.') else 'create'
        if case.product!='kuasar' or wiring.terminal.launch_kind!=expected_kind:
            raise ValueError('terminal profile does not match method/product')
    steady_config=None
    if wiring.steady:
        spec=wiring.steady
        if spec.cohort_stability and wiring.sampler.kind!='collector_procfs':
            raise ValueError('cohort stability requires a dynamic collector inventory')
        steady_config=SteadyConfig(case.resources.window_s,case.resources.min_samples,case.resources.max_gap_s,
            spec.cohort_stability,(spec.minimum,spec.maximum),(spec.slope_minimum,spec.slope_maximum),case.resources.max_wait_s)
    if wiring.restore and (not case.method_id.startswith('B08.') or wiring.restore.max_requests>case.load.requests):
        raise ValueError('restore budget must belong to this finite snapshot case')
    if wiring.restore and case.ttl_s<=case.load.run_timeout_s+case.completion.timeout_s+wiring.restore.timeout_s+case.cleanup_timeout_s:
        raise ValueError('TTL must also cover the separate restore drain budget')
    options={};sampler=None;inventory=None;restore_verifier=None
    if wiring.restore:options['fixture_validation_timeout_s']=wiring.restore.timeout_s
    if wiring.first:
        ledger=FirstCommandLedger(wiring.first.path)
        async def reserve(environment_ref,sandbox_id,generation):
            if environment_ref!=case.environment_ref:raise ValueError('FIRST environment mismatch')
            await asyncio.to_thread(ledger.reserve,digest([environment_ref,sandbox_id]),generation)
        options['first_command_reserver']=reserve
    if wiring.probe:
        proof=_frozen_json(wiring.probe.before_path,wiring.probe.before_sha256)
        if proof.get('environment_ref')!=case.environment_ref or proof.get('fixture_ref')!=case.fixture_ref:
            raise ValueError('guest probe fixture binding mismatch')
        if not proof.get('evidence_ref') or not isinstance(proof.get('sandboxes'),dict):
            raise ValueError('guest fixture baseline evidence required')
        client=ProbeClient(wiring.probe.socket_directory,timeout_s=wiring.probe.timeout_s,
                           max_reply_bytes=wiring.probe.max_reply_bytes)
        async def validate(phase,handle):
            if phase=='snapshot_restore':
                if restore_verifier is None:raise ValueError('budgeted restore adapter missing')
                return await restore_verifier.verify(handle)
            sid=handle['sandbox_id']
            started=time.monotonic()
            def retain(value):
                path=folder/'provider_evidence'/(phase+'-'+digest([sid,started])+'.json')
                atomic_json(path,{'environment_ref':case.environment_ref,'fixture_ref':case.fixture_ref,
                    'sandbox_id':sid,'phase':phase,'started_s':started,'ended_s':time.monotonic(),**value})
            if phase in {'memory_continuity','disk_content'}:
                before=proof['sandboxes'].get(sid)
                if not isinstance(before,dict):raise ValueError('guest baseline missing')
                after=await client.request(sid,{'action':'inspect'})
                retain({'before':before,'after':after})
                return verify_continuity(before,after,mode='memory' if phase=='memory_continuity' else 'disk')['valid']
            if phase=='clone_isolation':
                source_id=handle.get('source_sandbox_id')
                if not source_id:raise ValueError('clone source identity missing')
                ids=list(dict.fromkeys([sid,source_id,*handle.get('sibling_ids',[])]))
                before={key:await client.request(key,{'action':'inspect'}) for key in ids}
                retain({'before':before,'source_sandbox_id':source_id})
                baseline=proof['sandboxes'].get(source_id)
                if not baseline or not verify_continuity(baseline,before[source_id],mode='memory')['valid']:
                    return False
                if not verify_continuity(before[source_id],before[sid],mode='memory')['valid']:
                    return False
                if not verify_continuity(before[source_id],before[sid],mode='disk')['valid']:
                    return False
                if before[sid].get('allocated_bytes',0)<=0:return False
                marker='clone-'+digest([case.work_id,sid])[:24]
                await client.request(sid,{'action':'mutate','index':0,'value':223})
                await client.request(sid,{'action':'write_file','name':marker,'content':marker})
                after={key:await client.request(key,{'action':'inspect'}) for key in ids}
                retain({'before':before,'after':after,'source_sandbox_id':source_id,'mutation_index':0,
                        'mutation_value':223,'file_marker':marker})
                changed=before[sid].get('memory_sha256')!=after[sid].get('memory_sha256')
                private=all(before[key]==after[key] and marker not in after[key].get('files',{}) for key in ids if key!=sid)
                return changed and private and marker in after[sid].get('files',{})
            raise ValueError('unsupported fixture validation phase')
        options['fixture_validator']=validate
    if wiring.sampler:
        binding=wiring.sampler
        inventory=_frozen_json(binding.inventory_path,binding.inventory_sha256)
        if (inventory.get('environment_ref')!=case.environment_ref or
                inventory.get('inventory_ref')!=case.resources.inventory_ref):
            raise ValueError('resource inventory binding mismatch')
        required={'node','boot_id','processes','evidence_ref'}
        if not required<=inventory.keys() or not all(inventory[k] for k in required):
            raise ValueError('explicit attributed process inventory required')
        boot=(Path(binding.proc_root)/'sys/kernel/random/boot_id').read_text().strip()
        if boot!=inventory['boot_id']:raise ValueError('procfs boot differs from inventory')
        for item in inventory['processes']:
            identity=item['identity']
            if any(identity.get(k)!=v for k,v in {'environment':case.environment_ref,'node':inventory['node'],'boot_id':boot}.items()):
                raise ValueError('process inventory scope mismatch')
        if wiring.cohort and wiring.cohort.node!=inventory['node']:
            raise ValueError('cohort and resource node differ')
        domain=digest([case.environment_ref,inventory['node'],boot])
        sampler_type=CollectorResourceSampler if binding.kind=='collector_procfs' else ResourceSampler
        dynamic={'collector_binding':binding,'case':case,'inventory':inventory} if binding.kind=='collector_procfs' else {}
        sampler=sampler_type(inventory['processes'],folder/'sampler.sqlite',proc_root=binding.proc_root,
            clock_ticks=binding.clock_ticks,max_pending=binding.max_pending,clock_domain=domain,run_id=case.work_id,**dynamic)
    else:domain=None
    try:
        sched=None
        if wiring.sched_switch:
            from .sched_peak import BpfSchedSwitchCollector
            from .sched_observation import SchedObservation
            collector=BpfSchedSwitchCollector(run_id=case.work_id,clock_domain='bpf:ktime:'+boot)
            sched=SchedObservation(collector,inventory=lambda:sampler.inventory)
        session=BenchSession(case,config,folder,sampler=sampler,clock_domain=domain,scenario_options=options,
            sched_observer=sched)
    except BaseException:
        if sampler:sampler.close()
        raise
    if wiring.sampler and wiring.sampler.discovery:
        from sandbox_graph.environment.probes.discovery import refresh_snapshot
        discovery=wiring.sampler.discovery
        machine_id=Path(discovery.machine_id_file).read_text().strip()
        if not machine_id:raise ValueError('discovery machine identity missing')
        def refresh_runtime_inventory():
            # Read-only API discovery runs in the existing sampler executor, so
            # its deadline and shutdown are covered by N04/N07's original owner.
            started=time.monotonic();deadline=started+case.resources.max_gap_s
            def remaining():
                seconds=deadline-time.monotonic()
                if seconds<=0:raise TimeoutError('cohort discovery deadline')
                return min(seconds,case.completion.timeout_s)
            if Path(discovery.machine_id_file).read_text().strip()!=machine_id:
                raise ValueError('discovery machine identity changed')
            current_boot=(Path(wiring.sampler.proc_root)/'sys/kernel/random/boot_id').read_text().strip()
            if current_boot!=boot:raise ValueError('discovery boot changed')
            handles=session.adapter.list_owned(case.work_id,remaining());ids=[]
            if len(handles)>10000:raise ValueError('cohort discovery count budget')
            for handle in handles:
                row=session.adapter.info(handle,remaining());node=row
                for key in wiring.cohort.node_field.split('.'):
                    node=node.get(key) if isinstance(node,dict) else None
                if node!=wiring.cohort.node:raise ValueError('discovery placement differs')
                if row.get('metadata',{}).get('run_id')!=case.work_id:raise ValueError('discovery ownership differs')
                # Starting/paused instances are retained: missing processes are
                # explicit gaps, never a zero-cost or fabricated ready sample.
                if row.get('state') not in {'running','paused','starting','creating'}:
                    raise ValueError('unresolved cohort state')
                ids.append(handle['sandbox_id'])
            snapshot=refresh_snapshot(product=discovery.product,roles=discovery.roles,
                runtime_root=discovery.runtime_root,proc_root=wiring.sampler.proc_root,
                machine_id=machine_id,boot_id=boot,context={'environment_ref':case.environment_ref,
                    'node':inventory['node'],'inventory_ref':inventory['inventory_ref'],'work_id':case.work_id,'cohort_ids':ids},
                output=wiring.sampler.snapshot_path)
            remaining()
            return snapshot
        sampler.refresh_provider=refresh_runtime_inventory
    if wiring.probe:atomic_json(folder/'provider_evidence'/'guest_before.json',proof)
    if inventory is not None:atomic_json(folder/'provider_evidence'/'inventory.json',inventory)
    if wiring.source_cleanup:
        from .source_wiring import ConfiguredSourceLifecycle
        lifecycle=ConfiguredSourceLifecycle(session,wiring.source_cleanup.model_dump())
        session.source_lifecycle=lifecycle
        session.scenario.source_pin=lifecycle.pin
        session.scenario.source_bind=lifecycle.bind
        for resource in session.journal.owned(session.environment):
            if resource['kind']=='source':
                # Only an already persisted, explicit source kind can be restored.
                source_kind=resource.get('source_kind')
                if source_kind is None:continue
                lifecycle.restore_owned(resource['resource_id'],source_kind,resource.get('request_index'))
    if wiring.restore:
        from .fixture_lifecycle import RestoreVerifier
        async def validate_restore(handle,source):
            baseline=proof['sandboxes'].get(source.get('source_sandbox_id'))
            if not isinstance(baseline,dict):raise ValueError('snapshot source baseline missing')
            after=await client.request(handle['sandbox_id'],{'action':'inspect'})
            atomic_json(folder/'provider_evidence'/('restore-'+digest([source['source_id'],handle['sandbox_id']])+'.json'),
                {'environment_ref':case.environment_ref,'source_id':source['source_id'],
                 'sandbox_id':handle['sandbox_id'],'before':baseline,'after':after,
                 'validation_mode':wiring.restore.validation_mode})
            valid=verify_continuity(baseline,after,mode='disk')['valid']
            if wiring.restore.validation_mode=='memory_and_disk':
                valid=valid and verify_continuity(baseline,after,mode='memory')['valid']
            return valid
        restore_verifier=RestoreVerifier(case=case,adapter=session.adapter,folder=folder/'fixture_restore',
            max_requests=wiring.restore.max_requests,timeout_s=wiring.restore.timeout_s,
            environment=session.environment,on_owned=session._owned,validator=validate_restore,
            can_submit=lambda:not session.journal.state().get('cancelled') and (session.sampler is None or session.sampler.safe_to_submit),
            process_epoch=session.process_epoch,
            pin_source=session.source_lifecycle.pin if session.source_lifecycle else None,
            bind_child_source=session.source_lifecycle.bind if session.source_lifecycle else None)
        session.fixture_restore=restore_verifier
        for resource in restore_verifier.journal.owned(session.environment):
            session.journal.own('sandbox',resource['resource_id'],{'environment':session.environment,
                'source_id':resource['source_id']})
    if wiring.terminal:
        binding=wiring.terminal
        async def terminal(handle, binding=binding):
            expected=LogIdentity(handle['sandbox_id'],case.work_id,binding.launch_kind,binding.profile)
            parser=TerminalLogParser()
            while True:
                document,accepted_hash=await asyncio.to_thread(_scoped_document,binding.accepted_path,case.environment_ref,binding.max_bytes)
                entries=document.get('operations',[])
                match=next((row for row in entries if all(row.get(key)==getattr(expected,key)
                           for key in ('sid','run_id','kind','profile'))),None)
                evidence=AcceptedAtEvidence(expected,match.get('accepted_at',''),match.get('evidence_ref','')) if match else None
                text=(await asyncio.to_thread(_read,binding.path,binding.max_bytes)).decode('utf-8',errors='strict')
                for raw_record in text.splitlines():
                    parser.parse(raw_record,expected=expected,accepted_at=evidence,observed_at=time.monotonic())
                result=parser.records.get(expected)
                if result is not None:
                    evidence_path=folder/'provider_evidence'/('terminal-'+digest([expected.sid,expected.run_id,expected.kind,expected.profile])+'.json')
                    atomic_json(evidence_path,{'raw_record':result.raw_record,'raw_sha256':result.raw_sha256,
                        'accepted_evidence':match,'accepted_document_sha256':accepted_hash,
                        'collector_ref':binding.collector_ref,'observed_at':result.observed_at,
                        'parser_version':result.parser_version})
                    if result.reason=='terminal_failure':
                        return TerminalObservation(False,session.process_epoch,source_event_time=result.source_event_time,raw_ref='sha256:'+result.raw_sha256)
                    if result.status!='valid':raise ValueError('invalid correlated terminal evidence: '+str(result.reason))
                    return TerminalObservation(True,session.process_epoch,result.internal_duration_ns,
                        result.source_event_time,'sha256:'+result.raw_sha256)
                await asyncio.sleep(case.completion.poll_interval_s)
        session.scenario.terminal_provider=terminal
    if wiring.source:
        binding=wiring.source
        async def source_inspector(source, binding=binding):
            doc,sha=await asyncio.to_thread(_scoped_document,binding.path,case.environment_ref,binding.max_bytes)
            rows=doc.get('sources',{})
            row=rows.get(source['source_id'])
            if not isinstance(row,dict) or not row.get('evidence_ref'):
                return {'state':'unknown','raw_ref':'sha256:'+sha}
            return {'state':row.get('state','unknown'),'raw_ref':'sha256:'+sha,'source_event_time':row.get('source_event_time')}
        session.scenario.source_inspector=source_inspector
    if wiring.cohort:
        binding=wiring.cohort
        async def cohort(binding=binding):
            handles=await asyncio.to_thread(session.adapter.list_owned,case.work_id,case.completion.timeout_s)
            ready=[];paused=[];ids=[]
            for handle in handles:
                row=await asyncio.to_thread(session.adapter.info,handle,case.completion.timeout_s)
                node=row
                for key in binding.node_field.split('.'):
                    node=node.get(key) if isinstance(node,dict) else None
                if node!=binding.node:raise ValueError('observed placement differs from frozen cohort node')
                if row.get('metadata',{}).get('run_id')!=case.work_id:
                    raise ValueError('cohort ownership differs from case')
                sid=handle['sandbox_id'];ids.append(sid)
                if row.get('state')=='running':ready.append(sid)
                if row.get('state')=='paused':paused.append(sid)
            return CohortSnapshot(tuple(ids),tuple(ready),binding.node,case.environment_ref,binding.scope_ref,True,tuple(paused))
        observation_counter=0
        async def resources():
            nonlocal observation_counter
            from .observe import sample_proc
            started=time.monotonic()
            observed=await asyncio.to_thread(sample_proc,wiring.sampler.proc_root)
            boot=(Path(wiring.sampler.proc_root)/'sys/kernel/random/boot_id').read_text().strip()
            if boot!=inventory['boot_id']:raise ValueError('host boot changed')
            observation_counter+=1
            path=folder/'host_samples'/f'{observation_counter:08d}.json'
            envelope={**observed,'environment_ref':case.environment_ref,'node':inventory['node'],
                'boot_id':boot,'clock_domain':domain,'t_s':time.monotonic()}
            atomic_json(path,envelope)
            result={**envelope,'domain':domain,'evidence_ref':str(path),'window_kind':'declared_snapshot',
                    'window':[started,envelope['t_s']],'window_version':'host_snapshot.v1'}
            if wiring.slo:
                metrics,sha=await asyncio.to_thread(_scoped_document,wiring.slo.path,case.environment_ref,wiring.slo.max_bytes)
                age=time.monotonic()-metrics.get('observed_monotonic_s',float('-inf'))
                if (metrics.get('node')!=inventory['node'] or metrics.get('clock_domain')!=domain
                        or metrics.get('work_id')!=case.work_id or not 0<=age<=case.resources.max_gap_s):
                    raise ValueError('SLO metric scope or observation interval mismatch')
                result['metrics']=metrics.get('metrics',{})
                result['metric_evidence_ref']='sha256:'+sha
                atomic_json(folder/'provider_evidence'/('slo-'+sha+'.json'),metrics)
            return result
        async def pause_cohort(ids):
            for index,sid in enumerate(ids):
                if sampler is not None and not sampler.safe_to_submit:raise ValueError('sampler unavailable')
                if not any(row['resource_id']==sid and row['kind']=='sandbox' for row in session.journal.owned(session.environment)):
                    raise ValueError('pause target is not owned')
                event={'request_index':index,'stage':'cohort_pause','child_index':None}
                session.journal.stage_event({**event,'phase':'intent'},max_submissions=3*case.load.requests)
                await asyncio.to_thread(session.adapter.pause,{'sandbox_id':sid},case.completion.timeout_s,memory=True)
                session.journal.stage_event({**event,'phase':'receipt','identity':{'sandbox_id':sid}},max_submissions=3*case.load.requests)
            deadline=time.monotonic()+case.completion.timeout_s
            while True:
                observed=await cohort()
                if set(ids)<=set(observed.paused_ids):return
                if time.monotonic()>=deadline:raise TimeoutError('paused cohort not observed')
                await asyncio.sleep(case.completion.poll_interval_s)
        session.density_controller=DensityController(case,cohort_inspector=cohort,resource_snapshot=resources,
            pause_cohort=pause_cohort if case.method_id=='B09.PAUSED' else None)
    if wiring.steady:
        cfg=steady_config
        async def steady_check(sampler,contract):
            not_before=time.monotonic()
            deadline=not_before+contract.max_wait_s
            while True:
                rows=[]
                for path in (folder/'resource_samples').glob('*.json'):
                    sample=json.loads(path.read_text())
                    sample_at=sample.get('t_s',sample.get('started_s'))
                    if sample_at is None or sample_at<not_before or sample.get('clock_domain')!=domain:continue
                    processes=sample.get('processes',[])
                    valid=processes and sample.get('inventory_complete',True) and all(row.get('status')=='valid' and row.get('pss_bytes') is not None for row in processes)
                    row={'t_s':sample.get('t_s',sample.get('started_s')),'value':sum(p['pss_bytes'] for p in processes) if valid else None,
                         'clock_domain':sample.get('clock_domain')}
                    if cfg.cohort_stability:row['cohort']=sample.get('snapshot_cohort_ids')
                    rows.append(row)
                result=find_steady_window(sorted(rows,key=lambda row:row['t_s']),cfg,started_s=not_before)
                result.update(scope='inventory:'+contract.inventory_ref,clock_domain=domain,
                              process_epoch=session.process_epoch,window_version='steady.first_window.v1',
                              not_before_s=not_before)
                if result['status']=='steady':return {'status':'completed','steady':result}
                if time.monotonic()+contract.interval_s>=deadline:return {'status':'blocked','steady':result}
                await asyncio.sleep(contract.interval_s)
        session.steady_check=steady_check
    return session
