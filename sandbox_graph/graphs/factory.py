"""Real LangGraph graphs with deterministic routing and bounded reference state."""
from typing import TypedDict
from pydantic import Field
from langgraph.graph import StateGraph, START, END
from ..domain.models import Model, canonical_hash
from ..persistence.repository import Conflict
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
    resume_from: str
    recovery_dependency_failures: dict[str,str]
    recovery_acceptance: dict
    recovery_plan_ref: str
    recovery_route: list[str]
    measurement_blocked: bool
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


def build_work(executor,checkpointer=None,*,required_graph_ref=None):
    graph=StateGraph(GraphState)
    nodes=NODE_GROUPS['work']
    from ..services.recovery import RecoveryService, WORK_RECOVERY_CONTRACT
    if tuple(nodes) != tuple(WORK_RECOVERY_CONTRACT['nodes']):
        raise ValueError('WorkGraph recovery contract differs from runtime nodes')
    missing=set(nodes)-set(executor.handlers)
    if missing: raise ValueError(f'unregistered work handler: {sorted(missing)}')
    def wrap(node_id):
        async def handle(state):
            if state.get('recovery_plan_ref'):
                with executor.repository.transaction() as tx:
                    # Loss of ordinary dispatch permission still permits the
                    # bounded sealing/cleanup/delivery path under this owner.
                    RecoveryService.assert_execution_in(tx, state['recovery_plan_ref'], state, executor.token, drain=True)
                    if node_id not in {'N07','N09','N10'}:
                        try:
                            RecoveryService.assert_execution_in(tx, state['recovery_plan_ref'], state, executor.token)
                        except Conflict:
                            return {'results':state.get('results',{}) | {node_id:{'execution_status':'blocked',
                                'data_status':'unverified','review_status':'pending','performance_status':'not_evaluated',
                                'cleanup_status':'pending','coverage_status':'blocked','node_review_decision':'unavailable'}},
                                'next_node':'N09' if node_id == 'N08' else 'N07'}
                    if node_id == 'N08':
                        error = RecoveryService.reentry_dependency_error_in(tx, state['recovery_plan_ref'],
                            node_id=node_id, state=state, token=executor.token)
                        if error:
                            return {'results':state.get('results',{}) | {node_id:{'execution_status':'blocked',
                                'data_status':'unverified','review_status':'pending','performance_status':'not_evaluated',
                                'cleanup_status':'pending','coverage_status':'blocked','node_review_decision':'unavailable',
                                'recovery_dependency_error':error}}, 'next_node':'N09',
                                'recovery_dependency_failures':state.get('recovery_dependency_failures',{}) | {node_id:error}}
            result,ref=await executor.execute(node_id,state)
            summary={k:getattr(result,k) for k in ('execution_status','data_status','review_status','performance_status','cleanup_status','coverage_status')}
            with executor.repository.transaction() as tx:
                review = tx.get('node_reviews', ref)
            summary['node_review_decision'] = review['body']['decision'] if review else 'unavailable'
            rejected = result.review_status == 'rejected' or summary['node_review_decision'] == 'reject'
            unsuccessful = result.execution_status != 'succeeded' or rejected
            dependency_failures = dict(state.get('recovery_dependency_failures', {}))
            if state.get('recovery_plan_ref') and node_id == 'N01':
                with executor.repository.transaction() as tx:
                    error = RecoveryService.reentry_dependency_error_in(tx, state['recovery_plan_ref'],
                        node_id=node_id, state=state, token=executor.token, receipt_ref=ref)
                if error:
                    summary['recovery_dependency_error'] = error
                    dependency_failures[node_id] = error
                    unsuccessful = True
            if unsuccessful: next_node=WORK_RECOVERY_CONTRACT['failure'][node_id]
            elif node_id == 'N10': next_node=END
            elif state.get('recovery_plan_ref'):
                route = state['recovery_route']
                if node_id not in route:
                    raise ValueError('node outside compiled recovery route')
                next_node = route[route.index(node_id)+1] if route.index(node_id)+1 < len(route) else END
            else: next_node=WORK_RECOVERY_CONTRACT['successors'][node_id]
            acceptance = dict(state.get('recovery_acceptance', {}))
            if acceptance.get('source_node') == node_id:
                # Final node acceptance requires the current attempt's closing
                # facts and authoritative action-specific evidence in Driver.
                acceptance['accepted'] = False
                acceptance['execution_review_accepted'] = not unsuccessful
                acceptance['receipt_ref'] = ref
            return {'result_refs':state.get('result_refs',{})|{node_id:ref},'results':state.get('results',{})|{node_id:summary},'next_node':next_node,
                    **({'recovery_acceptance':acceptance} if acceptance else {}),
                    **({'recovery_dependency_failures':dependency_failures} if dependency_failures else {})}
        return handle
    for node_id in nodes:
        graph.add_node(node_id,wrap(node_id))
        graph.add_conditional_edges(node_id,lambda state:state['next_node'],list(nodes)+( [END]))
    async def restore(state):
        with executor.repository.transaction() as tx:
            return RecoveryService.execution_state_in(tx, state['recovery_plan_ref'], state, executor.token)
    graph.add_node('__recovery_entry', restore)
    graph.add_conditional_edges('__recovery_entry', lambda state:state['next_node'], list(nodes))
    def entry(state):
        if required_graph_ref is not None and state.get('graph_ref')!=required_graph_ref:
            raise Conflict('worker graph version differs from held-resource graph')
        if 'resume_from' in state or 'resume_from' in state.get('input_refs', {}):
            raise ValueError('raw resume_from is forbidden; compiled RecoveryPlan required')
        return '__recovery_entry' if state.get('recovery_plan_ref') else 'N00'
    graph.add_conditional_edges(START,entry,list(nodes)+['__recovery_entry'])
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


def build_work_hold(executor,checkpointer=None):
    """New deployment identity; historical work graphs retain their checkpoints."""
    return build_work(executor,checkpointer,required_graph_ref="hold:1")
