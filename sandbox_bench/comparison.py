"""Deterministic, evidence-cited comparison of verified immutable case exports.

No observations are pooled across cases or clock domains. Percentiles stay in
individual case/repeat strata. Interpretations are optional unverified prose,
never an input to facts, eligibility, or performance conclusions.
"""
from datetime import datetime
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from .analysis import verify_case

_FIELDS=('product','product_version','method_id','method_version','completion',
         'spec','cache_profile','condition','window')
_MATCH=('method_id','method_version','completion','spec','cache_profile','condition','window')


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _numbers(value,path=''):
    if isinstance(value,dict):
        for key in sorted(value):
            escaped=str(key).replace('~','~0').replace('/','~1')
            yield from _numbers(value[key],path+'/'+escaped)
    elif isinstance(value,list):
        for index,item in enumerate(value):
            yield from _numbers(item,path+'/'+str(index))
    elif type(value) in (int,float):
        yield path,value


def compare_cases(case_directories,*,analysis_input_ref=None,expected_manifests=None,case_aliases=None):
    """Load only verified seals; retain measurements even when incomparable.

    Sealed metadata.comparison must declare product/product_version, method_id/
    method_version, completion, spec, cache_profile, condition and window.
    condition/window are comparison contracts, not absolute monotonic timestamps.
    Missing contracts never imply equal conditions. Product versions must match
    within one product; cross-product comparisons retain both explicit versions.
    """
    if analysis_input_ref is not None and (not isinstance(analysis_input_ref,str) or not analysis_input_ref):
        raise ValueError('analysis_input_ref must be a nonempty frozen identity')
    if expected_manifests is not None and not isinstance(expected_manifests,dict):
        raise ValueError('expected_manifests must map case identities to manifest digests')
    cases=[];facts=[];seen=set()
    for directory in case_directories:
        root=Path(directory)
        manifest=verify_case(root)
        case_id=manifest['case_id']
        if case_id in seen:raise ValueError('duplicate case identity in comparison')
        seen.add(case_id)
        checksum=_digest((root/'case_manifest.json').read_bytes())
        if expected_manifests is not None and expected_manifests.get(case_id)!=checksum:
            raise ValueError('case manifest differs from frozen analysis input')
        summary=json.loads((root/'summary.json').read_bytes())
        descriptor=manifest['metadata'].get('comparison',{})
        if not isinstance(descriptor,dict):raise ValueError('comparison metadata must be an object')
        missing=[key for key in _FIELDS if descriptor.get(key) in (None,'',{},[])]
        qualification=summary.get('qualifications',{}).get('status','unverified')
        repeats=summary.get('latency',{}).get('repeats',{})
        success_count=sum(repeat.get('n_success',0) for repeat in repeats.values())
        cases.append({'case_id':case_id,'manifest_sha256':checksum,'comparison':descriptor,
                      'missing_contract_fields':missing,'qualification':qualification,
                      'success_samples':success_count,'repeat_ids':sorted(repeats),'summary':summary})
        for path,value in _numbers(summary):
            # Claims and input specifications are evidence, not measured metrics.
            if path.startswith('/qualifications/') or '/comparability/' in path:continue
            facts.append({'fact_id':_digest(_json([checksum,path])), 'case_id':case_id,
                          'manifest_sha256':checksum,'artifact':'summary.json',
                          'metric_path':path,'value':value,'qualification':qualification})
    if expected_manifests is not None and set(expected_manifests)!=seen:
        raise ValueError('frozen analysis input case set is incomplete')
    aliases={}
    if case_aliases is not None:
        if not isinstance(case_aliases,dict):
            raise ValueError('case aliases must map logical work identities to measured cases')
        for work,row in sorted(case_aliases.items()):
            if (not isinstance(work,str) or not work or not isinstance(row,dict)
                    or set(row)!={'case_id','source_delivery_ref','projection_ref'}
                    or row['case_id'] not in seen
                    or not isinstance(row['source_delivery_ref'],str) or not row['source_delivery_ref']
                    or (row['projection_ref'] is not None and
                        (not isinstance(row['projection_ref'],str) or not row['projection_ref']))):
                raise ValueError('invalid case alias or unknown measured case')
            aliases[work]=dict(row)
    cases.sort(key=lambda case:case['case_id'])
    facts.sort(key=lambda fact:(fact['case_id'],fact['metric_path']))
    pairs=[]
    for left,right in itertools.combinations(cases,2):
        reasons=['missing:'+key for key in sorted(set(left['missing_contract_fields']+right['missing_contract_fields']))]
        a,b=left['comparison'],right['comparison']
        for key in _MATCH:
            if key not in left['missing_contract_fields']+right['missing_contract_fields'] and a[key]!=b[key]:
                reasons.append('mismatch:'+key)
        if a.get('product')==b.get('product') and not {'product_version'} & set(left['missing_contract_fields']+right['missing_contract_fields']):
            if a.get('product_version')!=b.get('product_version'):reasons.append('mismatch:product_version')
        for case in (left,right):
            if case['qualification']!='valid':reasons.append('unqualified:'+case['case_id'])
            if not case['success_samples']:reasons.append('no_success_samples:'+case['case_id'])
        pairs.append({'left':left['case_id'],'right':right['case_id'],
                      'status':'incomparable' if reasons else 'comparable','reasons':reasons})
    strata={}
    for case in cases:
        descriptor=case['comparison']
        key=(str(descriptor.get('product')),str(descriptor.get('product_version')))
        strata.setdefault(key,[]).append({'case_id':case['case_id'],'repeat_ids':case['repeat_ids']})
    return {'schema_version':'comparison_facts.v1','analysis_input_ref':analysis_input_ref,
            'status':'measured_facts' if facts else 'no_data',
            'cases':cases,'facts':facts,'pairs':pairs,'case_aliases':aliases,
            'product_strata':[{'product':key[0],'product_version':key[1],'cases':value}
                              for key,value in sorted(strata.items())],
            'conclusions':[]}


