from datetime import datetime,timezone
import hashlib
import json
import pytest
from sandbox_graph.environment.bindings import verify_acceptance,scope_fingerprint
from sandbox_graph.environment.contracts import EnvironmentAcceptance,EnvironmentDeclaration
from tests.environment.unit.test_contracts import node
from sandbox_graph.domain.models import EvidenceRef


def inputs():
    declaration=EnvironmentDeclaration(environment_id='e',product='agentenv',product_version='v',profile='agentenv.native.v1',
        nodes=(node('n1',roles=('server',)),node('n2',roles=('server',),machine_id='machine-2')))
    acceptance=EnvironmentAcceptance(report_manifest_ref='artifact://report',declaration_hash=declaration.declaration_hash,
        product='agentenv',product_version='v',profile='agentenv.native.v1',scope=('n1',),credential_versions={'control':'v1'},
        valid_until='2026-10-25T00:00:00Z',invalidation_fingerprint='f'*64,
        functional_verification_refs=('artifact://functional',),cleanup_refs=('artifact://cleanup',))
    def resolve_evidence(ref):
        kind='functional' if ref=='artifact://functional' else 'cleanup'
        raw=json.dumps({'kind':kind,'declaration_hash':declaration.declaration_hash,'scope':['n1'],
            'status':'passed' if kind=='functional' else 'complete','resource_ids':['canary/1']}).encode()
        return EvidenceRef(artifact_id=kind,uri=kind,digest=hashlib.sha256(raw).hexdigest(),size=len(raw),
            media_type='application/json',scope='environment/e',producer='environment-check:1'),raw
    kwargs={'frozen_declaration':declaration,'current_declaration':declaration,'scope':('n1',),
        'current_fingerprint':'f'*64,'credential_versions':{'control':'v1'},
        'resolve_report':lambda ref:{'declaration_hash':declaration.declaration_hash,'readiness':{'n1':'ready','n2':'blocked'}},
        'now':datetime(2026,10,24,tzinfo=timezone.utc),'resolve_evidence':resolve_evidence,
        'evidence_scope':'environment/e','trusted_producers':('environment-check:1',)}
    return declaration,acceptance,kwargs


def test_only_selected_node_input_changes_invalidate_scope():
    decl,acceptance,kwargs=inputs()
    raw=decl.model_dump();raw['nodes'][1]['ssh_address']='192.0.2.11'
    changed=EnvironmentDeclaration.model_validate(raw)
    assert verify_acceptance(acceptance,**(kwargs|{'current_declaration':changed}))==acceptance
    raw['nodes'][0]['ssh_address']='192.0.2.12'
    with pytest.raises(ValueError):verify_acceptance(acceptance,**(kwargs|{'current_declaration':EnvironmentDeclaration.model_validate(raw)}))


@pytest.mark.parametrize('change',[{'current_fingerprint':'e'*64},{'credential_versions':{'control':'v2'}},
    {'scope':('n2',)},{'now':datetime(2026,10,26,tzinfo=timezone.utc)},
    {'resolve_report':lambda ref:{'readiness':{'n1':'ready'}}}])
def test_drift_stale_auth_expired_wrong_scope_or_unbound_report_is_rejected(change):
    _,acceptance,kwargs=inputs()
    with pytest.raises((ValueError,PermissionError)):verify_acceptance(acceptance,**(kwargs|change))


@pytest.mark.parametrize('fault',['missing','bytes','scope','producer','cleanup','unowned'])
def test_acceptance_cannot_use_missing_forged_or_foreign_cleanup_evidence(fault):
    _,acceptance,kwargs=inputs();original=kwargs['resolve_evidence']
    def altered(ref):
        proof,raw=original(ref)
        if fault=='missing':return None
        if fault=='bytes':return proof,b'changed'
        if fault=='scope':return proof.model_copy(update={'scope':'foreign'}),raw
        if fault=='producer':return proof.model_copy(update={'producer':'model'}),raw
        if ref=='artifact://cleanup':
            body=json.loads(raw)
            if fault=='cleanup':body['status']='unknown'
            if fault=='unowned':body['resource_ids']=['foreign']
            raw=json.dumps(body).encode();proof=proof.model_copy(update={'digest':hashlib.sha256(raw).hexdigest(),'size':len(raw)})
        return proof,raw
    with pytest.raises((ValueError,PermissionError)):
        verify_acceptance(acceptance,**(kwargs|{'resolve_evidence':altered}))


def test_multinode_acceptance_can_validate_unchanged_subset():
    from sandbox_graph.environment.acceptance import observed_fingerprint
    from sandbox_graph.environment.contracts import EnvironmentReport,CheckObservation
    decl,acceptance,kwargs=inputs()
    report=EnvironmentReport(declaration_hash=decl.declaration_hash,source_set=decl.source_set,profile=decl.profile,
        observations=tuple(CheckObservation(check_id='host',rule_ref='host.v1',node_id=node_id,component='host',
            status='satisfied',observed={'arch':'aarch64'},observed_at='2026-10-24T00:00:00Z',boot_id=node_id) for node_id in ('n1','n2')),
        readiness={'n1':'ready','n2':'ready'})
    acceptance=acceptance.model_copy(update={'scope':('n1','n2'),'invalidation_fingerprint':observed_fingerprint(report,('n1','n2'))})
    kwargs.update(resolve_report=lambda _:report.model_dump(mode='json'),current_fingerprint=observed_fingerprint(report,('n1',)))
    assert verify_acceptance(acceptance,**kwargs)==acceptance
    kwargs['current_fingerprint']='0'*64
    with pytest.raises(ValueError,match='drift'):verify_acceptance(acceptance,**kwargs)
