"""P2.4 / ORC-002,008: GraphDefinition validation rejects unsafe or unknown graphs."""
import pytest

from sandbox_graph.graphs.factory import GraphDefinition, validate_definition

HANDLERS = {'a': object(), 'b': object(), 'c': object()}


def definition(**extra):
    data = dict(graph_id='g', version='1', nodes=('a', 'b'), entry='a',
                edges={'a': ('b',), 'b': ('__end__',)})
    data.update(extra)
    return GraphDefinition(**data)


def test_valid_definition_with_registered_handlers():
    assert validate_definition(definition(), HANDLERS).entry == 'a'


def test_unknown_handler_duplicate_node_or_missing_entry_are_rejected():
    with pytest.raises(ValueError, match='unregistered handler'):
        validate_definition(definition(), {'a': object()})
    with pytest.raises(ValueError):
        validate_definition(definition(nodes=('a', 'a'), edges={'a': ('__end__',)}), HANDLERS)
    with pytest.raises(ValueError):
        validate_definition(definition(entry='missing'), HANDLERS)


def test_fixed_and_dynamic_route_overlap_is_rejected():
    with pytest.raises(ValueError, match='overlap'):
        validate_definition(definition(dynamic_routes={'a': ('b', '__end__')}), HANDLERS)


def test_unbounded_cycle_and_unreachable_node_are_rejected():
    cyclic = GraphDefinition(graph_id='g', version='1', nodes=('a',), entry='a', edges={},
                             dynamic_routes={'a': ('a', '__end__')})
    with pytest.raises(ValueError, match='bounded'):
        validate_definition(cyclic, HANDLERS)
    unreachable = GraphDefinition(graph_id='g', version='1', nodes=('a', 'b'), entry='a',
                                  edges={'a': ('__end__',), 'b': ('__end__',)})
    with pytest.raises(ValueError, match='unreachable'):
        validate_definition(unreachable, HANDLERS)


def test_removed_or_bypassable_required_cleanup_is_rejected():
    removed = GraphDefinition(graph_id='g', version='1', nodes=('a',), entry='a',
                              edges={'a': ('__end__',)}, required_cleanup=('cleanup',))
    with pytest.raises(ValueError, match='required cleanup'):
        validate_definition(removed, HANDLERS)
    bypass = GraphDefinition(graph_id='g', version='1', nodes=('a', 'b', 'c'), entry='a',
                             edges={'a': ('b', 'c'), 'b': ('__end__',), 'c': ('__end__',)},
                             required_cleanup=('c',))
    with pytest.raises(ValueError, match='bypass'):
        validate_definition(bypass, HANDLERS)
