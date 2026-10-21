"""Dedicated side-effect-free interrupt node and exact interrupt resume helper."""
from typing import TypedDict
from langgraph.graph import StateGraph,START,END
from langgraph.types import interrupt,Command

class DecisionState(TypedDict,total=False):
    question_id: str
    scope: str
    plan_ref: str
    graph_ref: str
    wait_version: int
    question: str
    options: list[str]
    decision: dict

async def wait_decision(state):
    # Replay starts here; only immutable question construction occurs before interrupt.
    answer=interrupt({k:state[k] for k in ('question_id','scope','plan_ref','graph_ref','wait_version','question','options')})
    if not isinstance(answer,dict): raise ValueError('STALE_DECISION: structured envelope required')
    for key in ('question_id','scope','plan_ref','graph_ref','wait_version'):
        if answer.get(key)!=state[key]: raise ValueError(f'STALE_DECISION: {key} differs')
    if answer.get('answer') not in state['options'] or not answer.get('actor') or not answer.get('interrupt_id'):
        raise ValueError('STALE_DECISION: invalid answer/actor/interrupt')
    return {'decision':answer}


def build_decision_graph(*,checkpointer):
    if checkpointer is None: raise ValueError('decision interrupt requires checkpointer')
    graph=StateGraph(DecisionState); graph.add_node('WaitDecision',wait_decision)
    graph.add_edge(START,'WaitDecision');graph.add_edge('WaitDecision',END)
    return graph.compile(checkpointer=checkpointer)

async def resume_decision(graph,config,envelope):
    snapshot=await graph.aget_state(config)
    ids={item.id for task in snapshot.tasks for item in task.interrupts}
    interrupt_id=envelope.get('interrupt_id')
    if interrupt_id not in ids: raise ValueError('STALE_DECISION: interrupt ID is not pending on this thread')
    return await graph.ainvoke(Command(resume={interrupt_id:envelope}),config)
