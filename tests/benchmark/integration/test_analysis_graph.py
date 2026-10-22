"""Real formulas/Excel/LangGraph; explicit synthetic repository and data."""
import json
from pathlib import Path
import pytest
from tests.orchestration.conftest import MemoryRepository, FakeClock
from sandbox_bench.analysis import export_case
from sandbox_bench.workflow_services import AnalysisReportServices
from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.domain.models import DeliveryManifest, NodeResult, RunRecord
from sandbox_graph.services.reviews import ReviewService
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.services.workflows import WorkflowHandlers, analysis_definition, linear_routers, report_definition
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.graphs.factory import build_definition as build_graph


def setup(tmp_path):
    repo = MemoryRepository(FakeClock())
    store = FileArtifactStore(tmp_path / 'artifacts')
    reviews = ReviewService(repo, store)
    RevisionService(repo).create(RunRecord(run_id='r', root_thread_id='t', graph_ref='graph1',
        contract_ref='contract1', active_plan_ref='plan1', auth_ref='test', lineage_id='r'))
    refs = {}
    deliveries = {}
    for group, product in [('ga', 'kuasar'), ('gb', 'cube')]:
        case_id = 'case-' + group
        descriptor = {'product':product, 'product_version':'1', 'method_id':'B01.TEMPLATE',
                      'method_version':'1', 'completion':{'endpoint':'STATE_READY'},
                      'spec':{'vcpus':2,'memory_bytes':2048}, 'cache_profile':'cold',
                      'condition':{'hardware_profile':'fixture'}, 'window':{'kind':'natural_drain'}}
        directory = tmp_path / case_id
        manifest = export_case({'case_id':case_id, 'case_hash':case_id, 'plan_ref':'plan1',
            'contract_ref':'contract1', 'comparison':descriptor, 'validation_policy':'none',
            'operations':[{'operation_id':'1','status':'success','elapsed_s':2,'repeat':1,'method':'B01.TEMPLATE'}],
            'assertions':[{'kind':'effective_spec','status':'valid'}]}, directory)
        index = {}
        for name in set(manifest['files']) | {'case_manifest.json'}:
            ref = store.commit(((directory/name).read_bytes(),), scope=group,
                media_type='application/octet-stream', producer='benchmark.session.v1')
            index[name] = ref.model_dump(mode='json'); refs[ref.artifact_id] = ref
        index_ref = store.commit((json.dumps({'case_id':case_id,'case_hash':case_id,'paths':index}).encode(),),
            scope=group, media_type='application/octet-stream', producer='benchmark.session.v1')
        evidence = tuple(r for r in refs.values() if r.scope == group) + (index_ref,)
        delivery = DeliveryManifest(delivery_id=group, version=1, scope=group, plan_ref='plan1',
            contract_ref='contract1', work_refs=(case_id,), evidence=evidence,
            result=NodeResult(execution_status='succeeded', data_status='valid',
                             output={'case_index_ref':index_ref.artifact_id,'case_hash':case_id}))
        reviews.review_delivery(delivery, expected_scope=group, plan_ref='plan1', contract_ref='contract1')
        deliveries[group] = (group,1)
        with repo.transaction() as tx:
            tx.put('work_instances',case_id,{'work_id':case_id,'run_id':'r','plan_ref':'plan1','status':'delivered'})
            tx.put('scope_dispositions',case_id,{'run_id':'r','plan_ref':'plan1','processing_ref':group,
                'result':NodeResult(execution_status='succeeded',data_status='valid',
                    cleanup_status='complete',scope_disposition='fulfilled').model_dump(mode='json')})
    frozen = reviews.freeze_analysis('plan1',deliveries,rules_ref='v1')
    return repo,store,reviews,frozen


