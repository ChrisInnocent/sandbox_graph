from copy import deepcopy
import pytest
from sandbox_graph.domain.evolution import CandidateRevision,GateManifest
from sandbox_graph.services.gate_policy import gate_plan
from sandbox_graph.services.gates import GateService
from sandbox_graph.services.agent_lifecycle import decode
from tests.orchestration.unit.test_evolution_models import candidate_data,gate_data


def profile():
    return {'profile_ref':'fixed-tests:1','suite_hash':'4'*64,'policy_hash':'5'*64,
        'cases':{gate:[gate+'/required'] for gate in ('G0','G1','G2','G3','G4','G6.pre')}}


def test_sensitive_diff_cannot_shrink_gates_or_accept_missing_profile_cases():
    candidate=CandidateRevision(**candidate_data(risk='R3'))
    plan=gate_plan(candidate,profile())
    assert plan['required_gates']==['G0','G1','G2','G3','G4','G6.pre']
    altered=profile();del altered['cases']['G3']
    with pytest.raises(ValueError):gate_plan(candidate,altered)
    with pytest.raises(ValueError):gate_plan(CandidateRevision(**candidate_data(risk='R0')),profile())


def setup(repository):
    candidate=CandidateRevision(**candidate_data(risk='R3'))
    fixed=profile();plan=gate_plan(candidate,fixed)
    body=gate_data();body.update(candidate_hash=candidate.candidate_hash,
        required_gates=plan['required_gates'],expected_case_ids=plan['expected_case_ids'],
        actual_case_ids=plan['expected_case_ids'],
        results=[{'schema_version':1,'gate_id':gate,'status':'pass','case_ids':ids,'log_hash':'8'*64} for gate,ids in fixed['cases'].items()],
        case_results=[{'schema_version':1,'case_id':case,'status':'pass','log_hash':'8'*64} for case in plan['expected_case_ids']],
        observed_at=1000.,expires_at=1050.,producer_identity='runner/trusted',producer_version='1')
    manifest=decode(GateManifest,body)
    with repository.transaction() as tx:
        token=tx.acquire_thread('gate/thread','trusted',100)
        tx.put('candidate_revisions',candidate.candidate_ref,candidate.model_dump(mode='json'),immutable=True)
        tx.put('gate_profiles','fixed-tests:1',fixed,immutable=True)
        tx.put('trusted_gate_producers','runner/trusted',{'version':'1','runner_ref':manifest.runner_ref,
            'runner_version':manifest.runner_version,'bundle_hash':manifest.runner_bundle_hash,
            'revoked':False,'expires_at':1100.},immutable=True)
        tx.put('gate_executions','gate/job',{'thread_id':token.thread_id,'candidate_ref':candidate.candidate_ref,
            'profile_ref':'fixed-tests:1','producer_identity':'runner/trusted','environment_ref':manifest.environment_ref,
            'environment_hash':manifest.environment_hash,'status':'running'},immutable=True)
    return candidate,manifest,token


def test_only_stored_authenticated_current_manifest_can_pass(repository):
    candidate,manifest,token=setup(repository)
    with repository.transaction() as tx:
        with pytest.raises(ValueError):GateService.evaluate_in(tx,manifest.manifest_ref,candidate)
        GateService.record_result_in(tx,'gate/job',manifest,producer_identity='runner/trusted',token=token)
        assert GateService.evaluate_in(tx,manifest.manifest_ref,candidate)['status']=='pass'


@pytest.mark.parametrize('fault',['producer','suite','case_missing','failed','skip','expiry','candidate','gate_missing'])
def test_forged_or_incomplete_results_never_become_promotion_proof(repository,fault):
    candidate,manifest,token=setup(repository)
    raw=manifest.model_dump(mode='json')
    if fault=='producer':raw['producer_identity']='model/self'
    if fault=='suite':raw['trusted_suite_hash']='e'*64
    if fault=='case_missing':
        raw['actual_case_ids']=raw['actual_case_ids'][:-1];raw['case_results']=raw['case_results'][:-1];raw['results']=raw['results'][:-1]
    if fault=='failed':raw['case_results'][0]['status']='fail'
    if fault=='skip':raw['case_results'][0]['status']='skipped'
    if fault=='expiry':raw['observed_at']=900.;raw['expires_at']=999.
    if fault=='candidate':raw['candidate_hash']='e'*64
    if fault=='gate_missing':raw['results']=raw['results'][:-1]
    altered=decode(GateManifest,raw)
    with repository.transaction() as tx:
        try:GateService.record_result_in(tx,'gate/job',altered,producer_identity='runner/trusted',token=token)
        except (ValueError,PermissionError):return
        assert GateService.evaluate_in(tx,altered.manifest_ref,candidate)['status']!='pass'


def test_later_producer_revocation_invalidates_previous_result(repository):
    candidate,manifest,token=setup(repository)
    with repository.transaction() as tx:
        GateService.record_result_in(tx,'gate/job',manifest,producer_identity='runner/trusted',token=token)
        producer=tx.get('trusted_gate_producers','runner/trusted')
        tx.put('trusted_gate_producers','runner/trusted',producer['body']|{'revoked':True},expected_revision=producer['revision'])
        with pytest.raises(PermissionError):GateService.evaluate_in(tx,manifest.manifest_ref,candidate)
