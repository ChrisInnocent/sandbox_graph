import json
import pytest
from sandbox_graph.environment.reporting import publish_report,verify_manifest
from sandbox_graph.environment.contracts import EnvironmentReport,CheckObservation
from sandbox_graph.artifacts.store import FileArtifactStore


def report(ref):
    return EnvironmentReport(declaration_hash='a'*64,source_set=('AgentENV=source',),profile='agentenv.native.v1',
        observations=(CheckObservation(check_id='server',rule_ref='product.probe.v1',node_id='n',component='server',
            status='missing',observed={'result':'=HYPERLINK("https://example.invalid")'},observed_at='2026-10-24T00:00:00Z',
            evidence_refs=(ref,)),),readiness={'n':'blocked'},artifact_manifest=(ref,))


def test_report_keeps_failed_nodes_links_and_safe_excel_cells(tmp_path,monkeypatch):
    import openpyxl
    store=FileArtifactStore(tmp_path)
    evidence=store.commit([b'{"status":"missing"}'],scope='environment-e',media_type='application/json',producer='probe')
    ref='artifact://'+evidence.uri
    manifest=publish_report(report(ref),store=store,scope='environment-e',resolve=lambda x:evidence,xlsx=True)
    verify_manifest(manifest,store=store)
    payload=json.loads(store.path_for(manifest['report']).read_bytes())
    assert payload['report']['readiness']=={'n':'blocked'}
    assert payload['installation_history']=='unavailable'
    assert payload['evidence'][ref]['digest']==evidence.digest
    workbook=openpyxl.load_workbook(store.path_for(manifest['xlsx']).open('rb'))
    assert all(cell.data_type!='f' for row in workbook.active for cell in row)
    assert 'missing' in store.path_for(manifest['markdown']).read_text()
    monkeypatch.setattr('zipfile.time.localtime',lambda *args:(2001,1,1,0,0,0,0,1,0))
    repeated=publish_report(report(ref),store=store,scope='environment-e',resolve=lambda x:evidence,xlsx=True)
    assert repeated['xlsx'].digest==manifest['xlsx'].digest


def test_report_rejects_missing_or_wrong_scope_evidence_before_publish(tmp_path):
    store=FileArtifactStore(tmp_path)
    evidence=store.commit([b'foreign'],scope='other',media_type='text/plain')
    with pytest.raises(PermissionError):
        publish_report(report('artifact://'+evidence.uri),store=store,scope='environment-e',resolve=lambda x:evidence)
    with pytest.raises(ValueError):
        publish_report(report('artifact://missing'),store=store,scope='environment-e',resolve=lambda x:None)


def test_report_redacts_known_secret_in_all_formats(tmp_path):
    store=FileArtifactStore(tmp_path)
    evidence=store.commit([b'{}'],scope='environment-e',media_type='application/json')
    raw=report('artifact://'+evidence.uri).model_dump()
    secret='sensitive-credential-value'
    raw['observations'][0]['observed']={'message':secret,'Authorization':secret}
    manifest=publish_report(EnvironmentReport.model_validate(raw),store=store,scope='environment-e',resolve=lambda x:evidence,
        secret_values=(secret,),xlsx=True)
    for key in ('report','markdown'):
        assert secret.encode() not in store.path_for(manifest[key]).read_bytes()


def test_public_manifest_rejects_unsanitized_referenced_evidence(tmp_path):
    store=FileArtifactStore(tmp_path)
    evidence=store.commit([b'{"envdAccessToken":"must-stay-private"}'],scope='environment-e',media_type='application/json')
    with pytest.raises(ValueError,match='sensitive'):
        publish_report(report('artifact://'+evidence.uri),store=store,scope='environment-e',resolve=lambda _:evidence)
