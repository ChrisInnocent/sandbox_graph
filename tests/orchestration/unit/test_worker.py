import asyncio
import pytest
from sandbox_graph.runtime.worker import Worker,WorkerPolicy,GraphJobHandler
from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
from sandbox_graph.runtime.waits import WaitRecord,WaitService
from sandbox_graph.runtime.events import EventService,EventEnvelope
from sandbox_graph.domain.models import RunRecord
from sandbox_graph.services.revisions import RevisionService


def enqueue(repository,kind='work',identity='j'):
    return TaskBackend(repository).enqueue(JobSpec(job_id=identity,job_kind=kind,scope_ref='G_A',thread_id='t',activation_id='a',dedupe_key=identity,due_at=1000,deadline=1100,run_id='r',payload={'plan_ref':'p','graph_ref':'g'}))


def run_record(repository):
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='root:t',graph_ref='g',contract_ref='c',active_plan_ref='p',auth_ref='a',lineage_id='r'))


def test_worker_consumes_durable_queue_and_missing_handler_is_blocked(repository):
    run_record(repository);enqueue(repository)
    worker=Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    assert asyncio.run(worker.run_once())
    with repository.transaction() as tx:
        job=tx.get('jobs','j')['body']
        assert job['status']=='blocked' and job['result']['error']['code']=='ADAPTER_MISSING'
    assert not asyncio.run(worker.run_once())


