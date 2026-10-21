import asyncio
import pytest
from sandbox_graph.graphs.factory import build_work, GraphDefinition, validate_definition
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.runtime.leases import LeaseService


@pytest.mark.parametrize('failure', ['N02','N03','N04','N05','N06','N07','N08','N09'])
def test_work_failure_always_preserves_evidence_and_reaches_cleanup_delivery(repository,failure):
    token=LeaseService(repository).acquire_thread('t','owner',100)
    visited=[]
    async def node(context):
        visited.append(context.node_id)
        return NodeResult(execution_status='failed' if context.node_id==failure else 'succeeded',cleanup_status='incomplete' if context.node_id==failure=='N09' else 'not_needed')
    handlers={f'N{i:02}':node for i in range(11)}
    executor=NodeExecutor(repository,handlers,token)
    graph=build_work(executor)
    state={'thread_id':'t','scope':'G_A','run_ref':'r','plan_ref':'p','graph_ref':'g','activation_id':'activation','input_refs':{'work':'w'}}
    out=asyncio.run(graph.ainvoke(state))
    assert visited[-2:]==['N09','N10']
    assert out['results'][failure]['execution_status']=='failed'
    if failure in {'N02','N03','N04'}: assert 'N05' not in visited
    assert out['results']['N10']['execution_status']=='succeeded'
    before=visited.copy()
    asyncio.run(graph.ainvoke(state))
    assert visited==before, 'receipt replay must not repeat any action'


def test_unknown_handler_and_unbounded_or_duplicate_routes_rejected():
    with pytest.raises(ValueError,match='handler'):
        validate_definition(GraphDefinition(graph_id='x',version='1',nodes=('a',),entry='a',edges={'a':('__end__',)}),{})
    with pytest.raises(ValueError,match='bounded'):
        validate_definition(GraphDefinition(graph_id='x',version='1',nodes=('a',),entry='a',edges={'a':('a',)}),{'a':object()})
    with pytest.raises(ValueError,match='route'):
        validate_definition(GraphDefinition(graph_id='x',version='1',nodes=('a','b'),entry='a',edges={'a':('b',),'b':('__end__',)},dynamic_routes={'a':('b',)}),{'a':object(),'b':object()})


def test_cleanup_cannot_be_bypassed_by_a_second_terminal_path():
    definition=GraphDefinition(graph_id='x',version='1',nodes=('start','cleanup'),entry='start',edges={'start':('cleanup','__end__'),'cleanup':('__end__',)},required_cleanup=('cleanup',))
    with pytest.raises(ValueError,match='cleanup'):
        validate_definition(definition,{'start':object(),'cleanup':object()})


def test_bounded_control_loop_stops_and_uses_distinct_visit_receipts(repository):
    from sandbox_graph.graphs.factory import build_definition
    token=LeaseService(repository).acquire_thread('loop','owner',100)
    visits=[]
    async def again(context):
        visits.append(context.visit_id)
        return NodeResult(execution_status='succeeded')
    executor=NodeExecutor(repository,{'a':again},token)
    definition=GraphDefinition(graph_id='loop',version='1',nodes=('a',),entry='a',edges={},dynamic_routes={'a':('a','__end__')},bounded_cycles={'a':3})
    graph=build_definition(definition,executor,routers={'a':lambda state:'a'})
    state={'thread_id':'loop','scope':'G_A','run_ref':'r','plan_ref':'p','graph_ref':'g','activation_id':'loop-a','input_refs':{}}
    output=asyncio.run(graph.ainvoke(state,{'recursion_limit':5}))
    assert output['node_visits']['a']==3
    assert len(set(visits))==3


def test_cycle_budget_cannot_create_implicit_cleanup_bypass():
    from sandbox_graph.graphs.factory import GraphDefinition,validate_definition
    from langgraph.graph import END
    definition=GraphDefinition(graph_id='unsafe',version='1',nodes=('loop','cleanup'),entry='loop',edges={'cleanup':(END,)},dynamic_routes={'loop':('loop','cleanup')},bounded_cycles={'loop':2},required_cleanup=('cleanup',))
    with pytest.raises(ValueError,match='budget.*cleanup'):
        validate_definition(definition,{'loop':object(),'cleanup':object()})


def test_work_graph_refuses_missing_cleanup_adapter_before_any_effect(repository):
    token=LeaseService(repository).acquire_thread('work','owner',30)
    async def handler(context): raise AssertionError('must not execute during compile')
    executor=NodeExecutor(repository,{f'N{i:02}':handler for i in range(11) if i!=9},token)
    with pytest.raises(ValueError,match='handler.*N09'):
        build_work(executor)
