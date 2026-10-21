"""BCT-09-BUILD-LOST / BCT-09-COST：B00 构建重入的独立集成检查。

独立 marker `b00_build`，不复用 DB/fault fixture，不触网。
"""
import json
import pytest

from sandbox_bench.adapters import Adapter
from sandbox_bench.scenarios_build import BuildScenario, TargetBindings
from tests.benchmark.scenarios.test_scenarios import case

pytestmark = pytest.mark.b00_build


def build_case():
    return case('B00.TEMPLATE_BUILD', source={'kind': 'template', 'id': 'builder-1',
                                              'digest': 'sha256:builder',
                                              'artifact_kind': 'sandbox_artifact',
                                              'launch_path': 'cold_boot', 'producer_step': 'build_job'})


class ReentryAdapter:
    """每次进入都返回"写响应丢失"，并记录调用方传入的 handle。"""

    def __init__(self):
        self.handles = []

    def build_source(self, run_id, timeout, *, handle=None, on_progress=None):
        self.handles.append(handle)
        if on_progress is not None:
            on_progress({'phase': 'intent', 'stage': 'build_register'})
        return {'artifact': None, 'terminal': 'unknown', 'unresolved_external': 1,
                'reason': 'build_trigger_unconfirmed', 'handle': handle or {'triggered': False}}


def test_real_adapter_refuses_to_replay_unconfirmed_trigger():
    adapter = Adapter({'product': 'kuasar', 'api_url': 'http://localhost:1',
                       'data_url': 'http://localhost:1', 'product_version': 'sha'})
    outcome = adapter.build_source('work', 5, handle={'registration_id': 'r1', 'build_id': None,
                                                      'triggered': False})
    assert outcome['terminal'] == 'unknown' and outcome['unresolved_external'] == 1
    assert outcome['reason'] == 'build_trigger_unconfirmed'


@pytest.mark.asyncio
async def test_reentry_passes_persisted_handle_and_never_claims_ready():
    journal = []
    adapter = ReentryAdapter()
    scenario = BuildScenario(build_case(), adapter, on_progress=journal.append)
    first = await scenario.execute(0)
    assert first.status == 'unknown' and adapter.handles == [None]
    assert journal and journal[0]['stage'] == 'build_register'
    persisted = {'registration_id': 'r1', 'build_id': None, 'triggered': False}
    second = await scenario.execute(0, handle=persisted)
    assert second.status == 'unknown' and adapter.handles[-1] == persisted
    assert second.value['build']['terminal'] == 'unknown'


def test_build_cost_charged_once_when_build_is_retried(tmp_path):
    ledger = TargetBindings(tmp_path / 'target_bindings.json')
    artifact = {'kind': 'image_artifact', 'id': 'e2b-img-built', 'digest': None, 'size_bytes': None,
                'producer_ref': 'build:digest-a'}
    ledger.record(artifact, case_ref='case-1', producer_step='build_job', cost_scope='build_included')
    retried = ledger.record(artifact, case_ref='case-1', producer_step='build_job',
                            cost_scope='build_included')
    assert retried['cost_charged'] is True and retried['charged_this_call'] is False
    assert 'reuse_of' not in retried and retried['cases'] == ['case-1']
    assert json.loads((tmp_path / 'target_bindings.json').read_text())['artifacts'][
        'e2b-img-built']['cost_charged'] is True
