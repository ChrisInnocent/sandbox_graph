"""Synthetic remote boundary; real worker, graphs, receipts, formulas and exports."""
import json
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from tests.orchestration.conftest import MemoryRepository,FakeClock
from tests.benchmark.unit.test_contract_models import sample
from tests.benchmark.unit.test_session import Scenario
from sandbox_bench.session import BenchSession
from sandbox_bench.load import OperationResult
from sandbox_bench.bindings import RegisteredBenchmarks
from sandbox_bench.bridge import BenchGateway,work_handlers,delivery_finalizer
from sandbox_bench.workflow_services import AnalysisReportServices
from sandbox_graph.domain.models import ScriptRef,EvidenceRef
from sandbox_graph.planning.compiler import Case,Selection,PlanCompiler
from sandbox_graph.interfaces.service import RunService
from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.services.reviews import ReviewService
from sandbox_graph.services.environment import EnvironmentHandlers,EnvironmentSnapshot
from sandbox_graph.runtime.runner import OperationService,RunnerStatus
from sandbox_graph.runtime.bootstrap import build_job_handlers
from sandbox_graph.runtime.worker import Worker,WorkerPolicy


class EnvironmentFixture:
    def __init__(self): self.configured=set();self.calls=[]
    def snapshot(self,scope,ref):
        return EnvironmentSnapshot(scope=scope,connected=True,authenticated=True,identity_confirmed=True,
            state='satisfied' if scope in self.configured else 'drift',source_ref=ref,
            differences=() if scope in self.configured else ('fixture configuration absent',))
    async def connect(self,scope,ref):self.calls.append(('connect',scope));return self.snapshot(scope,ref)
    async def inspect(self,scope,ref):self.calls.append(('inspect',scope));return self.snapshot(scope,ref)
    async def submit(self,operation):
        self.calls.append(('configure',operation.scope));self.configured.add(operation.scope)
        return RunnerStatus(state='completed',authoritative=True)


class FailedScenario(Scenario):
    async def execute(self,index):
        self.calls.append(index)
        return OperationResult('failed',value={'operation_id':str(index),'status':'failed','elapsed_s':.1})


@pytest.mark.asyncio
async def test_two_groups_flow_through_worker_real_sessions_and_partial_archive(tmp_path):
    repo=MemoryRepository(FakeClock());store=FileArtifactStore(tmp_path/'artifacts');reviews=ReviewService(repo,store)
    bench_script=ScriptRef(script_id='benchmark.step',version='1',digest='a'*64)
    config_script=ScriptRef(script_id='fixture.configure',version='1',digest='b'*64)
    gateway=BenchGateway({},bench_script);environment=EnvironmentFixture()
    operations=OperationService(repo,gateway,{('benchmark.step','1'):bench_script})
    configs=OperationService(repo,environment,{('fixture.configure','1'):config_script})
    cases=[];templates={};scenarios={}
    for group in ('G_A','G_B'):
        body=sample();body.update(group_id=group,environment_ref='fixture:'+group,work_id='template:'+group)
        config={'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1',
            'source':{'kind':body['source']['kind'],'id':body['source']['id']},
            'create_body':{'timeout':100,'metadata':{'kuasar-sandbox.resource':'{"capacity":{"cpu":2,"memory":"2GiB"}}'}},
            'source_evidence':{'source_id':body['source']['id'],'source_digest':body['source']['digest'],
                'environment_ref':body['environment_ref'],'cache_profile':body['cache_profile'],
                'effective_spec':body['spec'],'evidence_ref':'synthetic-source'},
            'environment_evidence':{'ref':body['environment_ref'],'product_version':body['product_version'],'evidence_kind':'synthetic'}}
        templates[group]={'case':body,'config':config}
        cases.append(Case(capability='supported',adapter_verified=True,case_id=group,group_id=group,sandbox_type='kuasar',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'case_template_ref':group},),conflict_domains=('host:'+group,),contract_ref='contracthash',environment_ref=body['environment_ref']))
    def session_factory(case,config,folder):
        scenario=Scenario() if case.group_id=='G_A' else FailedScenario();scenarios[case.group_id]=scenario
        return BenchSession(case,config,folder,scenario=scenario)
    registry=RegisteredBenchmarks(repo,gateway,templates,tmp_path/'sessions',session_factory=session_factory)
    analysis=AnalysisReportServices(repo,reviews,store,deadline=1200)
    def services(token,activation):
        mapping=EnvironmentHandlers(repo,environment,deadline=1200,operation_service=configs,configuration_script=config_script,configuration_verified=True).mapping()
        mapping.update(analysis.mapping());mapping['T2']=registry.validate_conditions
        if activation.scope in gateway.sessions:
            mapping.update(work_handlers(operations,token,registry.session(activation.scope),run_id=activation.run_id,scope=activation.scope,dispatch_generation=1,deadline=1200))
        return mapping
    saver=InMemorySaver()
    handlers=build_job_handlers(repo,graph_ref='graph1',saver_factory=lambda token:saver,node_service_factory=services,review_service=reviews,deadline=1200,lease_seconds=30,segment_seconds=20,delivery_finalizer=delivery_finalizer(gateway.sessions,store),operation_service=operations,max_work_concurrency=2)
    worker=Worker(repo,handlers,policy=WorkerPolicy(claim_seconds=30,job_seconds=25,poll_seconds=1))
    api=RunService(repo,PlanCompiler(cases));plan=api.preview(Selection(),authorized_groups={'G_A','G_B'},graph_ref='graph1',contract_ref='contracthash');run=api.start('start',plan.hash,authorized_groups={'G_A','G_B'})
    for tick in range(35):
        if not await worker.run_once(): break
        repo.clock.advance(.001)
    else:pytest.fail('worker never became idle')
    worker.pump_outbox()
    with repo.transaction() as tx:
        jobs=[r['body'] for r in tx.scan('jobs')]
        archives=tx.scan('archives');groups=tx.scan('group_deliveries')
        assert archives,([r['body']['result'] for r in tx.scan('receipts') if r['body']['identity']['node_id']=='F4'],tx.scan('resource_leases'))
        assert all(row['body']['status']=='partial' for row in archives)
        assert len(groups)==2 and {g['body']['scope'] for g in groups}=={'G_A','G_B'}
        records=[r['body'] for r in tx.scan('analysis_results')]
        assert records
        exports=tx.scan('analysis_exports')
        assert all('facts.xlsx' in row['body']['files'] for row in exports)
        frozen=reviews.get_analysis(records[-1]['analysis_input_ref'])
        assert frozen['delivery_kind']=='group_deliveries'
    assert scenarios['G_A'].calls==[0,1,2,3,4] and scenarios['G_B'].calls==[0,1,2,3,4]
    assert environment.calls.count(('configure','G_A'))==environment.calls.count(('configure','G_B'))==1
    assert all(environment.calls.count(('inspect',group))>=2 for group in ('G_A','G_B'))
    facts=[json.loads(store.path_for(EvidenceRef.model_validate(record['facts_ref'])).read_bytes()) for record in records]
    complete_inputs=[item for item in facts if len(item['cases'])==2]
    assert complete_inputs
    assert sorted(case['success_samples'] for case in complete_inputs[0]['cases'])==[0,5]
    assert complete_inputs[0]['pairs'][0]['status']=='incomparable'
    assert complete_inputs[0]['conclusions']==[]
    # Every operation is a registered configuration or benchmark step; reporting
    # never introduces a second experiment despite repeated root activations.
    assert len(gateway.sessions)==2
