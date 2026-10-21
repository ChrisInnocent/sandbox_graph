"""BCD-12 §12.9/§12.10 / BCT-12-BGDENSITY,BUILDMETRIC: build background density + metrics."""
import pytest

from sandbox_bench.contracts import BackgroundConditions
from sandbox_bench.scenarios_build import BuildScenario
from tests.benchmark.scenarios.test_scenarios import case


class FakeAdapter:
    def __init__(self, *, fail_create=False):
        self.created = []
        self.fail_create = fail_create
        self.built_with = None

    def create(self, run_id, timeout):
        if self.fail_create:
            raise RuntimeError('create failed')
        self.created.append(run_id)
        return {'sandbox_id': f'sb-{len(self.created)}', 'private': {}}

    def build_source(self, run_id, timeout, *, handle=None, on_progress=None):
        self.built_with = list(self.created)
        return {'artifact': {'kind': 'image_artifact', 'id': 'tpl-1', 'digest': 'd', 'size_bytes': 123},
                'terminal': 'ready', 'unresolved_external': 0, 'reason': None,
                'handle': {'registration_id': 'r', 'build_id': 'b', 'triggered': True},
                'build_elapsed_s': 2.0, 'cache_state': 'cold',
                'resource_peak': {'pss_bytes': 1000, 'cpu_seconds': 5}}


def build_case(**extra):
    return case('B00.TEMPLATE_BUILD', **extra)


async def test_background_density_is_prepared_then_build_runs_with_it():
    contract = build_case(background=BackgroundConditions(background_count=3))
    adapter = FakeAdapter()
    result = await BuildScenario(contract, adapter, on_progress=lambda row: None).execute(0)
    assert result.status == 'success'
    assert len(adapter.created) == 3
    assert adapter.built_with == adapter.created          # build ran with the background present
    background = result.value['build']['background']
    assert background['requested'] == 3 and background['actual'] == 3


async def test_background_prepare_failure_is_blocked_not_silent():
    contract = build_case(background=BackgroundConditions(background_count=2))
    result = await BuildScenario(contract, FakeAdapter(fail_create=True),
                                 on_progress=lambda row: None).execute(0)
    assert result.reason == 'background_prepare_failed'
    assert result.value['execution'] == 'blocked'


async def test_build_metrics_are_surfaced_not_placeholder():
    result = await BuildScenario(build_case(), FakeAdapter(), on_progress=lambda row: None).execute(0)
    build = result.value['build']
    assert build['artifact']['size_bytes'] == 123
    assert build['cache_state'] == 'cold'
    assert build['resource_peak'] == {'pss_bytes': 1000, 'cpu_seconds': 5}


async def test_background_incremental_resource_is_total_minus_baseline():
    contract = build_case(background=BackgroundConditions(background_count=2))
    samples = iter([{'pss_bytes': 100, 'cpu_seconds': 1}, {'pss_bytes': 900, 'cpu_seconds': 9}])
    result = await BuildScenario(contract, FakeAdapter(), on_progress=lambda row: None,
                                 resource_probe=lambda: next(samples)).execute(0)
    incremental = result.value['build']['background']['incremental_resource']
    assert incremental == {'pss_bytes': 800, 'cpu_seconds': 8}