def _cell(value):
    return str(value).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('|','&#124;').replace('\n',' ')


def factual_report(facts):
    """Render measured values and eligibility; never invent a performance verdict."""
    lines=['# Benchmark measured facts','',
           'Each value belongs to its case and repeat. Percentiles are not averaged across repeats.',
           'No performance ranking or causal conclusion is asserted.','',
           '| Case | Qualification | Manifest SHA256 |','| --- | --- | --- |']
    for case in facts['cases']:
        lines.append('| '+' | '.join(_cell(case[key]) for key in ('case_id','qualification','manifest_sha256'))+' |')
    if not facts['cases']:lines.extend(['','No verified case data supplied.'])
    if facts.get('case_aliases'):
        lines.extend(['','Each alias references an original measurement; reuse does not add samples.','',
                      '| Selected work | Measured case | Source delivery | Reuse projection |',
                      '| --- | --- | --- | --- |'])
        for work,row in facts['case_aliases'].items():
            lines.append('| '+' | '.join(_cell(v) for v in
                (work,row['case_id'],row['source_delivery_ref'],row['projection_ref'] or 'direct'))+' |')
    lines.extend(['','| Left | Right | Comparability | Reasons |','| --- | --- | --- | --- |'])
    for pair in facts['pairs']:
        lines.append('| '+' | '.join(_cell(value) for value in (pair['left'],pair['right'],pair['status'],', '.join(pair['reasons'])))+' |')
    lines.extend(['','| Case | Metric JSON pointer | Value | Evidence |','| --- | --- | --- | --- |'])
    for fact in facts['facts']:
        citation=f"sha256:{fact['manifest_sha256']}/{fact['artifact']}#{fact['metric_path']}"
        lines.append('| '+' | '.join(_cell(value) for value in (fact['case_id'],fact['metric_path'],fact['value'],citation))+' |')
    return '\n'.join(lines)+'\n'