def test_worker_publishes_completion_once_and_event_wakes_parked_parent(repository):
    run_record(repository)
    with repository.transaction() as tx:
        tx.put('activations','parent',{'record':{'activation_id':'parent','run_id':'r','thread_id':'root:t','scope':'r','plan_ref':'p','graph_ref':'g','input_refs':{},'lease_seconds':30,'segment_seconds':20},'hash':'fixture','status':'complete','result':{}},immutable=True)
        tx.put('activation_jobs','parent',{'job_kind':'run','run_id':'r'},immutable=True)
    WaitService(repository).park(WaitRecord(wait_id='wait',activation_id='parent',thread_id='root:t',scope='r',plan_ref='p',graph_ref='g',version=1,aggregate_id='run:r',cursor=0,deadline=1090))
    enqueue(repository,'group')
    async def deliver(job): return {'execution_status':'succeeded','scope':'G_A','evidence_refs':['fixture:evidence']}
    worker=Worker(repository,{'group':deliver},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    assert asyncio.run(worker.run_once())
    worker.pump_outbox();worker.pump_outbox()
    with repository.transaction() as tx:
        events=tx.scan('events')
        assert len(events)==1 and events[0]['body']['payload']['job_id']=='j'
        wakeups=[r for r in tx.scan('jobs') if r['body']['dedupe_key'].startswith('wake:')]
        assert len(wakeups)==1
        assert wakeups[0]['body']['payload']['event_cursor']=='1'
        assert tx.get('waits','wait')['body']['status']=='resumed'


def test_graph_job_handler_uses_real_driver_factory_with_current_token(repository):
    from langgraph.checkpoint.memory import InMemorySaver
    from sandbox_graph.graphs.factory import build_work
    from sandbox_graph.domain.models import NodeResult
    run_record(repository);enqueue(repository)
    seen=[];saver=InMemorySaver()
    def factory(token,activation):
        async def node(context):
            seen.append((context.node_id,token.owner_epoch,context.owner_epoch))
            return NodeResult(execution_status='succeeded')
        return {f'N{i:02}':node for i in range(11)}
    handler=GraphJobHandler(repository,build_work,lambda token:saver,factory,lease_seconds=30,segment_seconds=20)
    worker=Worker(repository,{'work':handler},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    asyncio.run(worker.run_once())
    assert len(seen)==11 and all(a==b==1 for _,a,b in seen)


def test_recovery_handler_does_not_release_unknown_resources(repository):
    from sandbox_graph.runtime.worker import RecoveryJobHandler
    from sandbox_graph.runtime.runner import RunnerStatus,CleanupResult
    run_record(repository)
    with repository.transaction() as tx:
        tx.put('operations','op',{'scope':'G_A','run_id':'r','phase':'uncertain','request':{}},immutable=True)
        tx.put('resource_leases','host_A',{'scope':'G_A','status':'quarantine'},immutable=True)
    class UnknownGateway:
        async def reconcile(self,operation_id,scope,token): return RunnerStatus(state='unknown')
        async def cancel(self,operation_id,scope,reason,token): raise AssertionError('unknown operation must not claim cancelled')
        async def cleanup(self,scope,resource_ids,token): raise AssertionError('unknown resources cannot be released')
    handler=RecoveryJobHandler(repository,UnknownGateway(),lease_seconds=30)
    result=asyncio.run(handler({'thread_id':'recover','scope_ref':'G_A','job_kind':'reconcile','payload':{'operation_id':'op'},'run_id':'r'}))
    assert result['execution_status']=='blocked'
    with repository.transaction() as tx: assert tx.get('resource_leases','host_A')['body']['status']=='quarantine'


def test_work_completion_requires_manifest_and_keeps_failure_facts(repository):
    from langgraph.checkpoint.memory import InMemorySaver
    from sandbox_graph.graphs.factory import build_work
    from sandbox_graph.domain.models import NodeResult,DeliveryManifest
    run_record(repository);enqueue(repository)
    async def node(context): return NodeResult(execution_status='succeeded')
    factory=lambda token,activation:{f'N{i:02}':node for i in range(11)}
    handler=GraphJobHandler(repository,build_work,lambda token:InMemorySaver(),factory,lease_seconds=30,segment_seconds=20)
    with repository.transaction() as tx: job=tx.get('jobs','j')['body']
    result=asyncio.run(handler(job))
    assert result['execution_status']=='blocked' and result['error']['code']=='DELIVERY_MISSING'


def test_wait_wakes_exact_parent_activation_not_lexicographic_latest(repository):
    run_record(repository)
    with repository.transaction() as tx:
        for identity,marker in [('a-parent','correct'),('z-other','wrong')]:
            tx.put('activations',identity,{'record':{'activation_id':identity,'run_id':'r','thread_id':'root:t','scope':'r','plan_ref':'p','graph_ref':'g','input_refs':{'marker':marker},'lease_seconds':30,'segment_seconds':20},'hash':identity,'status':'complete','result':{}},immutable=True)
    WaitService(repository).park(WaitRecord(wait_id='wait',activation_id='a-parent',thread_id='root:t',scope='r',plan_ref='p',graph_ref='g',version=1,aggregate_id='run:r',cursor=0,deadline=1090))
    EventService(repository).append(EventEnvelope(event_id='e',producer_scope='G_A',producer_epoch=1,aggregate_id='run:r',aggregate_seq=1,run_ref='r',plan_ref='p',graph_ref='g',kind='delivery',payload={}))
    worker=Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    worker.pump_outbox()
    with repository.transaction() as tx:
        wake=[r['body'] for r in tx.scan('jobs') if r['body']['dedupe_key'].startswith('wake:')][0]
        assert wake['payload']['marker']=='correct'
        assert wake['payload']['continuation_of']=='a-parent'


def test_queued_resume_command_reaches_original_graph_interrupt(repository):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph,START,END
    from langgraph.types import interrupt
    from sandbox_graph.graphs.factory import GraphState
    from sandbox_graph.runtime.worker import ResumeJobHandler
    from sandbox_graph.interfaces.service import RunService
    from sandbox_graph.planning.compiler import PlanCompiler
    run_record(repository);enqueue(repository,'run')
    def build(executor,checkpointer):
        async def ask(state):
            reply=interrupt({'question_id':'q','scope':state['scope'],'plan_ref':'p','graph_ref':'g','wait_version':1,'options':['continue']})
            return {'wait_ref':reply['answer']}
        graph=StateGraph(GraphState);graph.add_node('WaitDecision',ask);graph.add_edge(START,'WaitDecision');graph.add_edge('WaitDecision',END)
        return graph.compile(checkpointer=checkpointer)
    saver=InMemorySaver();handler=GraphJobHandler(repository,build,lambda token:saver,lambda token,a:{},lease_seconds=30,segment_seconds=20)
    worker=Worker(repository,{'run':handler,'resume':ResumeJobHandler(repository,{'run':handler})},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    asyncio.run(worker.run_once())
    with repository.transaction() as tx: interrupted=tx.get('activations','a')['body']['result']['__interrupt__'][0]['id']
    decision={'interrupt_id':interrupted,'question_id':'q','scope':'G_A','plan_ref':'p','graph_ref':'g','wait_version':1,'actor':'human','answer':'continue'}
    RunService(repository,PlanCompiler([])).resume('resume','r',activation_id='a',decision=decision)
    asyncio.run(worker.run_once())
    with repository.transaction() as tx:
        assert tx.get('activations','a')['body']['status']=='complete'
        assert tx.get('activations','a')['body']['result']['wait_ref']=='continue'


def test_cancel_run_fans_out_scoped_cleanup_even_after_dispatch_gate_closes(repository):
    from sandbox_graph.runtime.worker import RecoveryJobHandler
    from sandbox_graph.interfaces.service import RunService
    from sandbox_graph.planning.compiler import PlanCompiler
    run_record(repository)
    with repository.transaction() as tx:
        tx.put('operations','op',{'scope':'G_A','run_id':'r','phase':'running','request':{}},immutable=True)
    service=RunService(repository,PlanCompiler([]));service.cancel('cancel','r','user request')
    handler=RecoveryJobHandler(repository,object(),lease_seconds=30)
    worker=Worker(repository,{'cleanup':handler},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    asyncio.run(worker.run_once())
    with repository.transaction() as tx:
        children=[r['body'] for r in tx.scan('jobs') if r['body']['scope_ref']=='G_A']
        assert len(children)==1 and children[0]['job_kind']=='cleanup'
        assert tx.get('dispatch_gates','r')['body']['open'] is False


def test_deadline_expiry_publishes_terminal_event_for_parent(repository):
    run_record(repository);enqueue(repository)
    repository.clock.advance(200)
    worker=Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    assert not asyncio.run(worker.run_once())
    worker.pump_outbox()
    with repository.transaction() as tx:
        job=tx.get('jobs','j')['body']
        assert job['status']=='failed'
        assert job['result']['error']['code']=='DEADLINE_OR_ATTEMPT_BUDGET'
        assert len(tx.scan('events'))==1


def test_delivery_with_unknown_cleanup_quarantines_reservation_before_publication(repository):
    from sandbox_graph.domain.models import DeliveryManifest,NodeResult
    run_record(repository);enqueue(repository)
    with repository.transaction() as tx:
        tx.put('resource_leases','host',{'scope':'G_A','status':'reserved'})
        job=tx.get('jobs','j')['body']
    async def finalizer(job,result):
        return DeliveryManifest(delivery_id='d',version=1,scope='G_A',plan_ref='p',contract_ref='c',work_refs=('G_A',),evidence=(),result=NodeResult(execution_status='succeeded',cleanup_status='unknown'))
    handler=GraphJobHandler(repository,None,None,None,lease_seconds=30,segment_seconds=20,delivery_finalizer=finalizer)
    asyncio.run(handler.finalize(job,{'results':{}}))
    with repository.transaction() as tx:
        assert tx.get('resource_leases','host')['body']['status']=='quarantine'


def test_released_shared_domain_wakes_waiting_other_group(repository):
    from sandbox_graph.planning.compiler import PlanCompiler,Case,Selection
    from sandbox_graph.runtime.leases import LeaseService
    plan=PlanCompiler([Case(capability='supported',adapter_verified=True,case_id='b',group_id='G_B',sandbox_type='x',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'size':1},),conflict_domains=('shared',))]).compile(Selection(),authorized_groups={'G_B'})
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='root',graph_ref='g',active_plan_ref=plan.hash,contract_ref='c',auth_ref='a',lineage_id='r'))
    with repository.transaction() as tx:
        tx.put('plans',plan.hash,plan.model_dump(mode='json'),immutable=True)
        tx.put('activations','parent',{'record':{'activation_id':'parent','run_id':'r','thread_id':'group:B','scope':'G_B','plan_ref':plan.hash,'graph_ref':'g','input_refs':{},'lease_seconds':30,'segment_seconds':20},'hash':'fixture','status':'complete','result':{}},immutable=True)
        tx.put('activation_jobs','parent',{'job_kind':'group','run_id':'r','job_id':'parentjob'},immutable=True)
    WaitService(repository).park(WaitRecord(wait_id='wait',activation_id='parent',thread_id='group:B',scope='G_B',plan_ref=plan.hash,graph_ref='g',version=1,aggregate_id='group:r:G_B',cursor=0,deadline=1100))
    leases=LeaseService(repository);token=leases.acquire_thread('group:A','owner',30)
    leases.acquire_all(['shared'],scope='G_A',token=token,owner_run_id='r')
    leases.release(['shared'],scope='G_A',token=token,closed=True)
    worker=Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    worker.pump_outbox()
    with repository.transaction() as tx:
        assert tx.get('waits','wait')['body']['status']=='resumed'
        wakes=[row['body'] for row in tx.scan('jobs')]
        assert len(wakes)==1 and wakes[0]['scope_ref']=='G_B'


