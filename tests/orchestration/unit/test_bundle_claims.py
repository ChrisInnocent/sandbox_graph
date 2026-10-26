from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
import pytest


def install(repository):
    backend=TaskBackend(repository)
    backend.enqueue(JobSpec(job_id='job/new',job_kind='work',scope_ref='work/new',thread_id='thread/new',
        activation_id='activation/new',dedupe_key='new',due_at=1000.,deadline=1100.,run_id='run/new'))
    with repository.transaction() as tx:
        tx.put('dispatch_gates','run/new',{'open':True,'generation':1})
        tx.put('run_bundle_bindings','run/new',{'bundle_ref':'bundle/new','bundle_hash':'a'*64})
        tx.put('runs','run/new',{'graph_ref':'new:1'})
        tx.put('worker_registrations','worker/new',{'bundle_ref':'bundle/new','bundle_hash':'a'*64,
            'graph_ref':'new:1','status':'ready','expires_at':1100.})
    return backend


def test_only_matching_version_worker_can_claim_promoted_job(repository):
    backend=install(repository)
    assert backend.claim('old-worker',10) is None
    job=backend.claim('worker/new',10)
    assert job['job_id']=='job/new'
    with repository.transaction() as tx:TaskBackend.assert_claim_in(tx,job)


@pytest.mark.parametrize('fault',['expired','bundle','graph','disabled'])
def test_worker_capability_drift_is_rechecked_before_execution(repository,fault):
    backend=install(repository);job=backend.claim('worker/new',10)
    with repository.transaction() as tx:
        row=tx.get('worker_registrations','worker/new')
        update={'expired':{'expires_at':999.},'bundle':{'bundle_hash':'b'*64},
            'graph':{'graph_ref':'old:1'},'disabled':{'status':'draining'}}[fault]
        tx.put('worker_registrations','worker/new',row['body']|update)
        with pytest.raises(ValueError):TaskBackend.assert_claim_in(tx,job)
