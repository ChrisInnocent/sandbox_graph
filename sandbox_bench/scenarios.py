"""Executable base-scenario boundaries over the audited adapter protocol.

Fixture creation is deliberately not hidden in setup: preparatory side effects need
separate runner budgets/receipts. Missing targets, proofs, and callbacks fail before
submission. Operations are called once; the owning LoadEngine supplies admission.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, replace
import hashlib
import inspect as python_inspect
import time
import uuid
from typing import Any, Awaitable, Callable

from .completion import (Acceptance, CompletionObserver, CompletionResult, CompletionSpec,
                         StateObservation, TerminalObservation)
from .contracts import CaseContract
from .load import OperationResult
from .transport import TransportError


class ScenarioBlocked(ValueError):
    """The declared operation has no sufficient executable fixture/evidence."""


class ScenarioExecutor:
    def __init__(self, case: CaseContract, adapter: Any, *,
                 on_owned: Callable[[dict], Awaitable[None]],
                 on_progress: Callable[[dict], None] | None = None,
                 stage_journal: Callable[[dict], Awaitable[None]] | None = None,
                 first_command_reserver: Callable[[str,str,Any], Awaitable[Any]] | None = None,
                 fixture_validator: Callable[[str, dict], Awaitable[bool]] | None = None,
                 fixture_validation_timeout_s: float | None = None,
                 source_inspector: Callable[[dict], Awaitable[dict]] | None = None,
                 source_pin: Callable | None = None,
                 source_bind: Callable | None = None,
                 terminal_provider: Callable[[dict], Awaitable[TerminalObservation]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 process_epoch: str | None = None):
        self.case, self.adapter, self.on_owned = case, adapter, on_owned
        self.on_progress, self.fixture_validator = on_progress, fixture_validator
        self.fixture_validation_timeout_s=fixture_validation_timeout_s
        if fixture_validation_timeout_s is not None:
            CompletionSpec('STATE_READY',fixture_validation_timeout_s,case.completion.poll_interval_s)
        self.stage_journal = stage_journal
        self.first_command_reserver = first_command_reserver
        self._begun_stages: set[tuple] = set()
        self._stage_started: dict[tuple,float] = {}
        self._child_sink = None
        self.source_inspector, self.terminal_provider = source_inspector, terminal_provider
        self.source_pin,self.source_bind=source_pin,source_bind
        self.clock, self.sleep = clock, sleep
        self.process_epoch = process_epoch or str(uuid.uuid4())
        self.observer = CompletionObserver(process_epoch=self.process_epoch, clock=clock, sleep=sleep)
        self.targets: list[dict] = []
        self.fixture: dict = {}
        self.owned: list[dict] = []
        self._pending_validation: list[tuple[int, dict]] = []
        self._executed_targets: set[str] = set()

    async def setup(self, *, targets: list[dict] | tuple[dict, ...] = (),
                    fixture: dict | None = None) -> None:
        fixture = copy.deepcopy(fixture or {})
        if targets and (not fixture.get('evidence_ref') or fixture.get('environment_ref') != self.case.environment_ref):
            raise ScenarioBlocked('target_fixture_evidence_required')
        ids = [handle.get('sandbox_id') for handle in targets]
        if any(not isinstance(sid, str) or not sid for sid in ids) or len(set(ids)) != len(ids):
            raise ScenarioBlocked('unique_target_identities_required')
        self.targets, self.fixture = copy.deepcopy(list(targets)), fixture

    async def _call(self, function: Callable, *args, **kwargs):
        # The audited adapter is synchronous with finite I/O deadlines. Async
        # adapters can be injected without blocking the load scheduler.
        if python_inspect.iscoroutinefunction(function):
            return await function(*args, **kwargs)
        result = await asyncio.to_thread(function, *args, **kwargs)
        if python_inspect.isawaitable(result):
            return await result
        return result

    def _remaining(self, began: float) -> float:
        remaining = began + self.case.completion.timeout_s - self.clock()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _target(self, index: int, *, shared: bool = False) -> dict:
        if not self.targets:
            raise ScenarioBlocked('prepared_target_required')
        if shared:
            return self.targets[0]
        if index >= len(self.targets):
            # A single command fixture can serve steady commands, but must not
            # repeat delete/pause/resume measurements over the same object.
            if self.case.method_id.startswith('B07.') and len(self.targets) == 1:
                return self.targets[0]
            raise ScenarioBlocked('one_prepared_target_per_request_required')
        return self.targets[index]

    async def _own(self, kind: str, resource_id: str, handle: dict, *, index: int) -> None:
        if not isinstance(resource_id, str) or not resource_id:
            raise ValueError('owned resource identity missing')
        if any(row['kind'] == kind and row['resource_id'] == resource_id for row in self.owned):
            raise ValueError('duplicate resource returned by a new operation')
        row = {'kind': kind, 'resource_id': resource_id, 'environment': self.case.environment_ref,
               'owner': self.case.work_id, 'request_index': index, 'handle': copy.deepcopy(handle)}
        # Keep the known resource even if persistence fails, for bounded reconcile.
        self.owned.append(row)
        safe_row = {k:v for k,v in row.items() if k != 'handle'}
        safe_row['handle'] = {'sandbox_id':resource_id} if kind == 'sandbox' else {'source_id':resource_id}
        if kind=='source':safe_row['source_kind']=handle.get('kind')
        for field in ('source_id','source_kind','consumer_ref'):
            if kind=='sandbox' and field in handle:safe_row[field]=handle[field]
        await self.on_owned(safe_row)

    def _spec(self, *, target: str | None = None, kind: str | None = None) -> CompletionSpec:
        contract = self.case.completion
        return CompletionSpec(kind or contract.endpoint, contract.timeout_s, contract.poll_interval_s,
                              (target or contract.target_state,))

    async def _command_assertion(self, handle: dict, began: float, adapter: Any = None) -> dict:
        contract = self.case.completion
        if not contract.command or contract.expected_stdout is None:
            raise ScenarioBlocked('explicit_command_and_output_required')
        self._executed_targets.add(handle['sandbox_id'])
        result = await self._call((adapter or self.adapter).execute, handle, contract.command, self._remaining(began))
        stdout = result.get('stdout')
        if not isinstance(stdout, (bytes,str)):
            return {'success': False, 'reason': 'invalid_output_type'}
        # Display text may contain UTF-8 replacement characters; the decoder's
        # original byte digest is authoritative for output identity.
        raw = stdout.encode() if isinstance(stdout,str) else stdout
        size = result.get('stdout_bytes',len(raw))
        sha = result.get('stdout_sha256',hashlib.sha256(raw).hexdigest())
        if type(size) is not int or size < 0 or not isinstance(sha,str) or len(sha)!=64:
            return {'success':False,'reason':'invalid_output_evidence'}
        expected = contract.expected_stdout.encode()
        expected_sha = hashlib.sha256(expected).hexdigest()
        success = size == len(expected) and sha == expected_sha and result.get('exit_code') == contract.expected_exit
        return {'success': success, 'exit_code': result.get('exit_code'),
                'stdout_bytes': size, 'stdout_sha256': sha,
                'expected_stdout_sha256': expected_sha,
                'reason': None if success else 'command_assertion_failed'}

    async def _complete(self, handle: dict, began: float, *, adapter: Any = None,
                        target: str | None = None, kind: str | None = None,
                        command_record: dict | None = None, absent: bool = False,
                        measurement_started: float | None = None):
        adapter = adapter or self.adapter
        async def inspect(handle):
            try:
                data = await self._call(adapter.inspect, handle, self._remaining(began))
            except TransportError as error:
                if absent and error.status == 404:
                    return StateObservation('absent', self.process_epoch)
                raise
            state = data.get('state', 'unknown')
            return StateObservation(state, self.process_epoch, data.get('raw_ref'), data.get('source_event_time'))
        async def execute(handle):
            result = await self._command_assertion(handle, began, adapter)
            if command_record is not None:
                command_record.update(result)
            return TerminalObservation(result['success'], self.process_epoch)
        measured = began if measurement_started is None else measurement_started
        spec = replace(self._spec(target=target, kind=kind),
                       timeout=began+self.case.completion.timeout_s-measured)
        return await self.observer.observe(spec,
            submitted_at=measured, process_epoch=self.process_epoch,
            accepted=Acceptance(True, self.clock(), self.process_epoch, handle),
            inspect=inspect, execute=execute, server_terminal=self.terminal_provider)

    async def _fixture_assertion(self, phase: str, handle: dict) -> dict:
        if self.fixture_validator is None:
            raise ScenarioBlocked('fixture_validator_required')
        result = await self.observer.validate(lambda: self.fixture_validator(phase, handle),
            timeout=(self.fixture_validation_timeout_s if phase=='snapshot_restore'
                     and self.fixture_validation_timeout_s is not None else self.case.completion.timeout_s))
        return asdict(result)

    def preflight(self, index: int = 0) -> None:
        """No I/O: runner can call this before consuming a durable request slot."""
        method = self.case.method_id
        if self.case.completion.endpoint == 'SERVER_TERMINAL' and self.terminal_provider is None:
            raise ScenarioBlocked('correlated_terminal_provider_required')
        if method.startswith(('B01.', 'B02.')) and self.case.source.artifact_kind and self.case.source.launch_path:
            # 已声明身份维度的用例必须与 BCD-12 §12.2 路由一致，不得命名所致误归类。
            from .source_resolution import Resolution, method_for_source
            source = self.case.source
            resolution = Resolution(source.artifact_kind, source.launch_path,
                                    source.producer_step or 'pre_existing', source.id, source.digest,
                                    source.producer_ref, 'request',
                                    source.resolution_ref or self.case.contract_ref, 'resolved')
            cost_scope = 'build_included' if source.producer_step == 'build_job' else 'prepared'
            family = 'image' if method.startswith('B02') else 'template'
            if method_for_source(resolution, cost_scope=cost_scope, family=family) != method:
                raise ScenarioBlocked('source_identity_method_mismatch')
        if method == 'B02.FULL' and self.on_progress is None:
            raise ScenarioBlocked('durable_build_progress_callback_required')
        if method.startswith(('B03.', 'B04.', 'B05.', 'B06.', 'B07.', 'B08.')):
            target = self._target(index, shared=method.startswith(('B05.', 'B08.')))
            if method == 'B07.FIRST' and (self.first_command_reserver is None
                    or not isinstance(target.get('generation'),(str,int))
                    or isinstance(target.get('generation'),bool) or not target.get('generation')):
                raise ScenarioBlocked('durable_first_authority_and_generation_required')
            if method == 'B07.FIRST' and (self.fixture.get('first_command_eligible') is not True or target['sandbox_id'] in self._executed_targets):
                raise ScenarioBlocked('first_command_eligibility_missing_or_consumed')
        if method.startswith('B07.') and (not self.case.completion.command or self.case.completion.expected_stdout is None):
            raise ScenarioBlocked('explicit_command_and_output_required')
        if method == 'B04.MEMORY' and (not self.fixture.get('memory_challenge_ref') or self.fixture_validator is None):
            raise ScenarioBlocked('memory_continuity_evidence_required')
        if method == 'B04.DISK' and (not self.fixture.get('disk_challenge_ref') or self.fixture_validator is None):
            raise ScenarioBlocked('disk_content_evidence_required')
        if method in {'B05.FULL', 'B05.PREPARED'}:
            if self.stage_journal is None:
                raise ScenarioBlocked('clone_expansion_requires_child_stage_journal')
            if self.fixture_validator is None:
                raise ScenarioBlocked('clone_isolation_validator_required')
            if method == 'B05.PREPARED' and self.case.source.kind != 'snapshot':
                raise ScenarioBlocked('prepared_clone_requires_snapshot_source')
        if method == 'B05.NATIVE' and self.case.source_must_run and self.stage_journal is None:
            raise ScenarioBlocked('source_recovery_requires_stage_journal')
        if method == 'B05.NATIVE' and self.case.product == 'kuasar':
            raise ScenarioBlocked('native_fork_unsupported_for_profile')
        if method.startswith('B08.'):
            if type(self.fixture.get('touched_bytes')) is not int or self.fixture['touched_bytes'] <= 0 or not self.fixture.get('touch_evidence_ref'):
                raise ScenarioBlocked('actual_touch_evidence_required')
            if method == 'B08.PUBLISHED' and self.source_inspector is None:
                raise ScenarioBlocked('publication_observer_required')
            if self.fixture_validator is None:
                raise ScenarioBlocked('snapshot_restore_validator_required')
        if method.startswith(('B09.', 'B10.GROWTH', 'B10.PROJECTION', 'B10.SLO_VERIFY', 'E')):
            raise ScenarioBlocked('dedicated_cohort_or_extension_session_required')

    async def execute_with_progress(self, index: int, child_terminal) -> OperationResult:
        """Bind one parent-scoped sink without sharing it across concurrent calls."""
        invocation = copy.copy(self)
        invocation._child_sink = child_terminal
        result = await invocation.execute(index)
        if not self.case.method_id.startswith(('B05.','B08.')):
            value = result.value if isinstance(result.value,dict) else {}
            at = value.get('completion',{}).get('observed_at',self.clock())
            await child_terminal(0,result,at)
        return result

    async def _notify_child(self, child_index: int, row: dict) -> None:
        if self._child_sink is not None:
            at = row.get('completion',{}).get('observed_at',self.clock())
            await self._child_sink(child_index,OperationResult(row['status'],
                row.get('unresolved_external',0),value=row),at)

    async def _observe_clone_child(self, child_index: int, handle: dict, began: float,
                                   adapter: Any, measured: float):
        result = await self._complete(handle,began,adapter=adapter,measurement_started=measured)
        await self._notify_child(child_index,{'status':result.status,
            'unresolved_external':result.unresolved_external,'sandbox_id':handle['sandbox_id'],
            'completion':asdict(result)})
        return result

    async def create_member(self, index: int) -> OperationResult:
        """Explicit cohort preparation action, called only under controller budgets."""
        if not self.case.method_id.startswith(('B09.', 'B10.')):
            raise ScenarioBlocked('cohort_member_requires_density_or_noise_contract')
        member = copy.copy(self)
        method, source = self._member_identity()
        member.case = self.case.model_copy(update={'method_id': method, 'source': source})
        return await member.execute(index)

    def _member_identity(self):
        """派生 cohort 成员用例的方法与来源身份；歧义的快照身份必须显式声明。"""
        source = self.case.source
        if source.artifact_kind and source.launch_path:
            from .source_resolution import Resolution, method_for_source
            resolution = Resolution(source.artifact_kind, source.launch_path,
                                    source.producer_step or 'pre_existing', source.id, source.digest,
                                    source.producer_ref, 'request',
                                    source.resolution_ref or self.case.contract_ref, 'resolved')
            cost_scope = 'build_included' if source.producer_step == 'build_job' else 'prepared'
            family = 'image' if source.kind == 'image' else 'template'
            return method_for_source(resolution, cost_scope=cost_scope, family=family), source
        if source.kind == 'snapshot':
            # 快照磁盘恢复与内存恢复不可由名称区分，必须声明维度。
            raise ScenarioBlocked('cohort_source_identity_required')
        artifact_kind, launch_path = {
            'template': ('sandbox_artifact', 'cold_boot'),
            'image': ('image_artifact', 'cold_boot'),
        }[source.kind]
        declared = source.model_copy(update={'artifact_kind': artifact_kind, 'launch_path': launch_path,
                                             'producer_step': source.producer_step or 'pre_existing'})
        return {'template': 'B01.TEMPLATE', 'image': 'B02.FULL'}[source.kind], declared

    async def execute(self, index: int) -> OperationResult:
        if self.case.method_id.startswith('B00.'):
            from .scenarios_build import BuildScenario
            builder = BuildScenario(self.case, self.adapter, on_progress=self.on_progress,
                                    stage_journal=self.stage_journal, clock=self.clock, sleep=self.sleep)
            return await builder.execute(index)
        try:
            self.preflight(index)
        except ScenarioBlocked as error:
            return OperationResult('unsent', reason=str(error), value={'execution': 'blocked'})
        method, began = self.case.method_id, self.clock()
        value: dict = {'method_id': method, 'request_index': index}
        try:
            adapter = self.adapter
            if method.startswith(('B01.', 'B02.', 'B10.FIXED_REQUESTS.')):
                if method == 'B02.FULL':
                    adapter = copy.copy(self.adapter)
                    adapter.config = copy.deepcopy(self.adapter.config)
                    adapter.config['source'] = self.case.source.model_dump()
                    built = await self._call(adapter.build_image, self.case.work_id, self._remaining(began),
                                             self.case.completion.poll_interval_s,
                                             on_progress=lambda row:self.on_progress({**row,'request_index':index}))
                    value['build'] = built
                    if not built.get('build_in_create'):
                        await self._own('source', built['source_id'], built, index=index)
                    adapter.config['source'] = {'kind': built['kind'], 'id': built['source_id']}
                source={'source_id':adapter.config['source']['id'],'kind':adapter.config['source']['kind']}
                consumer=await self.source_pin(source,'request:'+str(index)) if self.source_pin else None
                handle = await self._call(adapter.create, self.case.work_id, self._remaining(began))
                if consumer:
                    await self.source_bind(source,handle['sandbox_id'],consumer)
                    handle={**handle,'source_id':source['source_id'],'source_kind':source['kind'],'consumer_ref':consumer}
                await self._own('sandbox', handle['sandbox_id'], handle, index=index)
                result = await self._complete(handle, began, adapter=adapter)
                value['sandbox_id'] = handle['sandbox_id']
            elif method == 'B03.PAUSE':
                handle = self._target(index)
                await self._call(adapter.pause, handle, self._remaining(began), memory=True)
                result = await self._complete(handle, began, target='paused')
            elif method.startswith('B04.'):
                handle = self._target(index)
                await self._call(adapter.resume, handle, self._remaining(began),
                                 memory=method == 'B04.MEMORY', ttl=self.case.ttl_s)
                result = await self._complete(handle, began, target='running')
                if result.status == 'success':
                    value['validation_interference'] = True
                    value['validation'] = await self._fixture_assertion('memory_continuity' if method == 'B04.MEMORY' else 'disk_content', handle)
            elif method == 'B05.NATIVE':
                return await self._native_fork(index, began)
            elif method in {'B05.FULL', 'B05.PREPARED'}:
                return await self._expanded_clone(index, began)
            elif method.startswith('B06.'):
                handle = self._target(index)
                await self._call(adapter.delete, handle, self._remaining(began))
                result = await self._complete(handle, began, target='absent', absent=True)
                value['physical_release'] = 'unverified'
            elif method.startswith('B07.'):
                handle = self._target(index)
                value['command'] = {}
                if method == 'B07.FIRST':
                    self._executed_targets.add(handle['sandbox_id'])
                    try:
                        granted = await self.observer._bounded(
                            lambda:self.first_command_reserver(self.case.environment_ref,
                                handle['sandbox_id'],handle['generation']),
                            began+self.case.completion.timeout_s)
                        if granted is False:
                            raise ScenarioBlocked('first_authority_denied')
                    except Exception as error:
                        return OperationResult('unsent',reason='first_authority_denied',
                            value={**value,'execution':'blocked','error_type':type(error).__name__})
                command_began = self.clock()
                result = await self._complete(handle, began, kind='EXEC_COMPLETE',
                                              command_record=value['command'], measurement_started=command_began)
                if self.case.completion.expected_exit != 0 and result.status == 'success':
                    value['expected_error'] = True
                    value['success_command_throughput_eligible'] = False
            elif method.startswith('B08.'):
                return await self._snapshot(index, began)
            else:
                return OperationResult('unsent', reason='method_not_implemented', value={'execution':'blocked'})
            value['completion'] = asdict(result)
            if result.status == 'success' and not method.startswith('B07.'):
                if self.case.completion.validation_policy == 'batch_after':
                    self._pending_validation.append((index, copy.deepcopy(handle)))
                elif self.case.completion.validation_policy == 'stream_after':
                    value['validation_interference'] = True
                    try:
                        value['post_validation'] = await self._command_assertion(handle, self.clock())
                    except Exception as error:
                        value['post_validation'] = {'success':False, 'reason':'validation_error',
                                                    'error_type':type(error).__name__}
            return OperationResult(result.status, result.unresolved_external, value=value)
        except TimeoutError:
            return OperationResult('timeout', self.case.clone_count if method.startswith('B05.') else 1,
                                   'scenario_deadline', value)
        except NotImplementedError:
            return OperationResult('unsent', reason='adapter_profile_not_implemented', value=value)
        except Exception as error:
            # A thrown write response cannot establish no external side effect.
            return OperationResult('unknown', self.case.clone_count if method.startswith('B05.') else 1,
                                   'scenario_error', {**value, 'error_type': type(error).__name__})

    async def _stage_call(self, index: int, stage: str, child_index: int | None,
                          function: Callable, *args, compensation: bool = False, **kwargs):
        key = (index, stage, child_index)
        if key in self._begun_stages:
            raise ScenarioBlocked('stage_already_started_reconcile_required')
        self._begun_stages.add(key)
        event = {'request_index':index, 'stage':stage, 'child_index':child_index,
                 'compensation':compensation}
        # This hook must persist a fenced submission permission and consume any
        # per-stage HTTP budget before returning. A checkpoint-only callback is
        # insufficient authorization. The executor never replays a begun stage.
        await self.stage_journal({**event, 'phase':'intent'})
        self._stage_started[key] = self.clock()
        result = await self._call(function, *args, **kwargs)
        identity = {}
        if isinstance(result, dict):
            for field in ('sandbox_id', 'source_id', 'kind', 'registration_id', 'build_id'):
                if field in result:
                    identity[field] = result[field]
        await self.stage_journal({**event, 'phase':'receipt', 'identity':identity})
        return result

    async def _restore_source(self, index: int, source: dict, began: float):
        if not self.case.source_must_run:
            return None
        recovery_started = self.clock()
        deadline = recovery_started + self.case.cleanup_timeout_s
        async def restore():
            observed = await self._call(self.adapter.inspect, source, deadline-self.clock())
            if observed.get('state') == 'paused':
                await self._stage_call(index, 'source_resume', None, self.adapter.resume,
                    source, deadline-self.clock(), ttl=self.case.ttl_s, compensation=True)
            async def inspect(handle):
                row = await self._call(self.adapter.inspect, handle, deadline-self.clock())
                return StateObservation(row.get('state','unknown'),self.process_epoch)
            return await self.observer.observe(CompletionSpec('STATE_READY',self.case.cleanup_timeout_s,
                self.case.completion.poll_interval_s), submitted_at=recovery_started,
                process_epoch=self.process_epoch,
                accepted=Acceptance(True,self.clock(),self.process_epoch,source),inspect=inspect)
        try:
            result = await self.observer._bounded(restore,deadline)
        except Exception as error:
            result = CompletionResult('STATE_READY','timeout' if isinstance(error,TimeoutError) else 'unknown',
                recovery_started,self.clock(),deadline,self.process_epoch,None,
                reason='source_recovery_'+type(error).__name__,submitted_epoch=self.process_epoch)
        # Persist even during parent cancellation: the returned parent result may
        # be discarded by its timeout, while recovery facts remain actionable.
        try:
            await self.observer._bounded(lambda:self.stage_journal({
                'request_index':index,'stage':'source_resume','child_index':None,
                'phase':'observed','compensation':True,'sandbox_id':source['sandbox_id'],
                'completion':asdict(result)}),self.clock()+self.case.cleanup_timeout_s)
        except Exception:
            result = replace(result,status='unknown',latency=None,reason='source_recovery_evidence_unpersisted')
        return result

    async def _expanded_clone(self, index: int, began: float) -> OperationResult:
        source_handle = self._target(index, shared=True)
        source = None
        children: list[dict | None] = [None] * self.case.clone_count
        handles: dict[int,dict] = {}
        observations: dict[int,asyncio.Task] = {}
        stage_error = None
        try:
            if self.case.method_id == 'B05.FULL':
                if self.case.product == 'kuasar':
                    await self._stage_call(index, 'source_pause', None, self.adapter.pause,
                                          source_handle, self._remaining(began), memory=True)
                    paused = await self._complete(source_handle, began, target='paused', kind='STATE_READY')
                    if paused.status != 'success':
                        raise ScenarioBlocked('source_pause_not_observed')
                    capture = self.adapter.export_snapshot
                else:
                    capture = self.adapter.snapshot
                source = await self._stage_call(index, 'capture', None, capture, source_handle, self._remaining(began))
                await self._own('source', source['source_id'], source, index=index)
            else:
                source = {'source_id':self.case.source.id, 'kind':self.case.source.kind}
            adapter = copy.copy(self.adapter)
            adapter.config = copy.deepcopy(self.adapter.config)
            adapter.config['source'] = {'kind':source['kind'], 'id':source['source_id']}
            for child_index in range(self.case.clone_count):
                key = (index,'child_create',child_index)
                try:
                    consumer=await self.source_pin(source,f'clone:{index}:{child_index}') if self.source_pin else None
                    handle = await self._stage_call(index, 'child_create', child_index,
                        adapter.create, self.case.work_id, self._remaining(began))
                    if consumer:
                        await self.source_bind(source,handle['sandbox_id'],consumer)
                        handle={**handle,'source_id':source['source_id'],'source_kind':source['kind'],'consumer_ref':consumer}
                    handles[child_index] = handle
                    await self._own('sandbox', handle['sandbox_id'], handle, index=index)
                    observations[child_index] = asyncio.create_task(self._observe_clone_child(
                        child_index,handle,began,adapter,self._stage_started[key]))
                except Exception as error:
                    sent = key in self._stage_started
                    children[child_index] = {'status':'unknown' if sent else 'unsent',
                        'unresolved_external':int(sent),'error_type':type(error).__name__,
                        'sandbox_id':handles.get(child_index,{}).get('sandbox_id')}
                    await self._notify_child(child_index,children[child_index])
                    break
            if observations:
                await asyncio.gather(*observations.values())
        except Exception as error:
            stage_error = {'error_type':type(error).__name__,'reason':'stage_reconciliation_required'}
        finally:
            for task in observations.values():
                if not task.done():task.cancel()
            await asyncio.gather(*observations.values(), return_exceptions=True)
            # Recovery is compensation, even when ordinary intent is fenced or
            # the parent was cancelled. It has its own finite cleanup deadline.
            source_result = await self._restore_source(index, source_handle, began)
        for child_index, task in observations.items():
            if not task.cancelled() and task.exception() is None:
                result = task.result()
                children[child_index] = {'status':result.status,'sandbox_id':handles[child_index]['sandbox_id'],
                    'completion':asdict(result),'unresolved_external':result.unresolved_external}
        for child_index, row in enumerate(children):
            if row is None:
                sent = (index,'child_create',child_index) in self._stage_started
                children[child_index] = {'status':'unknown' if sent else 'unsent',
                    'unresolved_external':int(sent),'reason':'prior_stage_unresolved'}
                await self._notify_child(child_index,children[child_index])
        completed_at = self.clock()
        for child_index, row in enumerate(children):
            if row['status'] == 'success':
                handle = handles[child_index]
                validation_handle = {**handle, 'source_sandbox_id':source_handle['sandbox_id'],
                    'sibling_ids':[h['sandbox_id'] for i,h in handles.items() if i != child_index]}
                row['validation'] = await self._fixture_assertion('clone_isolation',validation_handle)
        unresolved = sum(row['unresolved_external'] for row in children)
        status = 'success' if all(row['status']=='success' for row in children) else 'unknown' if unresolved else 'failed'
        if source_result is not None and source_result.status != 'success':status='failed'
        return OperationResult(status, unresolved, value={
            'method_id':self.case.method_id, 'source':source, 'children':children,'stage_error':stage_error,
            'completion':{'status':status,'submitted_at':began,'observed_at':completed_at,
                          'latency':completed_at-began if status == 'success' else None,
                          'process_epoch':self.process_epoch},
            'parent_http_requests':sum((index,'child_create',i) in self._stage_started for i in range(self.case.clone_count)),
            'validation_interference':True,
            'source_recovery':asdict(source_result) if source_result else None,
            'source_residual':({'sandbox_id':source_handle['sandbox_id'],'status':source_result.status,
                                'reason':source_result.reason} if source_result and source_result.status!='success' else None)})

    async def _native_fork(self, index: int, began: float) -> OperationResult:
        source = self._target(index, shared=True)
        rows, child_tasks, children = [], [], []
        request_error = None
        async def child(child_index,row):
            if 'error' in row:
                value = {'status': 'failed', 'error': row['error'], 'unresolved_external': 0}
                await self._notify_child(child_index,value)
                return value
            handle = row['sandbox']
            await self._own('sandbox', handle['sandbox_id'], handle, index=index)
            result = await self._complete(handle, began)
            value = {'status': result.status, 'sandbox_id': handle['sandbox_id'],
                     'completion': asdict(result), 'unresolved_external': result.unresolved_external}
            await self._notify_child(child_index,value)
            return value
        try:
            rows = await self._call(self.adapter.fork, source, self.case.clone_count, self._remaining(began))
            if not isinstance(rows, list) or len(rows) != self.case.clone_count:
                raise ValueError('fork response does not cover reserved children')
            child_tasks = [asyncio.create_task(child(i,row)) for i,row in enumerate(rows)]
            results = await asyncio.gather(*child_tasks,return_exceptions=True)
            for child_index,result in enumerate(results):
                if isinstance(result,BaseException):
                    result={'status':'unknown','unresolved_external':1,'error_type':type(result).__name__}
                children.append(result)
        except Exception as error:
            request_error = type(error).__name__
            children=[{'status':'unknown','unresolved_external':1,'reason':'fork_response_unknown'}
                      for _ in range(self.case.clone_count)]
            for child_index,result in enumerate(children):
                await self._notify_child(child_index,result)
        finally:
            for task in child_tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*child_tasks,return_exceptions=True)
            source_result = await self._restore_source(index, source, began)
        unresolved = sum(row['unresolved_external'] for row in children)
        completed_at = self.clock()
        status = 'success' if all(row['status'] == 'success' for row in children) else 'unknown' if unresolved else 'failed'
        if source_result is not None and source_result.status != 'success':status = 'failed'
        if self.fixture_validator:
            for child_result, row in zip(children,rows if isinstance(rows,list) else []):
                if child_result['status']=='success':
                    validation_handle={**row['sandbox'],'source_sandbox_id':source['sandbox_id'],
                        'sibling_ids':[item['sandbox']['sandbox_id'] for item in rows
                                       if 'sandbox' in item and item['sandbox']['sandbox_id']!=row['sandbox']['sandbox_id']]}
                    child_result['validation']=await self._fixture_assertion('clone_isolation',validation_handle)
        return OperationResult(status, unresolved, value={'method_id': self.case.method_id,
            'children': children, 'parent_completed_at': completed_at,'request_error':request_error,
            'completion':{'status':status,'submitted_at':began,'observed_at':completed_at,
                          'latency':completed_at-began if status=='success' else None,
                          'process_epoch':self.process_epoch},
            'validation_interference':self.fixture_validator is not None,
            'source_recovery': asdict(source_result) if source_result else None,
            'source_residual':({'sandbox_id':source['sandbox_id'],'status':source_result.status,
                                'reason':source_result.reason} if source_result and source_result.status!='success' else None)})

    async def _snapshot(self, index: int, began: float) -> OperationResult:
        handle = self._target(index, shared=True)
        if self.case.product == 'kuasar':
            await self._call(self.adapter.pause, handle, self._remaining(began), memory=True)
            paused = await self._complete(handle, began, target='paused', kind='STATE_READY')
            if paused.status != 'success':
                return OperationResult(paused.status, paused.unresolved_external, value={'pause': asdict(paused)})
            source = await self._call(self.adapter.export_snapshot, handle, self._remaining(began))
        else:
            source = await self._call(self.adapter.snapshot, handle, self._remaining(began))
        await self._own('source', source['source_id'], source, index=index)
        if self.case.method_id == 'B08.PUBLISHED':
            async def inspect(source):
                row = await self.source_inspector(source)
                return StateObservation(row.get('state', 'unknown'), self.process_epoch,
                                        row.get('raw_ref'), row.get('source_event_time'))
            result = await self.observer.observe(self._spec(target='published', kind='STATE_READY'),
                submitted_at=began, process_epoch=self.process_epoch,
                accepted=Acceptance(True,self.clock(),self.process_epoch,source), inspect=inspect)
        else:
            result = await self.observer.observe(self._spec(kind='API_COMPLETE'), submitted_at=began,
                process_epoch=self.process_epoch, accepted=Acceptance(True,self.clock(),self.process_epoch,source))
        await self._notify_child(0,{'status':result.status,'unresolved_external':result.unresolved_external,
            'completion':asdict(result),'source':source})
        validation = await self._fixture_assertion('snapshot_restore',
            {**source,'source_sandbox_id':handle['sandbox_id'],'request_index':index}) if result.status == 'success' else None
        return OperationResult(result.status, result.unresolved_external,
                               value={'source': source, 'completion':asdict(result), 'validation':validation,
                                      'validation_interference':validation is not None})

    async def validate(self) -> list[dict]:
        pending, self._pending_validation = self._pending_validation, []
        results = []
        for index, handle in pending:
            try:
                result = await self._command_assertion(handle, self.clock())
            except Exception as error:
                result = {'success':False,'reason':'validation_error','error_type':type(error).__name__}
            results.append({'request_index':index, 'sandbox_id':handle['sandbox_id'], **result})
        return results

    async def cleanup(self) -> list[dict]:
        """Delete only resources created here; a DELETE ACK is not physical release."""
        deadline, results = self.clock() + self.case.cleanup_timeout_s, []
        for row in reversed(self.owned):
            remaining = deadline - self.clock()
            if remaining <= 0:
                results.append({'resource_id':row['resource_id'],'status':'residual','reason':'cleanup_deadline'})
                continue
            # Shared sources require refcounts/ownership proof from runner cleanup.
            if row['kind'] != 'sandbox':
                results.append({'resource_id':row['resource_id'],'status':'residual','reason':'source_reference_check_required'})
                continue
            try:
                await self._call(self.adapter.delete,row['handle'],remaining)
                results.append({'resource_id':row['resource_id'],'status':'api_deleted','physical_release':'unverified'})
            except Exception as error:
                results.append({'resource_id':row['resource_id'],'status':'residual','reason':type(error).__name__})
        return results
