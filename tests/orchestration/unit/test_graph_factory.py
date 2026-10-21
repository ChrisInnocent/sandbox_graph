"""P7.1 / ORC-002,027,070: real graphs compile only from registered handlers."""
import pytest

from sandbox_graph.graphs.factory import GraphDefinition, build_definition, build_work


class FakeExecutor:
    def __init__(self, handlers):
        self.handlers = handlers


def test_build_work_requires_every_work_handler():
    with pytest.raises(ValueError, match='unregistered work handler'):
        build_work(FakeExecutor({f'N{i:02}': object() for i in range(5)}))


def test_build_work_compiles_with_the_full_handler_set():
    graph = build_work(FakeExecutor({f'N{i:02}': object() for i in range(11)}))
    assert graph is not None


def test_build_definition_rejects_an_unregistered_handler():
    definition = GraphDefinition(graph_id='g', version='1', nodes=('a',), entry='a',
                                 edges={'a': ('__end__',)})
    with pytest.raises(ValueError, match='unregistered handler'):
        build_definition(definition, FakeExecutor({}))
