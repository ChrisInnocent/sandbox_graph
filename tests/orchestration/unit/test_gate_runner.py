import json
import pytest
from sandbox_graph.domain.evolution import CandidateRevision
from sandbox_graph.domain.models import canonical_hash
from sandbox_graph.runtime.gate_runner import TrustedGateRunner, GateCase
from sandbox_graph.services.gates import GateService
from tests.orchestration.unit.test_trusted_gates import setup


def runner_for(repository,manifest,observe):
    cases={gate+'/required':GateCase('v1',observe) for gate in ('G0','G1','G2','G3','G4','G6.pre')}
    return TrustedGateRunner(repository,cases=cases,producer_identity=manifest.producer_identity,
        producer_version=manifest.producer_version,runner_ref=manifest.runner_ref,
        runner_version=manifest.runner_version,runner_bundle_hash=manifest.runner_bundle_hash)


@pytest.mark.asyncio
async def test_runner_writes_provenance_and_replays_without_reexecuting_cases(repository):
    candidate,manifest,token=setup(repository);calls=[]
    async def observation(context):
        calls.append(context.case_id)
        return {'status':'pass','observations':{'actual':2,'expected':2}}
    runner=runner_for(repository,manifest,observation)
    with repository.transaction() as tx:
        row=tx.get('gate_profiles','fixed-tests:1')
        tx.put('gate_profiles','fixed-tests:1',row['body']|{'suite_hash':runner.suite_hash})
    result=await runner.run('gate/job',token=token)
    assert result['status']=='pass' and len(calls)==6
    assert await runner.run('gate/job',token=token)==result and len(calls)==6


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['collect','exception','blocked','forged_pass','producer','expired'])
async def test_missing_or_untrusted_observations_do_not_pass(repository,fault):
    candidate,manifest,token=setup(repository)
    async def observation(context):
        if fault=='exception':raise RuntimeError('trusted case failed')
        if fault=='blocked':return {'status':'blocked','observations':{'reason':'postgres_missing'}}
        if fault=='forged_pass':return {'passed':True}
        return {'status':'pass','observations':{}}
    runner=runner_for(repository,manifest,observation)
    if fault=='collect':runner.cases.pop('G1/required')
    with repository.transaction() as tx:
        row=tx.get('gate_profiles','fixed-tests:1')
        tx.put('gate_profiles','fixed-tests:1',row['body']|{'suite_hash':runner.suite_hash})
        if fault=='producer':tx.put('trusted_gate_producers',manifest.producer_identity,{'revoked':True})
        if fault=='expired':
            producer=tx.get('trusted_gate_producers',manifest.producer_identity)
            tx.put('trusted_gate_producers',manifest.producer_identity,producer['body']|{'expires_at':999.})
    try:result=await runner.run('gate/job',token=token)
    except (PermissionError,ValueError):return
    assert result['status']!='pass'


@pytest.mark.asyncio
async def test_case_observation_preserved_and_failed_revision_not_overwritten(repository):
    candidate,manifest,token=setup(repository)
    async def observation(context):return {'status':'fail','observations':{'sandbox_output':{'pass':True},'expected':42}}
    runner=runner_for(repository,manifest,observation)
    with repository.transaction() as tx:
        row=tx.get('gate_profiles','fixed-tests:1')
        tx.put('gate_profiles','fixed-tests:1',row['body']|{'suite_hash':runner.suite_hash})
    assert (await runner.run('gate/job',token=token))['status']=='fail'
    with repository.transaction() as tx:
        rows=tx.scan('gate_case_observations')
        assert len(rows)==6 and all(row['body']['result']['status']=='fail' for row in rows)


def test_gate_suite_identity_binds_explicit_observer_configuration():
    async def observation(context):return {'status':'pass','observations':{}}
    a=GateCase('v1',observation,configuration={'max_rss':10})
    b=GateCase('v1',observation,configuration={'max_rss':20})
    assert a.identity()!=b.identity()
