import asyncio
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sandbox_graph.runtime.driver import Driver, Activation
from sandbox_graph.domain.models import RunRecord, NodeResult
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.persistence.repository import Conflict


def test_driver_persists_frozen_activation_and_rejects_new_graph(repository):
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='t',graph_ref='g1',contract_ref='c',active_plan_ref='p',auth_ref='auth',lineage_id='r'))
    count=[]
    async def node(context): count.append(context.node_id); return NodeResult(execution_status='succeeded')
    saver=InMemorySaver()
    driver=Driver(repository,{f'N{i:02}':node for i in range(11)},lambda token:saver,build_work)
    activation=Activation(activation_id='a',run_id='r',thread_id='t',scope='G_A',plan_ref='p',graph_ref='g1',input_refs={'work':'w'},lease_seconds=30,segment_seconds=20)
    result=asyncio.run(driver.start(activation))
    assert result['results']['N10']['execution_status']=='succeeded'
    assert len(count)==11
    assert asyncio.run(driver.start(activation))==result
    assert len(count)==11
    with pytest.raises(Conflict): asyncio.run(driver.start(activation.model_copy(update={'graph_ref':'g2'})))


def test_completed_activation_releases_only_thread_lease_for_next_activation(repository):
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='t',graph_ref='g',contract_ref='c',active_plan_ref='p',auth_ref='auth',lineage_id='r'))
    async def node(context): return NodeResult(execution_status='succeeded')
    saver=InMemorySaver();driver=Driver(repository,{f'N{i:02}':node for i in range(11)},lambda token:saver,build_work)
    a=Activation(activation_id='a1',run_id='r',thread_id='t',scope='G_A',plan_ref='p',graph_ref='g',input_refs={},lease_seconds=30,segment_seconds=20)
    with repository.transaction() as tx: tx.put('resource_leases','host_A',{'scope':'G_A','status':'reserved'})
    asyncio.run(driver.start(a))
    out=asyncio.run(driver.start(a.model_copy(update={'activation_id':'a2'})))
    assert out['results']['N10']['execution_status']=='succeeded'
    with repository.transaction() as tx: assert tx.get('resource_leases','host_A')['body']['status']=='reserved'


def test_driver_interrupt_is_waiting_and_exact_decision_resumes_same_checkpoint(repository):
    from langgraph.graph import StateGraph,START,END
    from langgraph.types import interrupt
    from sandbox_graph.graphs.factory import GraphState
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='t',graph_ref='g',contract_ref='c',active_plan_ref='p',auth_ref='auth',lineage_id='r'))
    def builder(executor,checkpointer):
        async def ask(state):
            answer=interrupt({'question_id':'q','scope':state['scope'],'plan_ref':state['plan_ref'],'graph_ref':state['graph_ref'],'wait_version':1,'options':['stop','continue']})
            return {'wait_ref':answer['answer']}
        graph=StateGraph(GraphState);graph.add_node('WaitDecision',ask);graph.add_edge(START,'WaitDecision');graph.add_edge('WaitDecision',END)
        return graph.compile(checkpointer=checkpointer)
    saver=InMemorySaver();driver=Driver(repository,{},lambda token:saver,builder)
    a=Activation(activation_id='human',run_id='r',thread_id='t',scope='G_A',plan_ref='p',graph_ref='g',input_refs={},lease_seconds=30,segment_seconds=20)
    result=asyncio.run(driver.start(a))
    with repository.transaction() as tx: assert tx.get('activations','human')['body']['status']=='waiting'
    interrupt_id=result['__interrupt__'][0]['id']
    answer={'interrupt_id':interrupt_id,'question_id':'q','scope':'G_A','plan_ref':'p','graph_ref':'g','wait_version':1,'actor':'human','answer':'continue'}
    with pytest.raises(Conflict): asyncio.run(driver.resume('human',decision=answer|{'plan_ref':'new'}))
    output=asyncio.run(driver.resume('human',decision=answer))
    assert output['wait_ref']=='continue'
    with repository.transaction() as tx: assert tx.get('activations','human')['body']['status']=='complete'
    assert asyncio.run(driver.resume('human',decision=answer))==output
