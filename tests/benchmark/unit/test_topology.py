"""Topology-aware component catalog oracle; literal expectations, no self-reference."""
import pytest
from pydantic import ValidationError

from sandbox_bench.topology import (Topology, component_catalog, expected_component_ids,
                                    inventory_plan, match_process, planned_components)


def test_kuasar_multi_node_splits_management_and_data_components():
    topology = Topology(product='kuasar', nodes={'mgmt': ('management',), 'node1': ('data',)})
    assert expected_component_ids(topology, 'mgmt', component_class='management_plane') == (
        'kuasar.registry', 'kuasar.router', 'kuasar.placer')
    assert expected_component_ids(topology, 'mgmt', component_class='data_plane_management') == ()
    data = set(expected_component_ids(topology, 'node1', component_class='sandbox_runtime'))
    assert data == {'kuasar.run-sandbox', 'kuasar.sandbox-ctl', 'kuasar.cloud-hypervisor'}
    assert 'kuasar.registry' not in expected_component_ids(topology, 'node1')
    assert topology.has_management_plane is True


def test_single_node_co_locates_management_and_data_without_duplication():
    topology = Topology(product='kuasar', nodes={'all': ('management', 'data')})
    ids = expected_component_ids(topology, 'all')
    assert len(ids) == len(set(ids))  # a co-located role never duplicates a component
    assert 'kuasar.registry' in ids and 'kuasar.cloud-hypervisor' in ids


def test_cube_and_agentenv_sandbox_runtime_processes():
    cube = Topology(product='cubesandbox', nodes={'node1': ('data',)})
    assert set(expected_component_ids(cube, 'node1', component_class='sandbox_runtime')) == {
        'cubesandbox.cube-runtime', 'cubesandbox.cube-hypervisor'}
    cube_mgmt = Topology(product='cubesandbox', nodes={'mgmt': ('management',)})
    assert 'cubesandbox.cube-api' in expected_component_ids(cube_mgmt, 'mgmt')
    agentenv = Topology(product='agentenv', nodes={'node1': ('data',)})
    assert expected_component_ids(agentenv, 'node1', component_class='sandbox_runtime') == ('agentenv.firecracker',)


def test_shared_binary_is_only_resolved_with_an_explicit_argv_marker():
    assert match_process('kuasar', 'node-ctl', ('node-ctl', 'conductor')) == 'kuasar.node-conductor'
    assert match_process('kuasar', 'node-ctl', ('node-ctl', 'proxy')) == 'kuasar.node-proxy'
    assert match_process('kuasar', 'node-ctl') is None  # ambiguous -> unresolved
    assert match_process('kuasar', 'cloud-hypervisor') == 'kuasar.cloud-hypervisor'
    assert match_process('agentenv', 'firecracker') == 'agentenv.firecracker'
    # In-guest envd is never a host sandbox process.
    assert match_process('agentenv', 'envd') is None


def test_inventory_plan_marks_per_instance_shared_and_in_guest():
    topology = Topology(product='cubesandbox', nodes={'node1': ('data',)})
    plan = inventory_plan(topology)
    node = plan['nodes']['node1']
    assert 'cubesandbox.cube-runtime' in node['per_instance']
    assert 'cubesandbox.cube-egress' in node['shared_auxiliary']
    assert set(plan['in_guest_excluded']) == {'guest-init', 'envd'}
    assert plan['management_plane'] is False


def test_topology_validation_and_catalog_is_closed():
    with pytest.raises(ValidationError):
        Topology(product='kuasar', nodes={})
    with pytest.raises(ValidationError):
        Topology(product='kuasar', nodes={'n': ('control',)})
    with pytest.raises(ValueError):
        component_catalog('nope')
    # Every declared component is host-side and reachable from at least one role.
    for product in ('kuasar', 'cubesandbox', 'agentenv'):
        for component in component_catalog(product):
            assert component.node_roles and not component.guest_side
    assert planned_components(Topology(product='kuasar', nodes={'n': ('data',)}), 'n')


def test_build_inventory_attributes_declared_components_and_isolates_unknown():
    from sandbox_bench.topology import build_inventory
    topology = Topology(product='kuasar', nodes={'node1': ('data',)})
    identity = lambda pid: {'environment': 'e', 'node': 'node1', 'boot_id': 'b', 'pid': pid, 'start_ticks': 1}
    discovered = [
        {'identity': identity(10), 'comm': 'node-ctl', 'argv': ['node-ctl', 'conductor']},
        {'identity': identity(11), 'comm': 'node-ctl', 'argv': ['node-ctl', 'proxy']},
        {'identity': identity(12), 'comm': 'cloud-hypervisor', 'sandbox_id': 'sb1', 'generation': 'g1'},
        {'identity': identity(13), 'comm': 'unknown-daemon'},
        {'identity': identity(14), 'comm': 'sandbox-ctl'},  # per-instance without sandbox id
    ]
    result = build_inventory(topology, 'node1', discovered)
    assert {row['components'][0] for row in result['rows']} == {
        'kuasar.node-conductor', 'kuasar.node-proxy', 'kuasar.cloud-hypervisor'}
    assert len(result['unresolved']) == 2
    assert all(row['components'] for row in result['rows'])  # every kept row is owner-bearing
    assert result['status'] == 'partial'
