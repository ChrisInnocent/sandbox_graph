"""Real LangGraph/BenchSession/formulas; repository and sandbox explicitly synthetic."""
import pytest
from tests.orchestration.conftest import MemoryRepository,FakeClock
from tests.benchmark.unit.test_contract_models import sample
from tests.benchmark.unit.test_session import Scenario
from sandbox_bench.contracts import CaseContract
from sandbox_bench.session import BenchSession
from sandbox_bench.bridge import BenchGateway,work_handlers
from sandbox_graph.runtime.runner import OperationService
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.domain.models import ScriptRef,RunRecord
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.graphs.factory import build_work
from sandbox_bench.analysis import recompute_case

@pytest.mark.asyncio
async def test_real_work_graph_runs_receipts_analysis_and_replay(tmp_path):
    case=CaseContract.model_validate(sample());scenario=Scenario()
    config={'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1',
            'source':{'kind':case.source.kind,'id':case.source.id},
            'create_body':{'timeout':100,'metadata':{'kuasar-sandbox.resource':'{"capacity":{"cpu":2,"memory":"2GiB"}}'}},
            'source_evidence':{'source_id':case.source.id,'source_digest':case.source.digest,
                'environment_ref':case.environment_ref,'cache_profile':case.cache_profile,
                'effective_spec':case.spec.model_dump(),'evidence_ref':'synthetic-source-evidence'},
            'environment_evidence':{'ref':case.environment_ref,'product_version':case.product_version,'evidence_kind':'synthetic'}}
    session=BenchSession(case,config,tmp_path,scenario=scenario)
    repo=MemoryRepository(FakeClock())
    RevisionService(repo).create(RunRecord(run_id='r',root_thread_id='t',graph_ref='graph1',
        contract_ref='contracthash',active_plan_ref='plan1',auth_ref='fixture-auth',lineage_id='r'))
    token=LeaseService(repo).acquire_thread('t','worker',100)
    script=ScriptRef(script_id='benchmark.step',version='1',digest='a'*64)
    gateway=BenchGateway({'g/w':session},script)
    service=OperationService(repo,gateway,{('benchmark.step','1'):script})
    handlers=work_handlers(service,token,session,run_id='r',scope='g/w',dispatch_generation=1,deadline=1050)
    graph=build_work(NodeExecutor(repo,handlers,token))
    state={'thread_id':'t','scope':'g/w','run_ref':'r','plan_ref':'plan1','graph_ref':'graph1',
           'activation_id':'a','input_refs':{'case':case.digest()}}
    output=await graph.ainvoke(state)
    assert all(r['execution_status']=='succeeded' for r in output['results'].values()),output['results']
    assert len(output['results'])==11
    assert recompute_case(tmp_path/'evidence')['throughput']['count']==5
    await graph.ainvoke(state)
    assert scenario.calls==[0,1,2,3,4]

@pytest.mark.asyncio
async def test_finalizer_binds_case_identity_and_verifies_copied_artifacts(tmp_path):
    import json
    from sandbox_bench.bridge import delivery_finalizer
    from sandbox_graph.artifacts.store import FileArtifactStore
    case=CaseContract.model_validate(sample())
    session=BenchSession(case,{'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1'},tmp_path/'work',scenario=Scenario())
    for node in ('N05','N08','N09','N10'):
        assert (await session.apply_step(node,node))['status']=='completed'
    store=FileArtifactStore(tmp_path/'artifacts')
    finalize=delivery_finalizer({'w':session},store)
    job={'scope_ref':'w','payload':{'plan_ref':'plan1','group_id':'g'}}
    manifest=await finalize(job,{'results':{'N10':{'execution_status':'succeeded'}}})
    assert manifest.work_refs==('w',) and manifest.result.data_status=='unverified'
    for ref in manifest.evidence:store.verify(ref)
    path=session.folder/'evidence/case_manifest.json'
    foreign=json.loads(path.read_text());foreign['case_id']=foreign['metadata']['case_id']='foreign'
    path.write_text(json.dumps(foreign))
    with pytest.raises(ValueError,match='identity|sealed summary'):await finalize(job,{'results':{}})


@pytest.mark.asyncio
async def test_finalizer_seals_explicit_reuse_policy_against_work_case_and_script(tmp_path):
    from sandbox_bench.bridge import delivery_finalizer
    from sandbox_graph.artifacts.store import FileArtifactStore
    from sandbox_graph.domain.models import WorkKey,canonical_hash
    from tests.benchmark.unit.test_session import ValidationAdapter
    case=CaseContract.model_validate(sample())
    session=BenchSession(case,{'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1'},
                         tmp_path/'session',scenario=Scenario(),clock_domain='host:boot:monotonic')
    session.adapter=ValidationAdapter()
    await session._owned({'sandbox_id':'s'})
    for node in ('N05','N08','N09','N10'):
        assert (await session.apply_step(node,node))['status']=='completed'
    repo=MemoryRepository(FakeClock())
    key=WorkKey(group_id=case.group_id,scenario_id='B01',method_id=case.method_id,
                method_version=case.method_version,condition_hash=canonical_hash({'template':'fixed'}),
                repeat_id=case.repeat_id)
    with repo.transaction() as tx:
        tx.put('work_instances',case.work_id,{'work_id':case.work_id,'run_id':'r','group_id':case.group_id,
            'plan_ref':case.plan_ref,'environment_ref':case.environment_ref,'key':key.model_dump(mode='json')})
    script=ScriptRef(script_id='benchmark.step',version='1',digest='a'*64)
    from sandbox_bench.identity import measurement_contract_hash
    with repo.transaction() as tx:
        tx.put('measurement_bindings',case.work_id,{'work_id':case.work_id,'run_id':'r',
            'plan_ref':case.plan_ref,'case_hash':case.digest(),
            'measurement_contract_hash':measurement_contract_hash(case),'script_ref':canonical_hash(script)})
    policy={'case_hash':case.digest(),'valid_until':1100,'evidence_ref':'approved-freshness-policy:v1'}
    store=FileArtifactStore(tmp_path/'artifacts')
    finalize=delivery_finalizer({case.work_id:session},store,repository=repo,script_ref=script,
                                reuse_policies={case.work_id:policy})
    job={'run_id':'r','scope_ref':case.work_id,'payload':{'plan_ref':case.plan_ref,'group_id':case.group_id}}
    manifest=await finalize(job,{'results':{'N10':{'execution_status':'succeeded'}}})
    assert manifest.result.data_status=='valid'
    contract=manifest.result.output['reuse_contract']
    assert contract['spec_hash']==canonical_hash(case.spec)
    assert contract['condition_hash']==key.condition_hash and contract['script_ref']==canonical_hash(script)
    assert contract['time_domain']=='host:boot:monotonic'
    assert manifest.result.output['reuse_policy_ref'] in {r.artifact_id for r in manifest.evidence}
    bad=delivery_finalizer({case.work_id:session},store,repository=repo,script_ref=script,
                          reuse_policies={case.work_id:dict(policy,case_hash='foreign')})
    with pytest.raises(ValueError,match='reuse.*case'):
        await bad(job,{'results':{}})
