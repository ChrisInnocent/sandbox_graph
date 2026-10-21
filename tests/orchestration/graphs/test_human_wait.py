import asyncio
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sandbox_graph.graphs.decisions import build_decision_graph


def test_real_interrupt_rejects_wrong_version_and_accepts_exact_envelope():
    saver=InMemorySaver(); graph=build_decision_graph(checkpointer=saver)
    config={'configurable':{'thread_id':'decision-thread'}}
    state={'question_id':'q','scope':'G_A','plan_ref':'p','graph_ref':'g','wait_version':1,'question':'Which authorized option?','options':['stop','continue']}
    out=asyncio.run(graph.ainvoke(state,config))
    interrupt=out['__interrupt__'][0]
    wrong={'question_id':'q','scope':'G_A','plan_ref':'old','graph_ref':'g','wait_version':1,'interrupt_id':interrupt.id,'actor':'human','answer':'continue'}
    with pytest.raises(ValueError,match='STALE_DECISION'):
        asyncio.run(graph.ainvoke(Command(resume={interrupt.id:wrong}),config))
    # A fresh isolated thread proves the correct route, without treating an error checkpoint as clean.
    config2={'configurable':{'thread_id':'decision-thread-2'}}
    out2=asyncio.run(graph.ainvoke(state,config2)); exact=wrong|{'plan_ref':'p','interrupt_id':out2['__interrupt__'][0].id}
    out3=asyncio.run(graph.ainvoke(Command(resume={exact['interrupt_id']:exact}),config2))
    assert out3['decision']['answer']=='continue'