def _interpretation(layer,facts):
    if layer is None:return None
    if not isinstance(layer,list):raise ValueError('interpretation must be a list of cited prose records')
    allowed={fact['fact_id'] for fact in facts['facts']}
    for item in layer:
        if (not isinstance(item,dict) or set(item)!={'text','fact_refs'}
                or not isinstance(item['text'],str) or not item['text'].strip()
                or not isinstance(item['fact_refs'],list) or not item['fact_refs']
                or any(not isinstance(ref,str) or ref not in allowed for ref in item['fact_refs'])):
            raise ValueError('interpretation requires nonempty prose and existing fact references only')
    return {'status':'unverified_model_interpretation','claims':layer,
            'constraint':'Citations establish traceability, not the truth of prose; this layer cannot alter measured facts.'}


def _excel(facts):
    try:import openpyxl
    except ImportError as exc:
        raise RuntimeError('Excel export requires optional dependency openpyxl; JSON and Markdown need no dependency') from exc
    workbook=openpyxl.Workbook();workbook.remove(workbook.active)
    sheets={'facts':(['case_id','metric_path','value','qualification','manifest_sha256','fact_id'],facts['facts']),
            'cases':(['case_id','qualification','success_samples','manifest_sha256','comparison'],facts['cases']),
            'comparability':(['left','right','status','reasons'],facts['pairs'])}
    if facts.get('case_aliases'):
        sheets['coverage']=(['work_id','case_id','source_delivery_ref','projection_ref'],
                            [{'work_id':work,**row} for work,row in facts['case_aliases'].items()])
    for title,(columns,rows) in sheets.items():
        sheet=workbook.create_sheet(title);sheet.append(columns);sheet.freeze_panes='A2'
        for record in rows:
            values=[record.get(key) for key in columns]
            sheet.append([_json(value).decode() if isinstance(value,(dict,list)) else value for value in values])
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value,str):cell.data_type='s'
        sheet.auto_filter.ref=sheet.dimensions
    workbook.properties.created=workbook.properties.modified=datetime(2000,1,1)
    buffer=io.BytesIO();workbook.save(buffer)
    # Canonical ZIP timestamps remove wall-clock differences from identical exports.
    output=io.BytesIO()
    with zipfile.ZipFile(buffer) as source,zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as target:
        for name in sorted(source.namelist()):
            info=zipfile.ZipInfo(name,date_time=(2000,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            data=source.read(name)
            if name=='docProps/core.xml':
                data=re.sub(rb'(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)',
                            rb'\g<1>2000-01-01T00:00:00Z\g<2>',data)
            target.writestr(info,data)
    return output.getvalue()


def export_comparison(case_directories,destination,*,xlsx=False,interpretation=None,
                      analysis_input_ref=None,expected_manifests=None,case_aliases=None):
    """Atomically publish JSON/factual Markdown and optional Excel/interpretation."""
    facts=compare_cases(case_directories,analysis_input_ref=analysis_input_ref,
                        expected_manifests=expected_manifests,case_aliases=case_aliases)
    layer=_interpretation(interpretation,facts)
    files={'facts.json':_json(facts),'report.md':factual_report(facts).encode()}
    if layer is not None:files['interpretation.json']=_json(layer)
    if xlsx:files['facts.xlsx']=_excel(facts)
    manifest={'schema_version':'comparison_export.v1','analysis_input_ref':analysis_input_ref,
              'sources':[{'case_id':case['case_id'],'manifest_sha256':case['manifest_sha256']} for case in facts['cases']],
              'files':{name:{'sha256':_digest(data),'size_bytes':len(data)} for name,data in files.items()}}
    files['comparison_manifest.json']=_json(manifest)
    target=Path(destination)
    if target.exists() or target.is_symlink():raise FileExistsError(str(target))
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=Path(tempfile.mkdtemp(prefix='.comparison-',dir=target.parent))
    try:
        for name,data in files.items():
            with (temp/name).open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
        import fcntl
        with (target.parent/'.comparison-export.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if target.exists() or target.is_symlink():raise FileExistsError(str(target))
            os.rename(temp,target)
            fd=os.open(target.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        return manifest
    finally:
        if temp.exists():shutil.rmtree(temp)
