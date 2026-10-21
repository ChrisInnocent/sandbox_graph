"""ORC-071–073 / I13 / I14：图侧来源与端点身份绑定（D17）。

oracle 为显式字面量，不调用被测归约/路由函数生成期望值。
"""
import pytest
from pydantic import ValidationError

from sandbox_graph.planning.changes import diff
from sandbox_graph.planning.compiler import Case, PlanCompiler, Selection
from sandbox_graph.planning.identity import (BLACKBOX_ENDPOINTS, binding_payload,
                                             comparability_key, condition_identity,
                                             is_whitebox, missing_required_identity)
from sandbox_graph.catalog.registry import standard_registry


def case(**extra):
    data = dict(case_id='c', group_id='G_A', sandbox_type='x', scenario_id='B01',
                method_id='B01.TEMPLATE', priority='base', conditions=({'size': 1},),
                artifact_kind='sandbox_artifact', launch_path='cold_boot',
                producer_step='pre_existing', cost_scope='prepared')
    data.update(extra)
    return Case(**data)


# --- ORC-071：端点身份矛盾与缺失在计划期即失败 --------------------------------

@pytest.mark.parametrize('extra', [
    {'topology': 'co_located_cli', 'endpoint_kind': 'api_complete', 'readiness_signal': 'none'},
    {'topology': 'node_direct', 'endpoint_kind': 'guest_ready', 'readiness_signal': 'ready_fd'},
    {'endpoint_kind': 'guest_ready', 'readiness_signal': 'none'},
    {'method_id': 'B01.SNAPSHOT.MEMORY', 'launch_path': 'cold_boot'},
    {'method_id': 'B01.TEMPLATE', 'artifact_kind': 'snapshot_manifest'},
])
def test_endpoint_identity_contradictions_are_rejected(extra):
    with pytest.raises(ValidationError):
        case(**extra)


def test_remote_topology_rejects_ready_fd_and_whitebox_endpoint():
    with pytest.raises(ValidationError):
        case(topology='router', endpoint_kind='guest_ready', readiness_signal='ready_fd')


# --- I14：缺失身份被报告，不被名称或默认值填充 --------------------------------

def test_missing_identity_is_reported_not_defaulted():
    incomplete = case(artifact_kind=None, launch_path=None, producer_step=None)
    assert set(missing_required_identity(incomplete)) == {'artifact_kind', 'launch_path', 'producer_step'}
    non_creation = Case(case_id='e', group_id='G_A', sandbox_type='x', scenario_id='B04',
                        method_id='B04.MEMORY', priority='base', conditions=({'size': 1},))
    assert missing_required_identity(non_creation) == ()


# --- ORC-073：来源与端点身份分离工作实例，不合并 -------------------------------

def test_source_variants_produce_distinct_condition_identity():
    image = case(artifact_kind='image_artifact', launch_path='cold_boot')
    sandbox = case(artifact_kind='sandbox_artifact', launch_path='cold_boot')
    assert condition_identity(image, {'size': 1}) != condition_identity(sandbox, {'size': 1})
    disk = Case(case_id='d', group_id='G_A', sandbox_type='x', scenario_id='B01',
                method_id='B01.SNAPSHOT.DISK', priority='base', conditions=({'size': 1},),
                artifact_kind='snapshot_manifest', launch_path='disk_restore', producer_step='pre_existing')
    memory = Case(case_id='m', group_id='G_A', sandbox_type='x', scenario_id='B01',
                  method_id='B01.SNAPSHOT.MEMORY', priority='base', conditions=({'size': 1},),
                  artifact_kind='snapshot_manifest', launch_path='memory_restore', producer_step='pre_existing')
    assert disk.method_id != memory.method_id
    assert condition_identity(disk, {'size': 1}) != condition_identity(memory, {'size': 1})


def test_plan_hash_separates_source_identity():
    left = PlanCompiler([case(case_id='a', artifact_kind='image_artifact', launch_path='cold_boot')]).compile(
        Selection(), authorized_groups={'G_A'})
    right = PlanCompiler([case(case_id='a', artifact_kind='sandbox_artifact', launch_path='cold_boot')]).compile(
        Selection(), authorized_groups={'G_A'})
    assert left.hash != right.hash


# --- ORC-072 / I13：白盒与黑盒端点不可并入同一比较键 ---------------------------

def test_whitebox_and_blackbox_have_distinct_comparability_keys():
    black = case(topology='node_direct', endpoint_kind='state_ready', readiness_signal='poll_state')
    white = case(topology='co_located_cli', endpoint_kind='guest_ready', readiness_signal='ready_fd')
    assert comparability_key(black) != comparability_key(white)
    assert is_whitebox(white) and not is_whitebox(black)
    assert 'guest_ready' not in BLACKBOX_ENDPOINTS


def test_binding_payload_carries_source_and_endpoint_identity():
    payload = binding_payload(case(topology='router', endpoint_kind='api_complete',
                                   readiness_signal='none', endpoint_ref='ref-1'))
    assert payload['endpoint_ref'] == 'ref-1'
    assert payload['source_identity']['artifact_kind'] == 'sandbox_artifact'
    assert payload['endpoint_identity']['topology'] == 'router'


# --- 选测轴与变更影响 -----------------------------------------------------------

def test_selection_filters_by_topology_axis():
    router = case(case_id='a', topology='router', endpoint_kind='api_complete', readiness_signal='none')
    node = case(case_id='b', topology='node_direct', endpoint_kind='api_complete', readiness_signal='poll_state')
    plan = PlanCompiler([router, node]).compile(Selection(topologies=('router',)), authorized_groups={'G_A'})
    assert [c.case_id for c in plan.cases] == ['a']


def test_identity_change_forces_new_measurement():
    plan = diff({'artifact_kind': 'sandbox_artifact'}, {'artifact_kind': 'snapshot_manifest'})
    assert plan.requires_measurement is True
    assert 'identity' in plan.reason
    assert diff({'wording': 'a'}, {'wording': 'b'}).requires_measurement is False


def test_registry_exposes_identity_dimensions_and_unverified_capability():
    registry = standard_registry()
    assert 'artifact_kind' in registry.resolve_method('B01.TEMPLATE')['identity_dimensions']
    assert 'endpoint_kind' in registry.resolve_method('B01.TEMPLATE')['endpoint_dimensions']
    assert registry.capability('B01.TEMPLATE') == 'unverified'
    with pytest.raises(KeyError):
        registry.resolve_method('NOPE')
