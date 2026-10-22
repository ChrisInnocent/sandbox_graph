import pytest
from sandbox_graph.interfaces.service import RunService,GraphService,QueryService
from sandbox_graph.planning.compiler import PlanCompiler,Case,Selection
from sandbox_graph.persistence.repository import Conflict


def test_preview_start_hash_auth_drift_and_idempotent_queue(repository):
    compiler=PlanCompiler([Case(capability='supported',adapter_verified=True,case_id='case',group_id='G_A',sandbox_type='synthetic',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'cpu':1},),conflict_domains=('host_A',),contract_ref='fixture:c')])
    service=RunService(repository,compiler)
    preview=service.preview(Selection(groups=('G_A',)),authorized_groups={'G_A'},evidence={'snapshot':'1'},graph_ref='graph:1',contract_ref='fixture:c')
    with pytest.raises(Conflict): service.start('req',preview.hash,authorized_groups={'G_A'},evidence={'snapshot':'changed'})
    run=service.start('req',preview.hash,authorized_groups={'G_A'},evidence={'snapshot':'1'})
    assert service.start('req',preview.hash,authorized_groups={'G_A'},evidence={'snapshot':'1'})==run
    assert QueryService(repository).status(run.run_id)['run']['active_plan_ref']==preview.hash
    with repository.transaction() as tx: assert len(tx.scan('jobs'))==1


def test_reanalysis_and_rerender_bind_exact_inputs_on_independent_threads(repository):
    from sandbox_graph.domain.models import RunRecord,canonical_hash
    from sandbox_graph.services.revisions import RevisionService
    run=RunRecord(run_id='r',root_thread_id='root',graph_ref='g',active_plan_ref='p',contract_ref='c',auth_ref='a',lineage_id='r')
    RevisionService(repository).create(run)
    with repository.transaction() as tx:
        tx.put('analysis_inputs','input',{'plan_hash':'p'},immutable=True)
        tx.put('analysis_results','analysis',{'run_id':'r','plan_ref':'p','analysis_input_ref':'input'},immutable=True)
    service=RunService(repository,PlanCompiler([]))
    service.reanalyze('recalc','r','input');service.rerender('render','r','analysis')
    with repository.transaction() as tx:
        jobs={j['body']['job_kind']:j['body'] for j in tx.scan('jobs')}
    assert jobs['analysis']['payload']['analysis_input_ref']=='input'
    assert jobs['report']['payload']['analysis_input_ref']=='input'
    assert jobs['report']['payload']['report_facts_ref']==canonical_hash({'analysis_ref':'analysis','report_version':1})
    assert jobs['analysis']['thread_id']!=jobs['report']['thread_id']!='root'


def test_stage_only_start_requires_exact_source_and_dispatches_no_measurement(repository):
    from sandbox_graph.domain.models import RunRecord
    from sandbox_graph.services.revisions import RevisionService
    service=RunService(repository,PlanCompiler([]))
    source_plan=service.preview(Selection(stage='all'),authorized_groups={'G_A'},graph_ref='g')
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='root',graph_ref='g',active_plan_ref=source_plan.hash,contract_ref='c',auth_ref='a',lineage_id='r'))
    with repository.transaction() as tx:
        tx.put('analysis_inputs','input',{'plan_hash':source_plan.hash,'deliveries':{'G_A':['d',1]}},immutable=True)
    evidence={'source_run_id':'r','analysis_input_ref':'input'}
    preview=service.preview(Selection(stage='analysis'),authorized_groups={'G_A'},evidence=evidence,graph_ref='g')
    run=service.start('only-analysis',preview.hash,authorized_groups={'G_A'},evidence=evidence)
    assert run.run_id=='r'
    with repository.transaction() as tx:
        jobs=tx.scan('jobs')
        assert len(jobs)==1 and jobs[0]['body']['job_kind']=='analysis'
        assert jobs[0]['body']['payload']['analysis_input_ref']=='input'
        assert jobs[0]['body']['payload']['plan_ref']==source_plan.hash


def test_status_exposes_deliveries_analysis_artifacts_and_archives(repository):
    with repository.transaction() as tx:
        tx.put('runs','r',{'active_plan_ref':'p','graph_ref':'g'})
        tx.put('work_instances','w1',{'work_id':'w1','run_id':'r'})
        tx.put('work_deliveries','d1:1',{'scope':'G_A','work_refs':['w1'],
            'result':{'output':{'case_index_ref':'G_A:abc'}},
            'evidence':[{'artifact_id':'G_A:abc'},{'artifact_id':'G_A:def'}]})
        tx.put('analysis_results','a1',{'run_id':'r','analysis_input_ref':'i1'})
        tx.put('analysis_exports','a1',{'files':{'facts.xlsx':{'artifact_id':'r:x'},'report.md':{'artifact_id':'r:y'}}})
        tx.put('archives','arch',{'run_id':'r','status':'partial','artifact_refs':['r:x']})
    status=QueryService(repository).status('r')
    assert status['deliveries'][0]['case_index_ref']=='G_A:abc'
    assert 'G_A:def' in status['deliveries'][0]['evidence']
    assert status['analyses'][0]['analysis_ref']=='a1'
    assert status['analyses'][0]['files']==['facts.xlsx','report.md']
    assert status['archives'][0]['status']=='partial'
