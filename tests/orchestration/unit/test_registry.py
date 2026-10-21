"""P2.1 / ORC-002,027,070: the full node catalogue and method bindings are registered."""
import pytest

from sandbox_graph.catalog.registry import METHODS, NODE_GROUPS, standard_registry

EXPECTED_NODES = 45


def test_all_nodes_and_methods_are_registered():
    registry = standard_registry()
    assert len(registry.nodes) == EXPECTED_NODES == sum(len(v) for v in NODE_GROUPS.values())
    for method in METHODS:
        assert registry.resolve_method(method)['method_id'] == method
    assert {'B01.TEMPLATE', 'B10.FIXED_REQUESTS.WINDOW', 'E06'} <= set(METHODS)
    with pytest.raises(KeyError):
        registry.resolve_method('NO.SUCH.METHOD')


def test_conditional_diamonds_do_not_create_extra_work_nodes():
    registry = standard_registry()
    assert set(NODE_GROUPS['work']) == {f'N{i:02}' for i in range(11)}
    assert not any('.' in node for node in registry.nodes)   # no pseudo/agent nodes


def test_node_versions_are_immutable_and_unknown_version_is_rejected():
    registry = standard_registry()
    assert registry.resolve_node('N05').version == '1'
    with pytest.raises(ValueError):
        registry.resolve_node('N05', version='2')
    with pytest.raises(KeyError):
        registry.resolve_node('N99')
