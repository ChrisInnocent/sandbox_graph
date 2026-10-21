"""B00 构建场景阶段机（BCD-12 §12.4／§12.5／§12.8）。

阶段：intent → register(receipt) → trigger(receipt) → poll(building)* → terminal{ready|error}。
调用方必须先通过 `on_progress` 持久化 handle，再继续；未确认 trigger 的 handle 不得重放。
本模块扁平放置（`scenarios.py` 是模块，不建 `scenarios/` 包）。
"""
from __future__ import annotations

import asyncio
import inspect as python_inspect
import json
from pathlib import Path
import time
from typing import Any, Callable

from .contracts import CaseContract
from .load import OperationResult
from .scenarios import ScenarioBlocked


class BuildScenario:
    """单个 B00 构建／捕获场景，复用 ScenarioExecutor 的适配协议与预算语义。"""

    def __init__(self, case: CaseContract, adapter: Any, *,
                 on_progress: Callable[[dict], None] | None = None,
                 stage_journal: Callable[[dict], Any] | None = None,
                 resource_probe: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        self.case, self.adapter = case, adapter
        self.on_progress, self.stage_journal = on_progress, stage_journal
        self.resource_probe = resource_probe
        self.clock, self.sleep = clock, sleep

    def _background_count(self) -> int:
        background = getattr(self.case, 'background', None)
        return int(getattr(background, 'background_count', 0) or 0)

    async def _prepare_background(self, timeout) -> list:
        created = []
        for index in range(self._background_count()):
            handle = await self._call(self.adapter.create, f'{self.case.work_id}:bg:{index}', timeout)
            created.append(handle.get('sandbox_id'))
        return created

    @staticmethod
    def _increment(baseline: dict, peak: dict) -> dict:
        def delta(key):
            low, high = baseline.get(key), peak.get(key)
            return (high - low) if isinstance(low, (int, float)) and isinstance(high, (int, float)) else None
        return {'pss_bytes': delta('pss_bytes'), 'cpu_seconds': delta('cpu_seconds')}

    async def _call(self, function: Callable, *args, **kwargs):
        if python_inspect.iscoroutinefunction(function):
            return await function(*args, **kwargs)
        result = await asyncio.to_thread(function, *args, **kwargs)
        if python_inspect.isawaitable(result):
            return await result
        return result

    def preflight(self, index: int = 0) -> None:
        if not self.case.method_id.startswith('B00.'):
            raise ScenarioBlocked('build_scenario_requires_b00_method')
        if self.on_progress is None:
            raise ScenarioBlocked('durable_build_progress_callback_required')

    async def execute(self, index: int, *, handle: dict | None = None) -> OperationResult:
        try:
            self.preflight(index)
        except ScenarioBlocked as error:
            return OperationResult('unsent', reason=str(error), value={'execution': 'blocked'})
        requested = self._background_count()
        baseline = self.resource_probe() if self.resource_probe else None
        try:
            created = await self._prepare_background(self.case.completion.timeout_s)
        except Exception as error:
            return OperationResult('unsent', reason='background_prepare_failed',
                                   value={'execution': 'blocked', 'error_type': type(error).__name__,
                                          'background': {'requested': requested, 'actual': 0, 'created_ids': []}})
        try:
            built = await self._call(self.adapter.build_source, self.case.work_id,
                                     self.case.completion.timeout_s, handle=handle,
                                     on_progress=lambda row: self._progress(index, row))
        except (TimeoutError, asyncio.TimeoutError):
            built = {'artifact': None, 'terminal': 'error', 'unresolved_external': 0,
                     'reason': 'build_timeout'}
        peak = self.resource_probe() if self.resource_probe else None
        background = {'requested': requested, 'actual': len(created), 'created_ids': created}
        if self.resource_probe and baseline is not None and peak is not None:
            background['incremental_resource'] = self._increment(baseline, peak)
        built['background'] = background
        value = {'method_id': self.case.method_id, 'request_index': index, 'build': built}
        if built.get('terminal') == 'ready':
            return OperationResult('success', int(built.get('unresolved_external') or 0), value=value)
        if built.get('unresolved_external'):
            return OperationResult('unknown', int(built['unresolved_external']),
                                   reason=built.get('reason') or 'build_unresolved_external', value=value)
        return OperationResult('failed', 0, reason=built.get('reason') or 'build_terminal_error',
                               value=value)

    def _progress(self, index: int, row: dict) -> None:
        # 先持久化再继续：中间件必须在 I/O 之前写入 intent、收到回执后写入 receipt。
        self.on_progress({**row, 'request_index': index})


class TargetBindings:
    """产物身份账本 `target_bindings.json`（BCD-12 §12.6）。

    记录 `{artifact_kind, artifact_id, artifact_digest, producer_step, producer_ref,
    cache_state, size_bytes}`；同一 `producer_ref` 的构建成本只计一次，后续记 `reuse_of`。
    """

    def __init__(self, path):
        self.path = Path(path)

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {'schema_version': 'target_bindings.v1', 'artifacts': {}}

    def record(self, artifact: dict, *, case_ref: str, producer_step: str = 'pre_existing',
               cost_scope: str = 'prepared', cache_state: str = 'unknown') -> dict:
        document = self._load()
        producer_ref = artifact.get('producer_ref')
        prior = document['artifacts'].get(artifact['id'])
        entry = {'artifact_kind': artifact['kind'], 'artifact_id': artifact['id'],
                 'artifact_digest': artifact.get('digest'), 'producer_step': producer_step,
                 'producer_ref': producer_ref, 'cache_state': cache_state,
                 'size_bytes': artifact.get('size_bytes'),
                 'cases': sorted(set((prior or {}).get('cases', []) + [case_ref]))}
        charged_this_call = False
        if prior is not None:
            # 同一产物重录：保留首次计费事实，本次不再计费。
            entry['cost_charged'] = bool(prior.get('cost_charged'))
            if 'reuse_of' in prior:
                entry['reuse_of'] = prior['reuse_of']
        elif cost_scope == 'build_included':
            reuse_of = None
            if producer_ref:
                for existing in document['artifacts'].values():
                    if existing.get('producer_ref') == producer_ref and existing.get('cost_charged'):
                        reuse_of = existing['artifact_id']
                        break
            if reuse_of is not None:
                entry['cost_charged'] = False
                entry['reuse_of'] = reuse_of
            else:
                entry['cost_charged'] = True
                charged_this_call = True
        else:
            entry['cost_charged'] = False
        entry['charged_this_call'] = charged_this_call
        document['artifacts'][artifact['id']] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(document, sort_keys=True, allow_nan=False))
        return entry