@pytest.mark.asyncio
@pytest.mark.parametrize('foreign_disposition',[False,True])
async def test_frozen_cases_flow_through_analysis_excel_report_and_archive(tmp_path,foreign_disposition):
    repo,store,reviews,frozen = setup(tmp_path)
    services = AnalysisReportServices(repo,reviews,store,deadline=1100)
    handlers = WorkflowHandlers(repo,deadline=1100,node_services=services.mapping(),review_service=reviews).mapping()
    lease = LeaseService(repo).acquire_thread('analysis','worker',50)
    definition = analysis_definition()
    graph = build_graph(definition,NodeExecutor(repo,handlers,lease),
                        routers=linear_routers(definition.nodes))
    state = {'thread_id':'analysis','scope':'r','run_ref':'r','plan_ref':'plan1','graph_ref':'graph1',
             'activation_id':'a','input_refs':{'analysis_input_ref':frozen}}
    result = await graph.ainvoke(state)
    assert all(r['execution_status']=='succeeded' for r in result['results'].values()),result
    with repo.transaction() as tx:
        job = tx.scan('jobs')[0]['body']
        analysis = tx.get('analysis_results',job['payload']['analysis_ref'])['body']
        export = tx.get('analysis_exports',job['payload']['analysis_ref'])['body']
    facts = json.loads(store.path_for(__import__('sandbox_graph.domain.models',fromlist=['EvidenceRef']).EvidenceRef.model_validate(analysis['facts_ref'])).read_bytes())
    assert facts['pairs'][0]['status']=='comparable'
    assert 'facts.xlsx' in export['files']
    if foreign_disposition:
        with repo.transaction() as tx:
            row=tx.get('scope_dispositions','case-ga')['body']
            tx.put('scope_dispositions','case-ga',row|{'run_id':'another-run'})
    lease = LeaseService(repo).acquire_thread(job['thread_id'],'worker',50)
    definition = report_definition()
    graph = build_graph(definition,NodeExecutor(repo,handlers,lease),routers=linear_routers(definition.nodes))
    result = await graph.ainvoke({**state,'thread_id':job['thread_id'],'activation_id':job['activation_id'],
                                 'input_refs':job['payload']})
    if foreign_disposition:
        assert result['results']['F4']['execution_status']=='blocked'
        with repo.transaction() as tx: assert not tx.scan('archives')
        return
    assert all(r['execution_status']=='succeeded' for r in result['results'].values()),result
    with repo.transaction() as tx:
        assert tx.get('receipts',result['result_refs']['F4'])['body']['result']['output']['archive_status']=='complete'
        assert len(tx.scan('archives'))==1
        # F1 must also expose the Excel so it is discoverable before F4 closes.
        f1=tx.get('receipts',result['result_refs']['F1'])['body']['result']
        excel_id=export['files']['facts.xlsx']['artifact_id']
        assert f1['output']['excel_ref']==excel_id
        assert excel_id in f1['artifact_refs']
        assert excel_id in tx.scan('archives')[0]['body']['artifact_refs']


def test_frozen_delivery_path_and_digest_are_checked(tmp_path):
    repo,store,reviews,frozen = setup(tmp_path)
    services = AnalysisReportServices(repo,reviews,store,deadline=1100)
    destination = tmp_path/'materialized'; destination.mkdir()
    paths,expected,gaps = services._materialize(frozen,'plan1',destination)
    assert len(paths)==2 and len(expected)==2 and gaps==[]
    with pytest.raises(Exception,match='another plan'):
        services._materialize(frozen,'foreign',destination)
    ref = reviews.resolve_analysis(frozen)['groups']['ga']['delivery']['evidence'][0]
    (store.root/ref['uri']).write_bytes(b'corrupt')
    destination2 = tmp_path/'bad';destination2.mkdir()
    with pytest.raises(ValueError,match='digest'):
        services._materialize(frozen,'plan1',destination2)


@pytest.mark.parametrize('changed', ['run_ref','plan_ref','activation_id','thread_id','scope','graph_ref','node_id'])
def test_analysis_rejects_receipt_from_another_node_scope_or_activation(tmp_path,changed):
    from types import SimpleNamespace
    from sandbox_graph.persistence.repository import Conflict
    repo,store,reviews,frozen=setup(tmp_path)
    services=AnalysisReportServices(repo,reviews,store,deadline=1100)
    identity={'run_ref':'r','plan_ref':'plan1','activation_id':'a','thread_id':'t',
              'scope':'r','graph_ref':'graph1','node_id':'A3'}
    context=SimpleNamespace(**identity,result_refs={'A3':'receipt'})
    with repo.transaction() as tx:
        tx.put('receipts','receipt',{'identity':dict(identity,**{changed:'foreign'}),
                                  'result':{'output':{'analysis_ref':'untrusted'}}})
    with pytest.raises(Conflict,match='another activation'):
        services._receipt_output(context,'A3')


