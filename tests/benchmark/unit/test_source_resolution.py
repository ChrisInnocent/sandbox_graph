"""BCT-09-SRC / BCT-09-ROUTE / BCT-09-REJECT：来源身份解析与方法路由的独立 oracle。

oracle 为手写字面量（下表），不调用被测函数生成期望值。
"""
import pytest
from pydantic import ValidationError

from sandbox_bench.adapters import Adapter
from sandbox_bench.registry import METHODS
from sandbox_bench.scenarios import ScenarioBlocked
from sandbox_bench.source_resolution import (Resolution, kuasar_template_kind, method_for_source,
                                             resolve_source, source_from_resolution)
from tests.benchmark.scenarios.test_scenarios import case


def resolution(artifact_kind, launch_path, producer_step='pre_existing', *,
               artifact_id='artifact-1', status='resolved'):
    return Resolution(artifact_kind, launch_path, producer_step, artifact_id, None, None,
                      'request', 'evidence-ref', status)


# --- BCT-09-SRC：只读解析，NaN 级禁止名称推断 -------------------------------

def test_src_kuasar_marker_table_is_literal():
    table = {
        'e2b-img-abc': ('image_artifact', 'cold_boot'),
        'e2b-sbx-abc': ('sandbox_artifact', 'cold_boot'),
        'e2b-snp-abc': ('snapshot_manifest', 'memory_restore'),
        'tpl-plain': None,
        '': None,
    }
    for source_id, expected in table.items():
        assert kuasar_template_kind(source_id) == expected


def test_src_agentenv_template_is_inherited_memory_snapshot():
    resolved = resolve_source(product='agentenv', product_version='sha', parameter_name='template_id',
                              declared_kind='template', source_id='snap-1', digest=None,
                              probe=None, evidence_ref='docs/source-audit/agentenv.md')
    assert (resolved.artifact_kind, resolved.launch_path, resolved.spec_origin,
            resolved.status) == ('snapshot_manifest', 'memory_restore', 'inherited', 'resolved')


def test_src_missing_product_version_is_unverified():
    resolved = resolve_source(product='kuasar', product_version='', parameter_name='source',
                              declared_kind='template', source_id='e2b-sbx-1', digest=None,
                              probe=None, evidence_ref='x')
    assert resolved.status == 'unverified' and resolved.artifact_kind is None


def test_src_cubesandbox_name_is_never_guessed():
    source_id = 'tpl-123'
    resolved = resolve_source(product='cubesandbox', product_version='sha', parameter_name='template_id',
                              declared_kind='template', source_id=source_id, digest=None,
                              probe=None, evidence_ref='docs/source-audit/cubesandbox.md')
    assert resolved.status == 'unverified' and resolved.artifact_kind is None


def test_src_adapter_readonly_probe_returns_audited_identity():
    base = {'api_url': 'http://localhost:1', 'data_url': 'http://localhost:1', 'product_version': 'sha'}
    kuasar = Adapter({**base, 'product': 'kuasar'})
    resolved = kuasar.resolve_source({'kind': 'template', 'id': 'e2b-snp-1', 'digest': 'sha256:a'}, 5)
    assert (resolved['artifact_kind'], resolved['launch_path']) == ('snapshot_manifest', 'memory_restore')
    agentenv = Adapter({**base, 'product': 'agentenv'})
    resolved = agentenv.resolve_source({'kind': 'template', 'id': 'snap-1', 'digest': 'sha256:a'}, 5)
    assert resolved['spec_origin'] == 'inherited'
    cubesandbox = Adapter({**base, 'product': 'cubesandbox'})
    resolved = cubesandbox.resolve_source({'kind': 'template', 'id': 'tpl-1', 'digest': 'sha256:a'}, 5)
    assert resolved['resolution_status'] == 'unverified'


# --- BCT-09-ROUTE：决策表字面副本 -------------------------------------------

