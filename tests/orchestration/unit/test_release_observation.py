import pytest
from sandbox_graph.domain.models import RunRecord


def seeded(repository):
    run=RunRecord(run_id='successor',root_thread_id='thread',graph_ref='g:2',contract_ref='c',active_plan_ref='p',auth_ref='a',lineage_id='lineage',supersedes_run_id='old')
    with repository.transaction() as tx:
        tx.put('runs',run.run_id,run.model_dump(mode='json'))
        tx.put('dispatch_gates',run.run_id,{'open':True,'generation':1})
        tx.put('promotion_records','promotion',{'result':{'status':'activated','run_ref':'successor','candidate_ref':'candidate/1'},'request':{'candidate_hash':'a'*64}})
        tx.put('run_bundle_bindings',run.run_id,{'promotion_ref':'promotion','bundle_ref':'bundle','bundle_hash':'b'*64,'candidate_ref':'candidate/1'})
        tx.put('plans','p',{'stage':'all'})
    return run


def test_release_observer_requires_actual_completed_activation_and_final_receipt(repository):
    from sandbox_graph.services.release_observation import ReleaseObservationService
    seeded(repository)
    with repository.transaction() as tx:
        assert ReleaseObservationService.observe_in(tx,'successor')['status']=='pending'
        tx.put('receipts','fake',{'identity':{'run_ref':'successor','graph_ref':'g:2','plan_ref':'p','node_id':'F4','activation_id':'unknown'},'result':{'execution_status':'succeeded'}})
        assert ReleaseObservationService.observe_in(tx,'successor')['status']=='pending'
        tx.put('activations','done',{'status':'complete','record':{'run_id':'successor','graph_ref':'g:2','plan_ref':'p'}})
        tx.put('receipts','final',{'identity':{'run_ref':'successor','graph_ref':'g:2','plan_ref':'p','node_id':'F4','activation_id':'done'},'result':{'execution_status':'succeeded','cleanup_status':'complete'}})
        tx.put('jobs','report',{'run_id':'successor','job_kind':'report','status':'complete'})
        assert ReleaseObservationService.observe_in(tx,'successor')['status']=='pass'


def test_release_failure_stops_new_dispatch_and_preserves_original_safety_jobs(repository):
    from sandbox_graph.services.release_observation import ReleaseObservationService
    seeded(repository)
    with repository.transaction() as tx:
        tx.put('jobs','work',{'run_id':'successor','job_kind':'work','status':'failed','scope_ref':'work','job_id':'work','result':{'execution_status':'failed'}})
        tx.put('jobs','cleanup',{'run_id':'successor','job_kind':'cleanup','status':'pending'})
        result=ReleaseObservationService.observe_in(tx,'successor')
        assert result['status']=='fail' and result['failure_refs']==['work']
        assert tx.get('dispatch_gates','successor')['body']['open'] is False
        assert tx.get('jobs','cleanup')['body']['status']=='pending'
        assert len(tx.scan('run_stop_requests'))==1
        assert ReleaseObservationService.observe_in(tx,'successor')==result
