import asyncio
from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.domain.models import DeliveryManifest,NodeResult
from sandbox_graph.planning.compiler import Case,Selection,PlanCompiler
from sandbox_graph.interfaces.service import RunService
from sandbox_graph.services.reviews import ReviewService
from sandbox_graph.services.workflows import WorkflowHandlers
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.runtime.leases import LeaseService


def test_group_delivery_and_incremental_analysis_freeze_group_versions_only(repository,tmp_path):
    cases=[Case(capability='supported',adapter_verified=True,case_id=g,group_id=g,sandbox_type='synthetic',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'cpu':1},),contract_ref='c') for g in ('G_A','G_B')]
    service=RunService(repository,PlanCompiler(cases));plan=service.preview(Selection(),authorized_groups={'G_A','G_B'},contract_ref='c')
    run=service.start('run',plan.hash,authorized_groups={'G_A','G_B'})
    store=FileArtifactStore(tmp_path);reviews=ReviewService(repository,store)
    handlers=WorkflowHandlers(repository,deadline=1090,review_service=reviews)
    token=LeaseService(repository).acquire_thread('group:A','group',100)
    executor=NodeExecutor(repository,handlers.mapping(),token)
    state={'thread_id':'group:A','scope':'G_A','run_ref':run.run_id,'plan_ref':plan.hash,'graph_ref':plan.graph_ref,'activation_id':'group-1','input_refs':{},'result_refs':{}}
    ref=store.commit([b'{"samples":[1]}'],scope='G_A',media_type='application/json')
    first=DeliveryManifest(delivery_id='work-delivery',version=1,scope='G_A',plan_ref=plan.hash,contract_ref='c',work_refs=('work-A',),evidence=(ref,),result=NodeResult(execution_status='succeeded',data_status='valid',cleanup_status='complete'))
    def add(delivery):
        with repository.transaction() as tx:
            tx.put('work_instances','work-A',{'work_id':'work-A','run_id':run.run_id,'group_id':'G_A','plan_ref':plan.hash,'status':'delivered','result_ref':'job:work'})
            key=f'{delivery.delivery_id}:{delivery.version}'
            tx.put('work_deliveries',key,delivery.model_dump(mode='json'),immutable=True)
            tx.put('work_delivery_owners',key,{'run_id':run.run_id,'group_id':'G_A','work_id':'work-A'},immutable=True)
    add(first)
    async def publish():
        for node in ('T6','T8','GE'):
            result,receipt=await executor.execute(node,state)
            assert result.execution_status=='succeeded',result
            state['result_refs'][node]=receipt
    asyncio.run(publish())
    root_token=LeaseService(repository).acquire_thread(run.root_thread_id,'root',100)
    root=NodeExecutor(repository,handlers.mapping(),root_token)
    root_state=state|{'thread_id':run.root_thread_id,'scope':run.run_id,'activation_id':'root-1','result_refs':{}}
    result,_=asyncio.run(root.execute('R3',root_state))
    frozen=reviews.get_analysis(result.output['analysis_input_ref'])
    assert frozen['delivery_kind']=='group_deliveries'
    assert set(frozen['deliveries'])=={'G_A'}
    group_id,version=frozen['deliveries']['G_A']
    assert version==1
    assert reviews.resolve_analysis(result.output['analysis_input_ref'])['groups']['G_A']['work_deliveries'][0]['delivery_id']=='work-delivery'
    add(first.model_copy(update={'version':2,'gaps':('late qualification',)}))
    state['activation_id']='group-2';state['result_refs']={}
    asyncio.run(publish())
    root_state['activation_id']='root-2'
    second,_=asyncio.run(root.execute('R3',root_state))
    assert reviews.get_analysis(second.output['analysis_input_ref'])['deliveries']['G_A']==[group_id,2]
    assert frozen['deliveries']['G_A']==[group_id,1]
    with repository.transaction() as tx:
        assert len([j for j in tx.scan('jobs') if j['body']['job_kind']=='analysis'])==2
        assert not any(j['body']['scope_ref']=='G_B' and j['body']['job_kind']=='work' for j in tx.scan('jobs'))


def test_terminal_missing_delivery_records_partial_disposition_without_claiming_cleanup(repository):
    from sandbox_graph.domain.models import RunRecord
    from sandbox_graph.services.revisions import RevisionService
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='root',graph_ref='g',active_plan_ref='p',contract_ref='c',auth_ref='a',lineage_id='r'))
    with repository.transaction() as tx:
        tx.put('work_instances','w',{'work_id':'w','run_id':'r','group_id':'G_A','plan_ref':'p','status':'failed','result_ref':'job:failed'})
    token=LeaseService(repository).acquire_thread('group','owner',30)
    executor=NodeExecutor(repository,WorkflowHandlers(repository,deadline=1090).mapping(),token)
    state={'thread_id':'group','scope':'G_A','run_ref':'r','plan_ref':'p','graph_ref':'g','activation_id':'a','input_refs':{}}
    result,_=asyncio.run(executor.execute('T6',state))
    assert result.execution_status=='succeeded' and result.review_status=='rejected'
    with repository.transaction() as tx:
        disposition=tx.get('scope_dispositions','w')['body']
        assert disposition['result']['scope_disposition']=='failed'
        assert disposition['result']['cleanup_status']=='unknown'
        assert disposition['processing_ref']
