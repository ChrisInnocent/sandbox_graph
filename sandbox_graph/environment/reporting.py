"""Immutable environment manifests and deterministic Markdown/XLSX projections."""
import io
import json
import re
import zipfile
from .evidence import SecretRedactor,assert_public_payload_safe
from .contracts import EnvironmentReport,StepReceipt
from ..domain.models import EvidenceRef


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def _cell(value):
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,sort_keys=True)
    return "'"+text if text.startswith(('=','+','-','@','\t','\r')) else text


def _stable_xlsx(payload):
    """ZIP and document timestamps are rendering details, not new evidence."""
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source,zipfile.ZipFile(output,'w') as target:
        for name in sorted(source.namelist()):
            data=source.read(name)
            if name=='docProps/core.xml':
                data=re.sub(rb'(<dcterms:(?:created|modified)[^>]*>)[^<]+',
                    rb'\g<1>2000-01-01T00:00:00Z',data)
            item=zipfile.ZipInfo(name,date_time=(2000,1,1,0,0,0));item.compress_type=zipfile.ZIP_DEFLATED
            target.writestr(item,data)
    return output.getvalue()


def publish_report(report,*,store,scope,resolve,receipts=(),secret_values=(),xlsx=False):
    report=EnvironmentReport.model_validate(report)
    receipts=tuple(StepReceipt.model_validate(row) for row in receipts)
    references=set(report.artifact_manifest)|set(report.cleanup_refs)
    references.update(ref for row in report.observations for ref in row.evidence_refs)
    for receipt in receipts:
        references.add(receipt.plan_ref)
        references.update(receipt.output_refs);references.update(receipt.evidence_refs)
    resolved={}
    for ref in sorted(references):
        actual=resolve(ref)
        if actual is None:raise ValueError('environment evidence reference is missing')
        actual=EvidenceRef.model_validate(actual)
        if actual.scope!=scope:raise PermissionError('environment evidence scope differs')
        if ref!='artifact://'+actual.uri:raise ValueError('environment reference resolved to another identity')
        store.verify(actual)
        path=store.path_for(actual)
        if actual.media_type=='application/json':
            if actual.size>16*1024*1024:raise ValueError('public JSON evidence exceeds inspection byte budget')
            assert_public_payload_safe(json.loads(path.read_bytes()),secret_values=secret_values)
        else:
            # Inspect large raw logs without copying them into model context.
            overlap=max([256,*[len(value.encode()) for value in secret_values]])
            carry=b''
            with path.open('rb') as stream:
                while block:=stream.read(65536):
                    chunk=carry+block
                    assert_public_payload_safe(chunk.decode('utf-8',errors='replace'),secret_values=secret_values)
                    carry=chunk[-overlap:]
        resolved[ref]=actual.model_dump(mode='json')
    body={'schema_version':'environment_manifest.v1','report':report.model_dump(mode='json'),
        'steps':[row.model_dump(mode='json') for row in receipts],
        'installation_history':'recorded_steps' if receipts else 'unavailable','evidence':resolved}
    body=SecretRedactor(secret_values).redact(body)
    assert_public_payload_safe(body,secret_values=secret_values)
    def commit(data,media_type):
        return store.commit([data],scope=scope,media_type=media_type,producer='environment-report.v1')
    result={'report':commit(_json(body),'application/json')}
    columns=('node','component','check','status','observed','evidence')
    rows=[columns]+[(row['node_id'],row['component'],row['check_id'],row['status'],row['observed'],
        ', '.join(row['evidence_refs'])) for row in body['report']['observations']]
    def markdown_cell(value):return _cell(value).replace('|','\\|').replace('\n','<br>')
    md=['Environment report','',f"Profile: {body['report']['profile']}",f"Declaration: {report.declaration_hash}",
        'Installation history: '+body['installation_history'],'',
        '| '+' | '.join(columns)+' |','| '+' | '.join('---' for _ in columns)+' |']
    md.extend('| '+' | '.join(markdown_cell(value) for value in row)+' |' for row in rows[1:])
    md.extend(['','Readiness: '+json.dumps(body['report']['readiness'],ensure_ascii=False),
        'Limitations: '+json.dumps(body['report']['limitations'],ensure_ascii=False)])
    result['markdown']=commit(('\n'.join(md)+'\n').encode(),'text/markdown')
    if xlsx:
        from openpyxl import Workbook
        workbook=Workbook();sheet=workbook.active;sheet.title='environment'
        for row in rows:sheet.append([_cell(value) for value in row])
        steps=workbook.create_sheet('steps')
        steps.append(['step_id','result','effect_certainty','plan_ref','output_refs'])
        for row in body['steps']:
            steps.append([_cell(row[key]) for key in ('step_id','result','effect_certainty','plan_ref','output_refs')])
        buffer=io.BytesIO();workbook.save(buffer)
        result['xlsx']=commit(_stable_xlsx(buffer.getvalue()),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    result['manifest']=commit(_json({key:value.model_dump(mode='json') for key,value in result.items()}),'application/json')
    return result


def verify_manifest(manifest,*,store):
    declared=json.loads(store.path_for(manifest['manifest']).read_bytes())
    store.verify(manifest['manifest'])
    if set(declared)!=set(manifest)-{'manifest'}:raise ValueError('environment manifest projection set differs')
    for key,raw in declared.items():
        if EvidenceRef.model_validate(raw)!=manifest[key]:raise ValueError('environment projection identity mismatch')
        store.verify(manifest[key])
    body=json.loads(store.path_for(manifest['report']).read_bytes())
    for raw in body['evidence'].values():store.verify(EvidenceRef.model_validate(raw))
    return body
