"""N00–N10 local runner bridge with persistent receipts and conservative recovery.

Each session is bound to immutable case/config hashes. No remote submission is replayed
from a missing reply. Formal distributed ownership belongs to the graph repository.
"""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import fcntl
import hashlib
import json
from pathlib import Path
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .adapters import Adapter
from .contracts import CaseContract
from .journal import FileJournal
from .load import LoadEngine, LoadSchedule, OperationResult
from .runner import atomic_json, digest, environment_identity, redact
from .transport import Session
from .measure import error_record, CREATE_CALIBER_ONE_OFF, CREATE_CALIBER_REUSE


def _http_session(config):
    """run 级连接复用（官方口径）；`connection_reuse: false` 时退回单次连接。"""
    if config.get('connection_reuse', True) is False:
        return None
    pool = config.get('http_pool') or {}
    if not isinstance(pool, dict):
        raise ValueError('http_pool must be a mapping')
    return Session(max_per_host=pool.get('max_per_host', 64),
                   idle_timeout_s=pool.get('idle_timeout_s', 90.0))


class BenchSession:
    def __init__(self,case:CaseContract,config,folder,*,scenario=None,sampler=None,steady_check=None,density_controller=None,scenario_options=None,clock_domain=None):
        self.case=case;self.config=dict(config);self.folder=Path(folder)
        self.folder.mkdir(parents=True,exist_ok=True)
        self.environment=environment_identity(config)
        self.process_epoch=uuid.uuid4().hex
        if sampler is not None and (not clock_domain or sampler.clock_domain!=clock_domain):
            raise ValueError('sampler and runner require an explicit common observation clock domain')
        self.clock_domain=clock_domain or self.process_epoch
        identity={'schema':'bench-session.v1','case_hash':case.digest(),'config_hash':digest(config),
                  'environment':self.environment,'work_id':case.work_id,'group_id':case.group_id}
        # Creation/identity check shares a process-safe lock with all subsequent sessions.
        with (self.folder/'identity.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            path=self.folder/'session.json'
            if path.exists():
                if json.loads(path.read_text())!=identity:raise ValueError('session identity/config conflict')
            else:
                atomic_json(path,identity);atomic_json(self.folder/'case.json',case.model_dump(mode='json'))
        self.journal=FileJournal(self.folder/'ledger',case.work_id,
                                child_budget=case.load.requests,parent_budget=case.load.requests//(case.clone_count or 1))
        self.adapter=Adapter(config, session=_http_session(config))
        if scenario is None:
            from .scenarios import ScenarioExecutor
            scenario=ScenarioExecutor(case,self.adapter,on_owned=self._owned,on_progress=self._build_event,
                stage_journal=self._stage_event,process_epoch=self.process_epoch,**(scenario_options or {}))
        self.scenario=scenario;self.sampler=sampler;self.steady_check=steady_check
        self.density_controller=density_controller
        self.source_lifecycle=None
        if density_controller is not None and (density_controller.case.digest()!=case.digest()
                or density_controller.clock is not time.monotonic):
            raise ValueError('density controller must bind the same case and host monotonic observation clock')
        self._sample_task=None;self._sample_stop=asyncio.Event()
        self._sampler_closed=False;self._sampler_close_future=None
        self._sampler_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='sandbox-observer') if sampler else None

    def _load_journal(self):
        session=self
        class GuardedJournal:
            def __getattr__(self,name):return getattr(session.journal,name)
            async def submission_started(self,token):
                if session.sampler is not None and not session.sampler.safe_to_submit:
                    raise OSError('resource observation unavailable; new submissions stopped')
                await session.journal.submission_started(token)
        return GuardedJournal()

    async def _owned(self,handle):
        sid=handle.get('resource_id') or handle.get('sandbox_id') or handle.get('source_id')
        if not sid:raise ValueError('owned resource identifier missing')
        kind=handle.get('kind') or ('sandbox' if 'sandbox_id' in handle else 'source')
        if handle.get('owner',self.case.work_id)!=self.case.work_id:raise ValueError('resource owner mismatch')
        attributes={'environment':self.environment}
        for field in ('source_id','source_kind','consumer_ref'):
            if handle.get(field) is not None:attributes[field]=handle[field]
        if kind=='source':attributes['request_index']=handle.get('request_index')
        self.journal.own(kind,sid,attributes)
        if kind=='source' and self.source_lifecycle is not None:
            await self.source_lifecycle.own_source(sid,handle.get('source_kind'),handle.get('request_index'))

    def _build_event(self,event):
        if event.get('phase')=='intent' and self.sampler is not None and not self.sampler.safe_to_submit:
            raise OSError('observation unavailable; build submission denied')
        self.journal.stage_event(redact(event),max_submissions=self.case.load.requests*2)

    async def _stage_event(self,event):
        if event.get('phase')=='intent' and not event.get('compensation') and self.sampler is not None and not self.sampler.safe_to_submit:
            raise OSError('observation unavailable; child submission denied')
        # Every clone child plus at most pause/capture/source-resume per parent.
        parents=self.case.load.requests//(self.case.clone_count or 1)
        self.journal.stage_event(redact(event),max_submissions=self.case.load.requests+3*parents)

    async def apply_step(self,step_id,input_hash,*,visit_id=None):
        frozen=json.loads((self.folder/'session.json').read_text())
        if frozen['case_hash']!=self.case.digest() or frozen['config_hash']!=digest(self.config):
            raise ValueError('session inputs were mutated after binding')
        if step_id not in {f'N{i:02}' for i in range(11)}:raise ValueError('unregistered benchmark step')
        step_key=step_id if visit_id is None else step_id+':'+visit_id
        existing=self.journal.receipt(step_key,input_hash)
        if existing is not None:
            if step_id=='N10':await self._N10()
            if step_id=='N04' and self.case.resources is not None and (
                    self._sample_task is None or self._sample_task.done()):
                return {'status':'unknown','reason':'interrupted_observation_requires_new_interval'}
            if step_id=='N02' and existing.get('status') in ('ready','completed'):
                # setup only attaches already prepared identities; it performs no remote preparation.
                await self.scenario.setup(targets=self.config.get('targets',()),fixture=self.config.get('fixture_input',{}))
            return existing
        # Fail closed instead of blocking the event loop behind another local worker.
        with (self.folder/'execution.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'status':'running','reason':'another_local_owner'}
            existing=self.journal.receipt(step_key,input_hash)
            if existing is not None:return existing
            fresh=self.journal.begin_step(step_key,input_hash)
            if not fresh and step_id in ('N02','N04','N05'):
                return await self.reconcile(step_id)
            if self.journal.state().get('cancelled') and step_id not in ('N03','N07','N08','N09','N10'):
                result={'status':'cancelled','reason':'persisted_cancel_request'}
            else:
                try:result=await getattr(self,'_'+step_id)()
                except Exception as exc:
                    result={'status':'unknown' if step_id in ('N02','N05') else 'failed',**error_record(exc)}
            result=redact(result)
            self.journal.receipt(step_key,input_hash,result)
            return result

    async def _N00(self):
        if self.config.get('product')!=self.case.product:raise ValueError('product mismatch')
        return {'status':'completed','case_hash':self.case.digest(),'environment':self.environment}

    async def _N01(self):
        from .fixtures import verify_source,verify_effective_spec,verify_request_spec
        proof=self.config.get('environment_evidence',{})
        if proof.get('ref')!=self.case.environment_ref or proof.get('product_version')!=self.case.product_version:
            return {'status':'blocked','reason':'versioned_environment_evidence_required'}
        executes=(self.case.method_id.startswith('B07.')
                  or self.case.completion.endpoint=='EXEC_COMPLETE'
                  or self.case.completion.validation_policy!='none')
        if self.case.product=='agentenv' and executes:
            protocol=self.config.get('protocol_evidence',{})
            if not isinstance(protocol,dict):
                return {'status':'blocked','reason':'agentenv_versioned_codec_evidence_required'}
            if protocol.get('codec')=='protobuf':
                return {'status':'blocked','reason':'agentenv_protobuf_execution_unsupported'}
            if (protocol.get('environment_ref')!=self.case.environment_ref
                    or protocol.get('product_version')!=self.case.product_version
                    or protocol.get('codec')!='connect-json'
                    or not isinstance(protocol.get('evidence_ref'),str)
                    or not protocol['evidence_ref'].strip()):
                return {'status':'blocked','reason':'agentenv_versioned_codec_evidence_required'}
        if self.config.get('source')!={'kind':self.case.source.kind,'id':self.case.source.id}:
            return {'status':'blocked','reason':'source_config_mismatch'}
        body=self.config.get('create_body',{})
        if type(body.get('timeout')) not in (int,float) or body['timeout']!=self.case.ttl_s:
            return {'status':'blocked','reason':'request_ttl_differs_from_case_contract'}
        source_evidence=self.config.get('source_evidence',{})
        if verify_source(self.case.source,source_evidence,environment_ref=self.case.environment_ref,
                         cache_profile=self.case.cache_profile)['status']!='valid':
            return {'status':'blocked','reason':'versioned_source_cache_evidence_required'}
        spec_check=verify_effective_spec(self.case.spec.model_dump(),source_evidence.get('effective_spec',{}))
        if spec_check['status']!='valid':
            return {'status':'blocked','reason':'source_effective_spec_not_verified','spec_check':spec_check}
        request_spec=verify_request_spec(self.case,self.config)
        if request_spec['status']!='valid':
            return {'status':'blocked','reason':'requested_spec_not_verified','spec_check':request_spec}
        if self.case.resources and self.sampler is None:
            return {'status':'blocked','reason':'resource_sampler_required'}
        if self.case.method_id.startswith(('B09.','B10.')) and self.density_controller is None:
            return {'status':'blocked','reason':'cohort_resource_controller_required'}
        return {'status':'completed','environment_ref':self.case.environment_ref,'source_digest':self.case.source.digest}

    async def _N02(self):
        result=await self.scenario.setup(targets=self.config.get('targets',()),fixture=self.config.get('fixture_input',{}))
        if result is None or isinstance(result,dict) and result.get('status') in ('completed','ready'):
            self._persist_target_bindings()
        return result if isinstance(result,dict) else {'status':'completed'}

    def _persist_target_bindings(self):
        targets=self.config.get('targets',())
        fixture=self.config.get('fixture_input',{})
        if not targets:return
        if not fixture.get('evidence_ref') or fixture.get('environment_ref')!=self.case.environment_ref:
            raise ValueError('prepared target identities require fixture evidence')
        value={'case_hash':self.case.digest(),'environment':self.environment,
               'fixture_ref':fixture['evidence_ref'],
               'targets':[{k:target[k] for k in ('sandbox_id','generation') if k in target} for target in targets]}
        path=self.folder/'target_bindings.json'
        if path.exists() and json.loads(path.read_text())!=value:
            raise ValueError('prepared target bindings changed')
        if not path.exists():atomic_json(path,value)

    def _target_bindings(self):
        path=self.folder/'target_bindings.json'
        if not path.exists():return []
        value=json.loads(path.read_text())
        if value.get('case_hash')!=self.case.digest() or value.get('environment')!=self.environment or not value.get('fixture_ref'):
            raise ValueError('persisted target binding identity mismatch')
        return value['targets']

    def _measurement_results(self,report):
        """Recover child facts from durable receipts even when parent cancellation lost its value."""
        import copy
        results=copy.deepcopy(report.get('results',{}))
        restore_state=None
        restore_path=self.folder/'fixture_restore'/'journal.json'
        if self.case.method_id.startswith('B08.') and restore_path.exists():
            restore_state=json.loads(restore_path.read_text())
            if restore_state.get('work_id')!=self.case.work_id+':snapshot-restore':
                raise ValueError('restore journal belongs to another capture case')
        for op in self.journal.state()['operations'].values():
            receipts=op.get('children',{})
            if not receipts and not self.case.method_id.startswith('B05.'):continue
            index=str(op['index'])
            parent=results.setdefault(index,copy.deepcopy(op.get('result',{'status':'unknown','value':None})))
            value=parent.get('value') or {}
            children=list(value.get('children',[]))
            if len(children)>op['weight']:raise ValueError('child evidence exceeds reserved budget')
            children.extend({'status':'unsent' if op['status'] in ('reserved','unsent') else 'unknown','reason':'child_terminal_evidence_missing'}
                            for _ in range(op['weight']-len(children)))
            for child_index,receipt in receipts.items():
                child_result=receipt['result'];child=copy.deepcopy(child_result.get('value') or {})
                child['status']=child_result['status']
                if self.case.method_id.startswith('B08.') and int(child_index)==0:
                    source=child.get('source',{})
                    if value.get('source') is not None and value['source']!=source:
                        raise ValueError('parent validation belongs to another capture source')
                    if value.get('completion') is not None and value['completion']!=child.get('completion'):
                        raise ValueError('parent capture completion conflicts with durable receipt')
                    validation=value.get('validation')
                    if isinstance(validation,dict) and validation.get('reason') not in ('deadline','validation_error','invalid_assertion_result'):
                        child['validation']=copy.deepcopy(validation)
                    saved=(restore_state or {}).get('receipts',{}).get('restore:'+index)
                    if saved is not None:
                        timeout=self.config.get('wiring',{}).get('restore',{}).get('timeout_s')
                        if type(timeout) not in (int,float):raise ValueError('restore receipt lacks bound timeout configuration')
                        expected=digest({'case':self.case.digest(),'source_id':source.get('source_id'),
                            'kind':source.get('kind'),'request_index':int(index),'environment':self.environment,
                            'timeout_s':float(timeout)})
                        receipt=saved['result']
                        if (saved['input_hash']!=expected or receipt.get('source_id')!=source.get('source_id')
                                or receipt.get('request_index')!=int(index)):
                            raise ValueError('restore receipt does not bind the captured source')
                        # Only an actual completed boolean assertion decides semantic validity.
                        if (receipt.get('status') in ('valid','invalid')
                                and type(receipt.get('validation')) is bool
                                and (receipt.get('completion') or {}).get('status')=='success'):
                            child['validation']={'success':receipt['validation'],
                                'restore_receipt_ref':'fixture_restore/journal.json#/receipts/restore:'+index,
                                'restored_sandbox_id':receipt.get('sandbox_id')}
                        else:child.pop('validation',None)
                if child.get('completion',{}).get('observed_at') is None and receipt.get('completed_at') is not None:
                    # Receipt times use this session clock; no mapping is invented on recovery.
                    if report.get('clock_domain')==self.clock_domain:
                        child.setdefault('completion',{})['observed_at']=receipt['completed_at']
                existing=children[int(child_index)]
                if existing.get('reason')!='child_terminal_evidence_missing':
                    for key in ('status','sandbox_id','completion'):
                        if key in child and key in existing and child[key]!=existing[key]:
                            raise ValueError('conflicting parent and durable child evidence')
                children[int(child_index)]={**child,**existing} if existing.get('reason')!='child_terminal_evidence_missing' else child
            parent['value']={**value,'children':children}
        return results

    def _subjects(self,report):
        targets=self._target_bindings()
        for index,result in self._measurement_results(report).items():
            value=result.get('value') or {}
            if not isinstance(value,dict):value={}
            if 'children' in value:
                for child_index,child in enumerate(value['children']):
                    yield f'{index}:{child_index}',dict(child)
            else:
                subject=dict(value,status=result['status'])
                if not subject.get('sandbox_id') and self.case.method_id.startswith(('B03.','B04.','B06.','B07.')):
                    slot=0 if self.case.method_id.startswith('B07.') and len(targets)==1 else int(index)
                    if slot<len(targets):subject.update(targets[slot])
                yield str(index),subject

    def _recovery_evidence(self,report=None):
        if report is None:
            path=self.folder/'load.json'
            report=json.loads(path.read_text()) if path.exists() else {}
        assertions=[];gaps=[];notes=[]
        state=self.journal.state()
        for index,result in self._measurement_results(report).items():
            value=result.get('value') or {}
            if not isinstance(value,dict):continue
            if value.get('validation_interference'):
                notes.append({'kind':'validation_interference','operation_id':str(index),'value':True})
                gaps.append('validation_interference:'+str(index))
            recovery=value.get('source_recovery')
            if (self.case.source_must_run and self.case.method_id.startswith('B05.')
                    and result['status']!='unsent') or recovery is not None or value.get('source_residual'):
                status=recovery.get('status','missing') if isinstance(recovery,dict) else 'missing'
                qualification='valid' if status=='success' else 'invalid' if status=='failed' else 'unverified'
                if value.get('source_residual') and qualification=='valid':qualification='unverified'
                assertions.append({'kind':'post_validation','assertion':'source_recovery','operation_id':str(index),
                                   'status':qualification,'source_recovery':recovery,'source_residual':value.get('source_residual')})
                if status!='success':gaps.append(f'source_recovery:{index}:{status}')
                if value.get('source_residual') and status=='success':gaps.append(f'source_residual:{index}')
        observations={}
        for event in state['events']:
            evidence=event.get('evidence',{})
            if event['kind']=='stage_observed' and evidence.get('stage')=='source_resume':
                observations[str(evidence['request_index'])]=evidence
        for index,evidence in observations.items():
            status=evidence.get('completion',{}).get('status','unknown')
            if status!='success':
                gaps.append(f'source_recovery:{index}:{status}')
                assertions.append({'kind':'post_validation','assertion':'source_recovery_observed',
                                   'status':'invalid' if status=='failed' else 'unverified',
                                   'operation_id':index,'source_recovery':evidence})
        if state.get('observation_pending'):
            gaps.append('observation_evidence_pending')
            assertions.append({'kind':'post_validation','assertion':'observation_evidence',
                               'status':'unverified','reason':'sampler_tail_unacknowledged'})
        for key,stage in state.get('stages',{}).items():
            if stage['phase']=='intent':
                gaps.append('stage_outcome_unknown:'+key)
                assertions.append({'kind':'post_validation','assertion':'stage_outcome',
                                   'status':'unverified','stage_ref':key,'reason':'stage_submission_without_receipt'})
        for operation in self._fixture_unknown():
            gaps.append('fixture_restore_unknown:'+operation['operation_id'])
            assertions.append({'kind':'post_validation','assertion':'fixture_restore_reconciliation',
                               **operation,'operation_status':operation['status'],'status':'unverified'})
        if self.source_lifecycle and any(event['kind']=='cleanup_observed' for event in state['events']):
            for consumer in self.source_lifecycle.residuals():
                gaps.append('source_consumer_pending:'+consumer['consumer_ref'])
        return assertions,sorted(set(gaps)),notes

    async def _N03(self):return await self.reconcile()

    async def _sample_loop(self):
        while not self._sample_stop.is_set():
            try:
                await asyncio.get_running_loop().run_in_executor(self._sampler_pool,self._capture_sample)
            except Exception as exc:
                self.journal.audit('resource_gap',**error_record(exc))
            try:await asyncio.wait_for(self._sample_stop.wait(),self.case.resources.interval_s)
            except TimeoutError:pass

    def _resource_sink(self, sample):
        folder=self.folder/'resource_samples';folder.mkdir(exist_ok=True)
        path=folder/(digest(sample['sample_id'])+'.json')
        if path.exists():
            if json.loads(path.read_text())!=sample:raise ValueError('sample identity conflict')
        else:atomic_json(path,sample)
        return True

    def _capture_sample(self):
        sample=self.sampler.sample_once()
        result=self.sampler.reconcile(self._resource_sink)
        if result['status']!='reconciled':raise OSError('sampler sink disconnected')
        return sample

    async def _N04(self):
        if self.case.resources is None:return {'status':'completed','observation':'not_requested'}
        if self.sampler is None:return {'status':'blocked','reason':'sampler_required'}
        # Persist handle before sampling; after a process restart this interval is interrupted.
        with self.journal._transaction() as state:state['observation_pending']=True
        self.journal.audit('observation_started',process_epoch=self.process_epoch)
        baseline=asyncio.get_running_loop().run_in_executor(self._sampler_pool,self._capture_sample)
        try:sample=await asyncio.wait_for(asyncio.shield(baseline),self.case.resources.max_wait_s)
        except TimeoutError:
            self.journal.audit('observation_baseline_unknown',process_epoch=self.process_epoch)
            return {'status':'unknown','reason':'baseline_sample_deadline','observation_pending':True}
        self.journal.audit('baseline_sample',sample_id=sample['sample_id'])
        self._sample_task=asyncio.create_task(self._sample_loop())
        return {'status':'completed','observation_handle':self.process_epoch}

    async def _N05(self):
        if self.case.resources is not None and (self._sample_task is None or self._sample_task.done()):
            return {'status':'blocked','reason':'active_observation_required_before_load'}
        if self.journal.state()['operations']:
            return {'status':'unknown','reason':'prior_request_budget_exists_reconcile_before_load'}
        await self.scenario.setup(targets=self.config.get('targets',()),fixture=self.config.get('fixture_input',{}))
        self._persist_target_bindings()
        if self.case.method_id.startswith(('B09.','B10.')):
            if self.density_controller is None:return {'status':'blocked','reason':'cohort_resource_controller_required'}
            density=await self.density_controller.run(self.scenario.create_member,journal=self._load_journal())
            value=asdict(density);atomic_json(self.folder/'density.json',value)
            reports=value['load_reports']
            if reports:
                combined={'started_at':reports[0]['started_at'],'finished_at':reports[-1]['finished_at'],
                          'events':[],'results':{},'process_epoch':self.process_epoch,'clock_domain':self.clock_domain}
                offset=0
                for report in reports:
                    for index,result in report['results'].items():combined['results'][str(int(index)+offset)]=result
                    for event in report['events']:
                        if event['request_index'] is not None:event['request_index']+=offset
                        combined['events'].append(event)
                    offset+=report['allocated']
                atomic_json(self.folder/'load.json',combined)
            return {'status':'completed' if density.status=='complete' and density.stop_reason not in ('journal_error','unknown_limit','deadline') else 'blocked' if density.status=='blocked' else 'failed',
                    'artifact_ref':'density.json','reason':density.stop_reason}
        load=self.case.load;weight=self.case.clone_count if self.case.method_id.startswith('B05.') else 1
        if load.requests%weight:raise ValueError('child budget must contain complete native fork operations')
        parents=load.requests//weight
        callback_timeout=self.case.completion.timeout_s
        if self.case.method_id.startswith('B08.'):
            callback_timeout+=self.config.get('wiring',{}).get('restore',{}).get('timeout_s',0)
        schedule=LoadSchedule(load.mode,parents,load.concurrency,callback_timeout,
            batch_size=load.batch_size,gap=load.batch_gap_s or 0,
            scheduled_at=tuple(i/load.arrival_rate for i in range(parents)) if load.mode=='OPEN_LOOP' else (),
            late_policy=load.late_policy or 'drop',max_queue=load.max_queue or 0,
            unknown_limit=load.max_unknown_external,deadline=load.run_timeout_s,
            child_weights=(weight,)*parents,child_budget=load.requests,parent_budget=parents)
        async def execute(index):
            if self.sampler is not None and not self.sampler.safe_to_submit:
                return OperationResult('unsent',reason='resource_observation_unavailable')
            return await self.scenario.execute(index)
        progress=getattr(self.scenario,'execute_with_progress',None) if self.case.method_id.startswith(('B05.','B08.')) else None
        report=await LoadEngine().run(schedule,execute,journal=self._load_journal(),progress_operation=progress)
        atomic_json(self.folder/'load.json',asdict(report)|{'process_epoch':self.process_epoch,'clock_domain':self.clock_domain})
        return {'status':'unknown' if report.unresolved_external else 'failed' if report.stop_reason=='journal_error'
                or any(x.status=='unsent' for x in report.results.values()) else 'completed',
                'submitted':report.submitted,'successes':report.successes,'stop_reason':report.stop_reason,
                'unresolved_external':report.unresolved_external,'artifact_ref':'load.json'}

    async def _N06(self):
        if self.case.resources is None:return {'status':'completed','steady':'not_requested'}
        if self.steady_check is None:return {'status':'blocked','reason':'steady_contract_adapter_required'}
        result=await asyncio.wait_for(self.steady_check(self.sampler,self.case.resources),self.case.resources.max_wait_s)
        atomic_json(self.folder/'steady.json',result)
        return result

    async def _N07(self):
        if self._sampler_closed:return {'status':'completed'}
        if self.sampler is None and self.journal.state().get('observation_pending'):
            return {'status':'unknown','reason':'original_sampler_spool_required'}
        deadline=time.monotonic()+self.case.cleanup_timeout_s
        if self.sampler is not None:
            with self.journal._transaction() as state:state['observation_pending']=True
        self._sample_stop.set()
        if self._sample_task is not None:
            try:await asyncio.wait_for(asyncio.shield(self._sample_task),max(.001,deadline-time.monotonic()))
            except TimeoutError:
                self.journal.audit('observation_stop_unknown',process_epoch=self.process_epoch)
                return {'status':'unknown','reason':'sampler_stop_deadline'}
        if self.sampler is not None:
            def close_sampler():
                reconciliation=self.sampler.reconcile(self._resource_sink)
                if reconciliation.get('status')!='reconciled' or reconciliation.get('pending',0):return reconciliation
                self.sampler.close()
                return reconciliation
            if self._sampler_close_future is None:
                self._sampler_close_future=asyncio.get_running_loop().run_in_executor(self._sampler_pool,close_sampler)
            closing=self._sampler_close_future
            try:
                reconciliation=await asyncio.wait_for(asyncio.shield(closing),max(.001,deadline-time.monotonic()))
            except TimeoutError:
                return {'status':'unknown','reason':'sampler_close_deadline'}
            if reconciliation.get('status')!='reconciled' or reconciliation.get('pending',0):
                self._sampler_close_future=None
                self.journal.audit('observation_tail_unacknowledged',reconciliation=reconciliation)
                return {'status':'unknown','reason':'sampler_pending_evidence',
                        'pending_samples':reconciliation.get('pending'),'spool_retained':True}
            self._sampler_pool.shutdown(wait=False)
            self._sampler_closed=True
            with self.journal._transaction() as state:state['observation_pending']=False
        self.journal.audit('observation_stopped',process_epoch=self.process_epoch)
        return {'status':'completed'}

    def close(self):
        """关闭 run 级连接池（不计入测量）。"""
        session=getattr(self.adapter,'session',None)
        if session is not None:
            session.close()

    def records(self):
        load_path=self.folder/'load.json'
        report=json.loads(load_path.read_text()) if load_path.exists() else {}
        rows=[];events=[];terminal_times=[];parents=[]
        for index,result in self._measurement_results(report).items():
            value=result.get('value') or {}
            if not isinstance(value,dict):value={}
            completion=value.get('completion',{})
            elapsed=value.get('elapsed_s',completion.get('latency'))
            api_status=('rejected' if completion.get('reason')=='api_rejected'
                        else 'not_sent' if result['status']=='unsent'
                        else 'accepted' if completion else 'unknown')
            row={'operation_id':str(index),'status':result['status'],'elapsed_s':elapsed,
                         'api_status':api_status,
                         'caliber_version':(CREATE_CALIBER_REUSE if getattr(self.adapter,'session',None) is not None
                                             else CREATE_CALIBER_ONE_OFF),
                         'method':self.case.method_id,'repeat':self.case.repeat_id,'product':self.case.product,
                         'completion_kind':self.case.completion.endpoint,
                         'clock_domain':report.get('clock_domain'),'fixture_id':self.case.fixture_ref,
                         'cache_mode':self.case.cache_profile,'specification':self.case.spec.model_dump()}
            row.update({key:value[key] for key in ('source_recovery','source_residual','validation_interference') if key in value})
            children=value.get('children')
            if children is not None:
                parents.append(row)
                for child_index,child in enumerate(children):
                    child_completion=child.get('completion',{})
                    oid=f'{index}:{child_index}'
                    child_api=('rejected' if child_completion.get('reason')=='api_rejected'
                               else 'accepted' if child_completion else 'unknown')
                    rows.append({**row,'operation_id':oid,'parent_operation_id':str(index),
                                 'api_status':child_api,
                                 'status':child['status'],'elapsed_s':child_completion.get('latency')})
                    at=child_completion.get('observed_at')
                    if child['status']=='success' and at is not None:
                        events.append({'operation_id':oid,'completed_s':at,'clock_domain':report.get('clock_domain')})
            else:rows.append(row)
            terminal=completion.get('observed_at')
            if self.case.method_id.startswith('B08.') and children:
                capture_times=[child.get('completion',{}).get('observed_at') for child in children]
                if all(at is not None for at in capture_times):terminal=max(capture_times)
            if terminal is None:
                terminal=next((x['completed_at'] for x in report.get('events',[])
                               if x['kind']=='completed' and str(x['request_index'])==str(index)),None)
            if terminal is not None:terminal_times.append(terminal)
            if children is None and result['status']=='success' and value.get('success_command_throughput_eligible',True) and terminal is not None:
                events.append({'operation_id':str(index),'completed_s':terminal,'clock_domain':report.get('clock_domain')})
        records={'case_id':self.case.work_id,'operations':rows,'events':events,'resource_series':[],
                 'cohorts':[],'assertions':[],'cleanup_ledger':self.journal.state().get('cleanup',[]),
                 'raw_refs':[{'path':'load.json','sha256':digest(report)}] if report else [],
                 'case_hash':self.case.digest(),'plan_ref':self.case.plan_ref,'contract_ref':self.case.contract_ref,
                 'environment_ref':self.case.environment_ref,'validation_policy':self.case.completion.validation_policy}
        if parents:records['parent_operations']=parents
        inventory=getattr(self.sampler,'inventory',None)
        if inventory:records['inventory']=inventory
        recovery_assertions,records['evidence_gaps'],records['qualification_notes']=self._recovery_evidence(report)
        samples=[json.loads(p.read_text()) for p in (self.folder/'resource_samples').glob('*.json')]
        expected_sequence=1
        for sample in sorted(samples,key=lambda s:s['seq']):
            if sample['seq']!=expected_sequence:raise ValueError('resource source sequence is missing or duplicated')
            expected_sequence=sample.get('through_seq',sample['seq'])+1
            sample['source_seq']=sample.pop('seq')
            records['resource_series'].append(sample)
        if self.case.resources:records['resource_max_gap_s']=self.case.resources.max_gap_s
        steady_path=self.folder/'steady.json'
        if steady_path.exists():records['steady']=json.loads(steady_path.read_text()).get('steady')
        if self.config.get('guest_pss_proof') is not None:records['guest_pss_proof']=self.config['guest_pss_proof']
        end=max(terminal_times) if terminal_times else report.get('finished_at')
        if report and end>report['started_at']:
            records['window']={'start_s':report['started_at'],'end_s':end,
                              'window_kind':'natural_drain','clock_domain':report['clock_domain']}
        density_path=self.folder/'density.json'
        if density_path.exists():
            density=json.loads(density_path.read_text());records['density']=density
            checksum=hashlib.sha256(density_path.read_bytes()).hexdigest()
            records['density_ref']={'logical_name':'density.json','sha256':checksum,'path':'raw/'+checksum}
            if density.get('final_cohort'):
                records['cohorts']=[density['final_cohort']]
                records['final_cohort']=density['final_cohort']['ids']
            if density.get('creation_throughput_status')=='not_applicable':records.pop('window',None)
        validation_path=self.folder/'validation.json'
        if validation_path.exists():records['assertions']=json.loads(validation_path.read_text())
        records['assertions'].extend(row for row in recovery_assertions if row not in records['assertions'])
        records['comparison']={'product':self.case.product,'product_version':self.case.product_version,
            'method_id':self.case.method_id,'method_version':self.case.method_version,
            'completion':self.case.completion.model_dump(mode='json'),'spec':self.case.spec.model_dump(mode='json'),
            'cache_profile':self.case.cache_profile,
            'condition':self.config.get('comparison_condition'),
            'window':{'kind':'natural_drain','load':self.case.load.model_dump(mode='json'),
                      'resources':self.case.resources.model_dump(mode='json') if self.case.resources else None}}
        return records

    async def _N08(self):
        from .analysis import analyze_case
        from .fixtures import verify_effective_spec
        validations=await self._validate_commands()
        load_path=self.folder/'load.json'
        report=json.loads(load_path.read_text()) if load_path.exists() else {}
        recovery,gaps,notes=self._recovery_evidence(report)
        validations.extend(recovery)
        if load_path.exists() and self.case.method_id.startswith(('B04.','B05.','B08.')):
            for _,subject in self._subjects(report):
                if subject.get('status')!='success':continue
                check=subject.get('validation')
                validations.append({'kind':'post_validation','sandbox_id':subject.get('sandbox_id'),
                    'assertion':'scenario_semantic_validation',
                    'status':'unverified' if check is None else 'valid' if check.get('success') else 'invalid'})
        spec_ids={r['resource_id'] for r in self.journal.owned(self.environment) if r['kind']=='sandbox'}
        spec_ids.update(subject['sandbox_id'] for _,subject in self._subjects(report)
                        if subject.get('status')=='success' and subject.get('sandbox_id'))
        for sid in sorted(spec_ids):
            try:
                effective=await asyncio.to_thread(self.adapter.effective_spec,
                    {'sandbox_id':sid},self.case.completion.timeout_s)
                actual={'vcpus':effective.get('vcpus'),
                        'memory_bytes':effective['memory_mib']*1024*1024 if effective.get('memory_mib') is not None else None}
                check=verify_effective_spec(self.case.spec.model_dump(),actual)
            except Exception as exc:check={'status':'unverified',**error_record(exc)}
            validations.append({'kind':'effective_spec','sandbox_id':sid,**check})
        atomic_json(self.folder/'validation.json',validations)
        result=analyze_case(self.records());atomic_json(self.folder/'analysis.json',result)
        return {'status':'completed','artifact_ref':'analysis.json','analysis':result,
                'gaps':gaps,
                'data_status':result.get('qualifications',{}).get('status','unverified')}

    async def _validate_commands(self):
        policy=self.case.completion.validation_policy
        if policy=='none':return []
        path=self.folder/'load.json'
        if not path.exists():return [{'kind':'post_validation','status':'unverified','reason':'measurement_records_missing'}]
        load=json.loads(path.read_text());rows=[]
        for index,value in self._subjects(load):
            if value['status']!='success':continue
            sid=value.get('sandbox_id')
            if policy=='stream_after':
                evidence=value.get('post_validation')
                rows.append({'kind':'post_validation','sandbox_id':sid,
                    'status':'unverified' if evidence is None else 'valid' if evidence.get('success') else 'invalid'})
                continue
            if sid is None:
                rows.append({'kind':'post_validation','status':'unverified','reason':'target_identity_missing'})
                continue
            key='validation:'+str(index);binding=digest({'case':self.case.digest(),'sandbox_id':sid})
            cached=self.journal.receipt(key,binding)
            if cached is not None:rows.append(cached);continue
            if not self.journal.begin_step(key,binding):
                rows.append({'kind':'post_validation','sandbox_id':sid,'status':'unverified','reason':'validation_outcome_unknown'})
                continue
            handle={'sandbox_id':sid}
            try:
                await asyncio.to_thread(self.adapter.info,handle,self.case.completion.timeout_s)
                reply=await asyncio.to_thread(self.adapter.execute,handle,self.case.completion.command,
                                             self.case.completion.timeout_s)
                stdout=reply.get('stdout')
                if not isinstance(stdout,(str,bytes)):raise ValueError('stdout byte evidence missing')
                raw=stdout.encode('utf-8') if isinstance(stdout,str) else stdout
                size=reply.get('stdout_bytes',len(raw));checksum=reply.get('stdout_sha256',hashlib.sha256(raw).hexdigest())
                expected=self.case.completion.expected_stdout.encode('utf-8')
                if type(size) is not int or size<0 or not isinstance(checksum,str) or len(checksum)!=64:
                    raise ValueError('stdout byte evidence invalid')
                ok=(size==len(expected) and checksum==hashlib.sha256(expected).hexdigest()
                    and reply.get('exit_code')==self.case.completion.expected_exit)
                row={'kind':'post_validation','sandbox_id':sid,'status':'valid' if ok else 'invalid',
                     'stdout_bytes':size,'stdout_sha256':checksum,'expected_stdout_sha256':hashlib.sha256(expected).hexdigest()}
            except Exception as exc:row={'kind':'post_validation','sandbox_id':sid,'status':'unverified',**error_record(exc)}
            self.journal.receipt(key,binding,row);rows.append(row)
        return rows

    async def _observe_gone(self, sid, deadline):
        """有界核验异步删除是否真的 gone；无法核验返回 None（保持 unverified）。"""
        inspect=getattr(self.adapter,'inspect',None)
        if inspect is None:return None
        for _ in range(3):
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            try:
                data=await asyncio.to_thread(inspect,{'sandbox_id':sid},remaining)
            except Exception:
                return None
            state=data.get('state') if isinstance(data,dict) else None
            if state in ('gone','dead','not_found','deleted'):return state
            await asyncio.sleep(0.05)
        return None

    async def _N09(self, resource_ids=None):
        rows=[]
        deadline=time.monotonic()+self.case.cleanup_timeout_s
        for resource in sorted(self.journal.owned(self.environment),key=lambda row:row['kind']!='sandbox'):
            if resource_ids is not None and f"{resource['kind']}:{resource['resource_id']}" not in resource_ids:
                continue
            if resource['kind']!='sandbox':
                if self.source_lifecycle is None:
                    result={'status':'retained','reason':'source_references_require_validation'}
                elif deadline<=time.monotonic():
                    result={'status':'unknown','reason':'cleanup_deadline'}
                else:
                    result=await self.source_lifecycle.cleanup(resource['resource_id'],deadline-time.monotonic())
                rows.append({'resource_id':resource['resource_id'],**result})
                continue
            sid=resource['resource_id']
            try:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError()
                await asyncio.to_thread(self.adapter.delete,{'sandbox_id':sid},remaining)
                status='api_deleted'
            except Exception as exc:
                status='api_absent' if getattr(exc,'status',None)==404 else 'unknown'
            from .operation_sync import completion_plan,physical_release_status
            product=getattr(self.adapter,'product',None)
            observed=None
            if status=='api_deleted' and completion_plan(product,'delete')['verified'] \
                    and not completion_plan(product,'delete')['synchronous']:
                observed=await self._observe_gone(sid,deadline)
            rows.append({'resource_id':sid,'status':status,
                         'physical_release':physical_release_status(product,status,observed_state=observed)})
        if self.source_lifecycle:
            await self.source_lifecycle.reconcile_consumers(deadline-time.monotonic())
        # WF-37/D19: an ownership sweep catches orphans not recorded in the journal and verifies
        # no residual. It only runs when the environment declares a restore contract.
        sweep=None;swept=bool(self.config.get('restore'))
        if swept:
            from .restore import restore_environment
            restore_config=self.config.get('restore') or {}
            try:
                sweep=await asyncio.to_thread(restore_environment,self.adapter,
                    run_id=self.case.work_id,
                    batch_size=int(restore_config.get('batch_size',5)),
                    batch_gap_s=float(restore_config.get('batch_gap_s',0)),
                    max_rounds=int(restore_config.get('max_rounds',100)),
                    timeout=self.case.cleanup_timeout_s,
                    deadline=time.monotonic()+self.case.cleanup_timeout_s,
                    baseline_ids=tuple(restore_config.get('baseline_ids',())))
            except Exception as exc:
                sweep={'status':'unknown','reason':type(exc).__name__,'residual_ids':None}
        residual_ids=list((sweep or {}).get('residual_ids') or [])
        with self.journal._transaction() as state:
            state.setdefault('cleanup',[]).extend(rows)
            self.journal._event(state,'cleanup_observed',rows=rows,restore_sweep=sweep)
        uncertain=[x for x in self.journal.state()['operations'].values()
                   if x['status'] in ('submission_started','unknown','timeout')]
        _,gaps,_=self._recovery_evidence()
        residual=[gap for gap in gaps if not gap.startswith('validation_interference:')]
        fixture_unknown=self._fixture_unknown()
        source_consumers=self.source_lifecycle.residuals() if self.source_lifecycle else []
        sweep_open=swept and (sweep is None or sweep.get('status')!='complete')
        complete=(not uncertain and not fixture_unknown and not source_consumers and not residual
                  and not residual_ids and not sweep_open
                  and all(x['status'] in ('api_deleted','api_absent','complete') for x in rows))
        return {'status':'completed' if complete else 'incomplete',
                'cleanup':rows,'unknown_operations':len(uncertain),'fixture_unknown_operations':fixture_unknown,
                'source_consumers':source_consumers,'physical_release':'unverified','gaps':residual,
                'restore_sweep':sweep,'residual_ids':residual_ids}

    def _fixture_unknown(self):
        verifier=getattr(self,'fixture_restore',None)
        if verifier is None:return []
        state=verifier.journal.state()
        identities={event['operation_id']:event for event in state['events'] if event['kind']=='restore_intent'}
        return [{'operation_id':key,'status':row['status'],'source_id':identities.get(key,{}).get('source_id')}
                for key,row in state['operations'].items()
                if row['status'] in ('submission_started','unknown','timeout')]

    async def _N10(self):
        from .analysis import export_case,verify_case
        output=self.folder/'evidence'
        state=self.journal.state()
        execution={key:state.get(key) for key in ('work_id','child_budget','parent_budget','child_reserved',
                   'parent_reserved','operations','stages','resources','cleanup','cancelled','observation_pending')}
        execution['stage_events']=[event for event in state['events'] if event['kind'].startswith('stage_')]
        # Exclude N10's own step/receipt bookkeeping so retries preserve identical evidence bytes.
        atomic_json(self.folder/'execution_journal.json',execution)
        raw_payloads={p.name:p.read_bytes() for p in (self.folder/'load.json',self.folder/'density.json',
                     self.folder/'case.json',self.folder/'validation.json',self.folder/'target_bindings.json',
                     self.folder/'steady.json',self.folder/'execution_journal.json') if p.exists()}
        raw_payloads.update({'resource/'+p.name:p.read_bytes() for p in (self.folder/'resource_samples').glob('*.json')})
        if self.source_lifecycle is not None:self.source_lifecycle.export()
        for folder in ('host_samples','provider_evidence','fixture_restore','source_lifecycle'):
            raw_payloads.update({folder+'/'+p.relative_to(self.folder/folder).as_posix():p.read_bytes()
                                 for p in (self.folder/folder).rglob('*.json')})
        records=self.records()
        signature=digest({'records':{k:v for k,v in records.items() if k!='raw_refs'},
                          'raw':{name:hashlib.sha256(data).hexdigest() for name,data in raw_payloads.items()}})
        records['seal_inputs_sha256']=signature
        manifest=verify_case(output) if output.exists() else export_case(records,output,raw_payloads=raw_payloads)
        if manifest['metadata'].get('seal_inputs_sha256')!=signature:
            raise ValueError('seal inputs changed; publish a new evidence version instead of reusing the old seal')
        if manifest['case_id']!=self.case.work_id or any(manifest['metadata'].get(k)!=v for k,v in {
                'case_hash':self.case.digest(),'plan_ref':self.case.plan_ref,'contract_ref':self.case.contract_ref}.items()):
            raise ValueError('sealed evidence does not belong to current work/contract')
        self.close()
        return {'status':'completed','artifact_ref':'evidence/case_manifest.json','manifest':manifest}

    async def reconcile(self,step_id=None,*,discover=False):
        state=self.journal.state()
        unknown=[key for key,row in state['operations'].items()
                 if row['status'] in ('submission_started','unknown','timeout')]
        _,gaps,_=self._recovery_evidence()
        residual=[gap for gap in gaps if not gap.startswith('validation_interference:')]
        fixture_unknown=self._fixture_unknown()
        if discover and (unknown or fixture_unknown):
            try:
                handles=await asyncio.to_thread(self.adapter.list_owned,self.case.work_id,self.case.cleanup_timeout_s)
                discovered=[]
                for handle in handles:
                    # Recheck ownership despite the adapter's own list filter.
                    metadata=handle.get('private',{}).get('metadata',{})
                    if metadata.get('run_id')!=self.case.work_id:
                        raise ValueError('discovered resource lacks exact work ownership')
                    if fixture_unknown and (not unknown or metadata.get('benchmark_fixture_role')=='snapshot_restore'):
                        matching=next((row for row in fixture_unknown
                            if row['source_id'] and row['source_id']==metadata.get('parent_source_id')
                            and row['operation_id']==metadata.get('benchmark_fixture_operation')),None)
                        if metadata.get('benchmark_fixture_role')!='snapshot_restore' or matching is None:
                            continue
                        await self._owned({'sandbox_id':handle['sandbox_id'],'source_id':matching['source_id']})
                    else:await self._owned(handle)
                    discovered.append(handle['sandbox_id'])
                self.journal.audit('resource_reconciliation',discovered_ids=discovered,
                                   association='work_scope_only_operation_still_unknown')
            except Exception as exc:
                self.journal.audit('reconciliation_gap',**error_record(exc))
        if unknown or fixture_unknown or residual or (step_id in ('N02','N04','N05') and step_id not in state['receipts']):
            return {'status':'unknown','reason':'external_effect_or_interrupted_step_requires_reconciliation',
                    'operation_ids':unknown,'fixture_unknown_operations':fixture_unknown,
                    'resources':self.journal.owned(self.environment),'gaps':residual}
        return {'status':'completed','resources':self.journal.owned(self.environment)}

    async def cancel(self,reason):
        self.journal.cancel(reason);self._sample_stop.set()
        return {'status':'cancel_requested','external_cancel_confirmed':False}

    def collect(self,last_seq=0):return self.journal.collect(last_seq)
