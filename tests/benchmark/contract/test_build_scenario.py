"""BCT-09-BUILD-READY/ERROR/LOST、BCT-09-SNAP、BCT-09-COST 的契约测试。

使用脚本化 adapter（不触网、不建真实沙箱）；oracle 为手写期望值。
"""
import json
import pytest

from sandbox_bench.scenarios import ScenarioBlocked
from sandbox_bench.scenarios_build import BuildScenario, TargetBindings
from sandbox_bench.source_resolution import Resolution, method_for_source
from tests.benchmark.scenarios.test_scenarios import case


def build_case(method='B00.TEMPLATE_BUILD'):
    return case(method, source={'kind': 'template', 'id': 'builder-1', 'digest': 'sha256:builder',
                                'artifact_kind': 'sandbox_artifact', 'launch_path': 'cold_boot',
                                'producer_step': 'build_job'})


class ScriptedBuildAdapter:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def build_source(self, run_id, timeout, *, handle=None, on_progress=None):
        self.calls.append({'run_id': run_id, 'handle': handle})
        if on_progress is not None:
            on_progress({'phase': 'receipt', 'stage': 'build_register'})
        return self.outcome


def ready_outcome(artifact_kind='image_artifact'):
    return {'artifact': {'kind': artifact_kind, 'id': 'e2b-img-built', 'digest': None, 'size_bytes': None},
            'terminal': 'ready', 'unresolved_external': 0, 'reason': None,
            'handle': {'registration_id': 'r1', 'build_id': 'b1', 'triggered': True},
            'build_elapsed_s': 2.0, 'cache_state': 'unknown',
            'resource_peak': {'pss_bytes': None, 'cpu_seconds': None}}


async def run(adapter, method='B00.TEMPLATE_BUILD', *, events=None):
    scenario = BuildScenario(build_case(method), adapter, on_progress=(events if events is not None else []).append)
    return await scenario.execute(0)


@pytest.mark.asyncio
async def test_build_ready_returns_artifact_and_journal_receipt():
    events = []
    result = await run(ScriptedBuildAdapter(ready_outcome()), events=events)
    assert result.status == 'success' and result.unresolved_external == 0
    assert result.value['build']['artifact'] == {'kind': 'image_artifact', 'id': 'e2b-img-built',
                                                 'digest': None, 'size_bytes': None}
    assert events and events[0]['stage'] == 'build_register'


@pytest.mark.asyncio
async def test_build_error_is_failed_without_claiming_ready():
    outcome = {'artifact': None, 'terminal': 'error', 'unresolved_external': 0,
               'reason': 'build_failed', 'handle': {'triggered': True}}
    result = await run(ScriptedBuildAdapter(outcome))
    assert result.status == 'failed' and result.reason == 'build_failed'
    assert result.value['build']['artifact'] is None


@pytest.mark.asyncio
async def test_build_lost_response_is_unknown_and_keeps_unresolved_count():
    outcome = {'artifact': None, 'terminal': 'unknown', 'unresolved_external': 1,
               'reason': 'build_trigger_unconfirmed', 'handle': {'triggered': False}}
    adapter = ScriptedBuildAdapter(outcome)
    result = await run(adapter)
    assert result.status == 'unknown' and result.unresolved_external == 1
    assert result.reason == 'build_trigger_unconfirmed'


@pytest.mark.asyncio
async def test_build_requires_durable_progress_callback():
    adapter = ScriptedBuildAdapter(ready_outcome())
    result = await BuildScenario(build_case(), adapter).execute(0)
    assert result.status == 'unsent' and result.reason == 'durable_build_progress_callback_required'
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_build_rejects_non_b00_method():
    adapter = ScriptedBuildAdapter(ready_outcome())
    scenario = BuildScenario(case('B01.TEMPLATE'), adapter, on_progress=lambda row: None)
    result = await scenario.execute(0)
    assert result.status == 'unsent' and result.reason == 'build_scenario_requires_b00_method'


def test_snap_disk_and_memory_are_distinct_methods():
    disk = method_for_source(Resolution('snapshot_manifest', 'disk_restore', 'pre_existing',
                                        's', None, None, 'request', 'e', 'resolved'), cost_scope='prepared')
    memory = method_for_source(Resolution('snapshot_manifest', 'memory_restore', 'pre_existing',
                                          's', None, None, 'request', 'e', 'resolved'), cost_scope='prepared')
    assert (disk, memory) == ('B01.SNAPSHOT.DISK', 'B01.SNAPSHOT.MEMORY') and disk != memory


def test_cost_is_charged_once_per_producer_ref(tmp_path):
    ledger = TargetBindings(tmp_path / 'target_bindings.json')
    artifact = {'kind': 'image_artifact', 'id': 'e2b-img-1', 'digest': None, 'size_bytes': None,
                'producer_ref': 'build:digest-a'}
    first = ledger.record(artifact, case_ref='case-1', producer_step='build_job', cost_scope='build_included')
    assert first['cost_charged'] is True and first['charged_this_call'] is True and 'reuse_of' not in first
    second = ledger.record({**artifact, 'id': 'e2b-img-2'}, case_ref='case-2',
                           producer_step='build_job', cost_scope='build_included')
    assert second['cost_charged'] is False and second['charged_this_call'] is False
    assert second['reuse_of'] == 'e2b-img-1'
    independent = ledger.record({**artifact, 'id': 'e2b-img-3', 'producer_ref': 'build:digest-b'},
                                case_ref='case-3', producer_step='build_job', cost_scope='build_included')
    assert independent['cost_charged'] is True
    document = json.loads((tmp_path / 'target_bindings.json').read_text())
    assert document['artifacts']['e2b-img-2']['cases'] == ['case-2']


def test_prepared_source_is_never_charged_twice(tmp_path):
    ledger = TargetBindings(tmp_path / 'target_bindings.json')
    artifact = {'kind': 'sandbox_artifact', 'id': 'e2b-sbx-1', 'digest': None, 'size_bytes': None,
                'producer_ref': None}
    entry = ledger.record(artifact, case_ref='case-1', producer_step='pre_existing', cost_scope='prepared')
    assert entry['cost_charged'] is False