ROUTE_TABLE = [
    (('image_artifact', 'cold_boot', 'build_job'), 'build_included', None, 'B02.FULL'),
    (('image_artifact', 'cold_boot', 'pre_existing'), 'prepared', 'template', 'B01.TEMPLATE'),
    (('image_artifact', 'cold_boot', 'pre_existing'), 'prepared', 'image', 'B02.PREPARED'),
    (('sandbox_artifact', 'cold_boot', 'pre_existing'), 'prepared', None, 'B01.TEMPLATE'),
    (('snapshot_manifest', 'disk_restore', 'pre_existing'), 'prepared', None, 'B01.SNAPSHOT.DISK'),
    (('snapshot_manifest', 'cold_boot', 'pre_existing'), 'prepared', None, 'B01.SNAPSHOT.DISK'),
    (('snapshot_manifest', 'memory_restore', 'pre_existing'), 'prepared', None, 'B01.SNAPSHOT.MEMORY'),
]


@pytest.mark.parametrize('identity,cost_scope,family,expected', ROUTE_TABLE)
def test_route_decision_table(identity, cost_scope, family, expected):
    assert method_for_source(resolution(*identity), cost_scope=cost_scope, family=family) == expected
    assert expected in METHODS


@pytest.mark.parametrize('artifact_kind,expected', [
    ('image_artifact', 'B00.TEMPLATE_BUILD'),
    ('sandbox_artifact', 'B00.TEMPLATE_BUILD'),
    ('snapshot_manifest', 'B00.SNAPSHOT_BUILD'),
])
def test_route_build_only_identity(artifact_kind, expected):
    launch = 'memory_restore' if artifact_kind == 'snapshot_manifest' else 'cold_boot'
    assert method_for_source(resolution(artifact_kind, launch), cost_scope='build_included',
                             measuring_build=True) == expected


def test_route_family_conflict_and_uncovered_are_blocked():
    ambiguous = resolution('image_artifact', 'cold_boot', 'pre_existing')
    with pytest.raises(ScenarioBlocked):
        method_for_source(ambiguous, cost_scope='prepared', family=None)
    with pytest.raises(ScenarioBlocked):
        method_for_source(resolution('sandbox_artifact', 'memory_restore'), cost_scope='prepared')
    with pytest.raises(ScenarioBlocked):
        method_for_source(resolution(None, None, 'pre_existing', status='unverified'), cost_scope='prepared')


# --- BCT-09-REJECT：非法用例在契约层拒绝 -------------------------------------

def test_reject_b01_template_with_snapshot_manifest():
    with pytest.raises(ValidationError):
        case('B01.TEMPLATE', source={'kind': 'snapshot', 'id': 's', 'digest': 'sha256:a',
                                     'artifact_kind': 'snapshot_manifest', 'launch_path': 'memory_restore',
                                     'producer_step': 'pre_existing'})


def test_reject_snapshot_memory_with_cold_boot():
    with pytest.raises(ValidationError):
        case('B01.SNAPSHOT.MEMORY', source={'kind': 'snapshot', 'id': 's', 'digest': 'sha256:a',
                                            'artifact_kind': 'snapshot_manifest', 'launch_path': 'cold_boot',
                                            'producer_step': 'pre_existing'})


def test_reject_b01_without_identity_dimensions():
    with pytest.raises(ValidationError):
        case('B01.SNAPSHOT.DISK', source={'kind': 'snapshot', 'id': 's', 'digest': 'sha256:a'})


def test_reject_b00_snapshot_build_without_manifest():
    with pytest.raises(ValidationError):
        case('B00.SNAPSHOT_BUILD', source={'kind': 'template', 'id': 't', 'digest': 'sha256:a',
                                           'artifact_kind': 'sandbox_artifact', 'launch_path': 'cold_boot',
                                           'producer_step': 'build_job'})


def test_source_from_resolution_roundtrips_identity():
    rebuilt = source_from_resolution(resolution('snapshot_manifest', 'memory_restore',
                                                artifact_id='e2b-snp-9', status='resolved'),
                                     'B01.SNAPSHOT.MEMORY')
    assert (rebuilt.kind, rebuilt.artifact_kind, rebuilt.launch_path) == (
        'snapshot', 'snapshot_manifest', 'memory_restore')
