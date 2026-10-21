"""Real LangGraph graphs with deterministic routing and bounded reference state."""
from typing import TypedDict
from pydantic import Field
from langgraph.graph import StateGraph, START, END
from ..domain.models import Model, canonical_hash
from ..catalog.registry import NODE_GROUPS

class GraphState(TypedDict, total=False):
    thread_id: str
    scope: str
    run_ref: str
    plan_ref: str
    graph_ref: str
    activation_id: str
    input_refs: dict[str,str]
    result_refs: dict[str,str]
    results: dict[str,dict[str,str]]
    next_node: str
    wait_ref: str
    node_visits: dict[str,int]

class GraphDefinition(Model):
    graph_id: str
    version: str
    state_schema_version: int = 1
    nodes: tuple[str,...]
    entry: str
    edges: dict[str,tuple[str,...]]
    dynamic_routes: dict[str,tuple[str,...]] = Field(default_factory=dict)
    bounded_cycles: dict[str,int] = Field(default_factory=dict)
    required_cleanup: tuple[str,...] = ()
    @property
    def hash(self): return canonical_hash(self)


def validate_definition(definition,handlers):
    nodes=set(definition.nodes)
    if len(nodes)!=len(definition.nodes) or definition.entry not in nodes: raise ValueError('duplicate nodes or missing entry')
    if nodes-set(handlers): raise ValueError(f'unregistered handler: {sorted(nodes-set(handlers))}')
    if set(definition.edges)&set(definition.dynamic_routes): raise ValueError('fixed and dynamic route overlap')
    if not set(definition.required_cleanup)<=nodes: raise ValueError('required cleanup node removed')
    if definition.required_cleanup and definition.bounded_cycles:
        raise ValueError('cycle budget END could bypass required cleanup; use an explicit acyclic cleanup segment')
    routes=definition.edges|definition.dynamic_routes
    for origin,targets in routes.items():
        if origin not in nodes or set(targets)-nodes-{END}: raise ValueError('route references missing node')
    visiting=set(); visited=set()
    def visit(node):
        if node==END: return
        if node in visiting:
            if definition.bounded_cycles.get(node,0)<=0: raise ValueError('control cycle must be bounded')
            return
        if node in visited: return
        visiting.add(node)
        for target in routes.get(node,(END,)): visit(target)
        visiting.remove(node); visited.add(node)
    visit(definition.entry)
    if nodes-visited: raise ValueError('unreachable graph nodes')
    for cleanup in definition.required_cleanup:
        def reaches(node,seen):
            if node==cleanup: return True
            if node in seen or node==END: return False
            return any(reaches(n,seen|{node}) for n in routes.get(node,()))
        if not reaches(definition.entry,set()): raise ValueError('required cleanup is unreachable')
        def bypasses(node,seen):
            if node==cleanup: return False
            if node==END: return True
            if node in seen: return False
            return any(bypasses(n,seen|{node}) for n in routes.get(node,(END,)))
        if bypasses(definition.entry,set()): raise ValueError('terminal route bypasses required cleanup')
    return definition


def build_work(executor,checkpointer=None):
    graph=StateGraph(GraphState)
    nodes=NODE_GROUPS['work']
    missing=set(nodes)-set(executor.handlers)
    if missing: raise ValueError(f'unregistered work handler: {sorted(missing)}')
    def wrap(node_id):
        async def handle(state):
            result,ref=await executor.execute(node_id,state)
            summary={k:getattr(result,k) for k in ('execution_status','data_status','review_status','performance_status','cleanup_status','coverage_status')}
            if node_id=='N10': next_node=END
            elif node_id=='N09': next_node='N10'
            elif node_id in {'N07','N08'} and result.execution_status!='succeeded': next_node='N09'
            elif node_id not in {'N07','N08'} and result.execution_status!='succeeded': next_node='N07'
            else: next_node=nodes[nodes.index(node_id)+1]
            return {'result_refs':state.get('result_refs',{})|{node_id:ref},'results':state.get('results',{})|{node_id:summary},'next_node':next_node}
        return handle
    for node_id in nodes:
        graph.add_node(node_id,wrap(node_id))
        graph.add_conditional_edges(node_id,lambda state:state['next_node'],list(nodes)+( [END]))
    graph.add_edge(START,'N00')
    return graph.compile(checkpointer=checkpointer)


def build_definition(definition,executor,*,routers=None,checkpointer=None):
    """All handlers must be supplied; no placeholder success nodes are generated."""
    validate_definition(definition,executor.handlers)
    routers=routers or {}
    graph=StateGraph(GraphState)
    def wrap(node):
        async def handle(state):
            result,ref=await executor.execute(node,state)
            return {'result_refs':state.get('result_refs',{})|{node:ref},'results':state.get('results',{})|{node:{'execution_status':result.execution_status,'continuation':result.continuation or ''}},'node_visits':state.get('node_visits',{})|{node:state.get('node_visits',{}).get(node,0)+1}}
        return handle
    for node in definition.nodes: graph.add_node(node,wrap(node))
    for source,targets in definition.edges.items():
        if source in definition.bounded_cycles:
            def bounded_fixed(state, node=source, destinations=targets):
                return END if state['node_visits'][node]>=definition.bounded_cycles[node] else list(destinations)
            graph.add_conditional_edges(source,bounded_fixed,list(set(targets)|{END}))
        else:
            for target in targets: graph.add_edge(source,target)
    for source,targets in definition.dynamic_routes.items():
        if source not in routers: raise ValueError('registered deterministic router required')
        def bounded_dynamic(state,node=source):
            if node in definition.bounded_cycles and state['node_visits'][node]>=definition.bounded_cycles[node]:
                return END
            return routers[node](state)
        graph.add_conditional_edges(source,bounded_dynamic,list(set(targets)|{END}))
    graph.add_edge(START,definition.entry)
    return graph.compile(checkpointer=checkpointer)


def build_run(definition,services,**kwargs): return build_definition(definition,services,**kwargs)
def build_group(definition,services,**kwargs): return build_definition(definition,services,**kwargs)
def build_analysis(definition,services,**kwargs): return build_definition(definition,services,**kwargs)
def build_report(definition,services,**kwargs): return build_definition(definition,services,**kwargs)
