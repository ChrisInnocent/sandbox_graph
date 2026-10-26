import json
from langgraph.checkpoint.memory import InMemorySaver
import pytest
from sandbox_graph.runtime.deployment import EvaluationDeployment
from tests.benchmark.unit.test_casegen import complete_case


def runtime(tmp_path):
    raw=complete_case();raw['environment_ref']='environment-acceptance:test';raw['axes']={'concurrency':[1,2]}
    path=tmp_path/'case.json';path.write_text(json.dumps(raw))
    return {'schema_version':1,'graph_ref':'graph1','contract_ref':'contracthash','authorized_groups':['G_1'],
        'cases':[{'case_file':str(path),'config':{'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1'},
            'priority':'base','capability':'supported','adapter_verified':True,'conflict_domains':['host:test']}],
        'environment_runtime':{'references':{}},'session_root':str(tmp_path/'sessions'),'artifact_root':str(tmp_path/'artifacts')}

@pytest.mark.asyncio
async def test_deployment_expands_cases_and_durably_binds_deadline_before_graph_dispatch(repository,tmp_path):
    value=runtime(tmp_path);saver=InMemorySaver()
    deployed=EvaluationDeployment(repository,value,saver_factory=lambda token:saver)
    assert len(deployed.cases)==2
    async def check(context,cases):pass  # controlled boundary, not product verification
    deployed.gate.check=check
    plan=deployed.preview({});run=await deployed.start('start-eval',plan.hash,seconds=100)
    assert (await deployed.start('start-eval',plan.hash,seconds=100)).run_id==run.run_id
    with repository.transaction() as tx:
        binding=tx.get('evaluation_deployments',run.run_id)['body']
        assert binding['deadline']==1100.
    assert await deployed.worker.run_once()
    with repository.transaction() as tx:
        jobs=tx.scan('jobs')
        assert any(row['body']['job_kind']=='group' for row in jobs)
        assert all(row['body']['deadline']<=1100 for row in jobs)


def test_live_deployment_refuses_synthetic_environment_proof(repository,tmp_path):
    value=runtime(tmp_path);value['cases'][0]['config']['environment_evidence']={'evidence_kind':'synthetic'}
    with pytest.raises(ValueError,match='synthetic'):
        EvaluationDeployment(repository,value,saver_factory=lambda token:InMemorySaver())


def test_case_file_change_changes_frozen_deployment_identity(repository,tmp_path):
    value=runtime(tmp_path)
    first=EvaluationDeployment(repository,value,saver_factory=lambda token:InMemorySaver())
    path=__import__('pathlib').Path(value['cases'][0]['case_file'])
    raw=json.loads(path.read_text());raw['axes']['concurrency']=[1,3];path.write_text(json.dumps(raw))
    second=EvaluationDeployment(repository,value,saver_factory=lambda token:InMemorySaver())
    assert first.fingerprint!=second.fingerprint


@pytest.mark.asyncio
async def test_original_start_replay_does_not_reprobe_or_extend_deadline(repository,tmp_path):
    deployed=EvaluationDeployment(repository,runtime(tmp_path),saver_factory=lambda token:InMemorySaver())
    async def check(context,cases):pass
    deployed.gate.check=check
    plan=deployed.preview({});run=await deployed.start('start-eval',plan.hash,seconds=100)
    async def forbidden(*args):raise AssertionError('replay performed a fresh environment operation')
    deployed.gate.check=forbidden
    assert (await deployed.start('start-eval',plan.hash,seconds=100)).run_id==run.run_id
    with pytest.raises(Exception,match='request_id input changed'):
        await deployed.start('start-eval',plan.hash,seconds=101)


def test_report_child_retains_analysis_only_execution_deadline(repository,tmp_path):
    from types import SimpleNamespace
    deployed=EvaluationDeployment(repository,runtime(tmp_path),saver_factory=lambda token:InMemorySaver())
    with repository.transaction() as tx:
        tx.put('evaluation_deployments','old-run',{'runtime_hash':deployed.fingerprint,'deadline':900.})
        tx.put('jobs','new-report',{'job_kind':'report','run_id':'old-run','activation_id':'report/activation',
            'deadline':1200.,'payload':{'graph_ref':'graph1','plan_ref':'new-analysis-plan'}})
    activation=SimpleNamespace(run_id='old-run',activation_id='report/activation',graph_ref='graph1',plan_ref='new-analysis-plan')
    assert deployed.activation_deadline(activation)==1200.
    activation.plan_ref='foreign-plan'
    with pytest.raises(PermissionError):deployed.activation_deadline(activation)


def test_evaluation_recheck_uses_bootstrapped_helper_paths(repository,tmp_path,monkeypatch):
    import sandbox_graph.runtime.deployment as module
    original=module.SSHTransport;seen=[]
    def transport(**kwargs):
        seen.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(module,'SSHTransport',transport)
    value=runtime(tmp_path)
    value['environment_runtime']['helper_paths']={'node-a':'/opt/helper/bin/sandbox-graph-remote-agent'}
    module.EvaluationDeployment(repository,value,saver_factory=lambda token:InMemorySaver())
    assert seen==[{'helper_paths':value['environment_runtime']['helper_paths']}]