def test_accepted_reuse_materializes_original_seal_once_and_preserves_both_aliases(tmp_path):
    from sandbox_graph.domain.models import WorkKey,canonical_hash
    from sandbox_graph.services.reuse import ReuseService,ReuseContract
    from sandbox_bench.comparison import compare_cases
    repo,store,reviews,_=setup(tmp_path)
    condition=canonical_hash({'q':1})
    contract=ReuseContract(environment_ref='environment:old',spec_hash='spec:old',
        method_ref='B01.TEMPLATE:1',completion_event='STATE_READY',time_domain='clock:old',
        script_ref='script:old',condition_hash=condition,valid_until=1100,measurement_contract_hash='a'*64)
    key=WorkKey(group_id='ga',scenario_id='B01',method_id='B01.TEMPLATE',
                method_version='1',condition_hash=condition,repeat_id=1)
    with repo.transaction() as tx:
        original=DeliveryManifest.model_validate(tx.get('deliveries','ga:1')['body'])
        old_work=tx.get('work_instances','case-ga')['body']
        tx.put('work_instances','case-ga',old_work|{'key':key.model_dump(mode='json'),
            'environment_ref':'environment:old','group_id':'ga'})
        tx.put('measurement_bindings','case-ga',{'work_id':'case-ga','run_id':'r',
            'plan_ref':'plan1','case_hash':'case-ga','measurement_contract_hash':'a'*64,'script_ref':'script:old'})
    source=original.model_copy(update={'delivery_id':'ga-reusable','result':original.result.model_copy(
        update={'output':{**original.result.output,'reuse_contract':contract.model_dump(mode='json')}})})
    reviews.review_delivery(source,expected_scope='ga',plan_ref='plan1',contract_ref='contract1')
    RevisionService(repo).create(RunRecord(run_id='new',root_thread_id='new-root',graph_ref='g2',
        active_plan_ref='plan2',contract_ref='contract2',auth_ref='a',lineage_id='new'))
    lease=LeaseService(repo).acquire_thread('new-group','owner',50)
    reuse=ReuseService(repo,reviews)
    deliveries={}
    for work in ('alias-a','alias-b'):
        with repo.transaction() as tx:
            tx.put('work_instances',work,{'work_id':work,'run_id':'new','plan_ref':'plan2',
                'graph_ref':'g2','contract_ref':'contract2','group_id':work,'environment_ref':'environment:old',
                'key':key.model_copy(update={'group_id':work}).model_dump(mode='json'),'status':'pending'})
            tx.put('measurement_bindings',work,{'work_id':work,'run_id':'new','plan_ref':'plan2',
                'case_hash':work,'measurement_contract_hash':'a'*64,'script_ref':'script:old'})
        reuse.consider(work,'ga-reusable:1',old_contract=contract,new_contract=contract,
                       deadline=1050,allowed_source_scopes={'ga'})
        alias=reuse.project_accepted(work,token=lease)
        deliveries[work]=(alias.delivery_id,alias.version)
    frozen=reviews.freeze_analysis('plan2',deliveries,rules_ref='v1')
    folder=tmp_path/'reused';folder.mkdir();aliases={}
    services=AnalysisReportServices(repo,reviews,store,deadline=1100)
    paths,expected,gaps=services._materialize(frozen,'plan2',folder,run_id='new',alias_sink=aliases)
    assert gaps==[] and len(paths)==1 and set(expected)=={'case-ga'}
    assert set(aliases)=={'alias-a','alias-b'}
    facts=compare_cases(paths,expected_manifests=expected,case_aliases=aliases)
    assert len(facts['cases'])==1 and facts['cases'][0]['success_samples']==1
    assert facts['case_aliases']['alias-a']['case_id']=='case-ga'
    assert json.loads((paths[0]/'case_manifest.json').read_bytes())['metadata']['plan_ref']=='plan1'