def test_resource_release_before_wait_registration_is_not_lost(repository):
    from sandbox_graph.planning.compiler import PlanCompiler,Case,Selection
    from sandbox_graph.runtime.leases import LeaseService
    plan=PlanCompiler([Case(capability='supported',adapter_verified=True,case_id='b',group_id='G_B',sandbox_type='x',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'size':1},),conflict_domains=('shared',))]).compile(Selection(),authorized_groups={'G_B'})
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='root',graph_ref='g',active_plan_ref=plan.hash,contract_ref='c',auth_ref='a',lineage_id='r'))
    with repository.transaction() as tx:
        tx.put('plans',plan.hash,plan.model_dump(mode='json'),immutable=True)
        tx.put('groups','group-key',{'run_id':'r','group_id':'G_B','plan_ref':plan.hash,'status':'pending','job_id':'group-job'},immutable=True)
    leases=LeaseService(repository);token=leases.acquire_thread('group:A','owner',30)
    leases.acquire_all(['shared'],scope='G_A',token=token,owner_run_id='r')
    leases.release(['shared'],scope='G_A',token=token,closed=True)
    Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1)).pump_outbox()
    assert WaitService(repository).park(WaitRecord(wait_id='late-wait',activation_id='parent',thread_id='group:B',scope='G_B',plan_ref=plan.hash,graph_ref='g',version=1,aggregate_id='group:r:G_B',cursor=0,deadline=1100))=='ready'


def test_verified_cleanup_releases_reservation_leaving_no_residue(repository):
    """issue #3 期望：清理已核实（cleanup_status=complete, 无未关闭资源）不产生任何租约残留。"""
    from sandbox_graph.domain.models import DeliveryManifest,NodeResult
    run_record(repository);enqueue(repository)
    with repository.transaction() as tx:
        tx.put('resource_leases','host',{'scope':'G_A','status':'reserved'})
        job=tx.get('jobs','j')['body']
    async def finalizer(job,result):
        return DeliveryManifest(delivery_id='d',version=1,scope='G_A',plan_ref='p',contract_ref='c',
            work_refs=('G_A',),evidence=(),result=NodeResult(execution_status='succeeded',cleanup_status='complete'))
    handler=GraphJobHandler(repository,None,None,None,lease_seconds=30,segment_seconds=20,delivery_finalizer=finalizer)
    asyncio.run(handler.finalize(job,{'results':{}}))
    with repository.transaction() as tx:
        assert tx.get('resource_leases','host') is None
        assert tx.scan('resource_leases')==[]
