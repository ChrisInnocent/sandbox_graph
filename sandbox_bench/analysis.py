"""Deterministic offline analysis and immutable, checksum-verified case export."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from .metrics import (summarize_repeats,summarize_quality,throughput,
                      cpu_segments,cpu_summary,resource_mean)

ARRAYS=('operations','events','resource_series','cohorts','assertions','cleanup_ledger','raw_refs')


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def _normalized(records):
    value=dict(records)
    if not isinstance(value.get('case_id'),str) or not value['case_id']:
        raise ValueError('case_id required')
    for name in ARRAYS:
        rows=value.get(name,[])
        if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
            raise ValueError(f'{name} must be a list of records')
        explicit=any('seq' in row for row in rows)
        normalized=[];expected=1
        for row in rows:
            seq=row.get('seq') if explicit else expected
            if type(seq) is not int or seq!=expected:
                raise ValueError(f'{name} sequence is missing, duplicated, or reordered')
            last=seq
            if row.get('kind')=='sample_gap':
                last=row.get('through_seq',seq)
                if (type(last) is not int or last<seq
                        or row.get('dropped_count')!=last-seq+1):
                    raise ValueError('invalid declared sample gap range')
            normalized.append(dict(row,seq=seq))
            expected=last+1
        value[name]=normalized
    return value


def _process_key(process):
    return json.dumps(process.get('expected_identity') or process.get('identity'),sort_keys=True,separators=(',',':'))


def _resource_rows(records):
    """Flatten snapshots; missing identities and cohort members break coverage."""
    result=[];segment=0
    expected={_process_key(p) for row in records['resource_series'] if row.get('kind')=='resource_sample' for p in row['processes']}
    for row in records['resource_series']:
        if row.get('kind')=='sample_gap':segment+=1;continue
        if row.get('kind')=='resource_sample':
            total=0;complete=row.get('inventory_complete',True);cohort=set();seen=set()
            for process in row['processes']:
                key=_process_key(process)
                if key in seen:raise ValueError('duplicate process identity in resource snapshot')
                seen.add(key)
                identity=[process.get('expected_identity') or process.get('identity'),process.get('sandbox_id'),process.get('generation')]
                result.append({'kind':'cpu','t_s':process.get('ended_s',row['t_s']),
                               'cpu_seconds':process.get('cpu_seconds'),'segment_id':segment,
                               'identity':identity,'component':key,'clock_domain':row['clock_domain']})
                if process.get('pss_bytes') is None:complete=False
                else:total+=process['pss_bytes']
                if process.get('sandbox_id'):cohort.add((process['sandbox_id'],process.get('generation')))
            for key in expected-seen:
                result.append({'kind':'cpu','t_s':row['t_s'],'cpu_seconds':None,'segment_id':segment,
                               'identity':None,'component':key,'clock_domain':row['clock_domain']})
            # Keep incomplete collector coverage explicit even if all visible processes succeeded.
            if not row.get('inventory_complete',True):
                result.append({'kind':'cpu','t_s':row['t_s'],'cpu_seconds':None,'segment_id':segment,
                               'identity':None,'component':'missing_inventory','clock_domain':row['clock_domain']})
            result.append({'kind':'pss','t_s':row['t_s'],'value':total if complete else None,
                           'clock_domain':row['clock_domain'],'cohort':sorted(cohort),'segment_id':segment})
        elif row.get('kind') in {'cpu','pss'}:result.append(dict(row,segment_id=(segment,row.get('segment_id'))))
    return result


def _scope_resources(records,window):
    series=records['resource_series']
    measured=[(i,row) for i,row in enumerate(series) if row.get('t_s') is not None]
    if not window or not measured or {row.get('clock_domain') for _,row in measured}!={window.get('clock_domain')}:
        return records
    before=[i for i,row in measured if row['t_s']<=window['start_s']]
    after=[i for i,row in measured if row['t_s']>=window['end_s']]
    left=before[-1] if before else 0;right=after[0] if after else len(series)-1
    return {**records,'resource_series':series[left:right+1]}


def _resource_window(records,window):
    records=_scope_resources(records,window)
    rows=_resource_rows(records)
    if not window or not rows:return {'status':'unavailable','reason':'missing_resource_window_or_evidence'}
    domains={row.get('clock_domain') for row in rows}
    if domains!={window.get('clock_domain')}:
        return {'status':'unavailable','reason':'resource_window_clock_mismatch',
                'resource_domains':sorted(str(d) for d in domains)}
    start,end=window['start_s'],window['end_s']
    if end<=start:return {'status':'unavailable','reason':'invalid_resource_window'}
    result={'status':'partial','window':dict(window)}
    cpu=[row for row in rows if row['kind']=='cpu'];pss=[row for row in rows if row['kind']=='pss']
    if cpu:
        segmented=cpu_segments(cpu)
        expected=records.get('expected_components') or sorted({row['component'] for row in cpu})
        result['cpu']=cpu_summary(segmented['intervals'],start,end,expected_components=expected)
        result['cpu']['gaps']=segmented['gaps']
    if pss:
        gap=records.get('resource_max_gap_s')
        result['pss']=resource_mean(pss,start,end,max_gap_s=gap) if gap is not None else {'status':'unavailable','reason':'missing_max_gap_s'}
        observed=[row['value'] for row in pss if start<=row['t_s']<=end and row['value'] is not None]
        result['pss']['sampled_peak_bytes']=max(observed) if observed else None
    return result


def _attributed_resources(records,window):
    from .metrics.resources import cohort_pss
    from .resources import guest_net_pss
    records=_scope_resources(records,window)
    result=_resource_window(records,window)
    snapshots=[row for row in records['resource_series'] if row.get('kind')=='resource_sample']
    for dimension,field in [('by_role','roles'),('by_component','components'),('by_identity',None)]:
        labels=set()
        for row in snapshots:
            for p in row['processes']:labels.update([_process_key(p)] if field is None else p.get(field,[]))
        result[dimension]={}
        for label in sorted(labels):
            selected=[]
            for row in records['resource_series']:
                if row.get('kind')!='resource_sample':
                    if row.get('kind')=='sample_gap':selected.append(row)
                    continue
                processes=[p for p in row['processes'] if (_process_key(p)==label if field is None else label in p.get(field,[]))]
                selected.append(dict(row,processes=processes,inventory_complete=bool(processes) and row.get('inventory_complete',True)))
            # Role aliases are views of identities, not additive components of the global total.
            result[dimension][label]=_resource_window({**records,'resource_series':selected,'expected_components':None},window)
    # Local-node views use their own observed spans when no common clock window exists.
    result['by_node']={};result['by_node_role_component']={}
    nodes=sorted({(p.get('expected_identity') or p.get('identity') or {}).get('node','unknown')
                  for row in snapshots for p in row['processes']})
    for node in nodes:
        node_series=[]
        for row in records['resource_series']:
            if row.get('kind')=='sample_gap':node_series.append(row);continue
            if row.get('kind')!='resource_sample':continue
            processes=[p for p in row['processes'] if (p.get('expected_identity') or p.get('identity') or {}).get('node','unknown')==node]
            if processes:node_series.append(dict(row,processes=processes))
        node_records={**records,'resource_series':node_series,'expected_components':None}
        node_samples=[row for row in node_series if row.get('kind')=='resource_sample']
        domains={row['clock_domain'] for row in node_samples}
        node_window=window
        if len(domains)==1 and (not window or domains!={window.get('clock_domain')}):
            times=[row['t_s'] for row in node_samples]
            node_window={'start_s':min(times),'end_s':max(times),'clock_domain':next(iter(domains)),
                         'window_kind':'observed_span','window_version':'resource.observed_span.v1'}
        result['by_node'][node]=_resource_window(node_records,node_window)
        result['by_node_role_component'][node]={}
        pairs=sorted({(role,component) for row in node_samples for p in row['processes']
                      for role in p.get('roles',[]) for component in p.get('components',[])})
        for role,component in pairs:
            selected=[]
            for row in node_series:
                if row.get('kind')=='sample_gap':selected.append(row);continue
                processes=[p for p in row['processes'] if role in p.get('roles',[]) and component in p.get('components',[])]
                selected.append(dict(row,processes=processes,inventory_complete=bool(processes) and row.get('inventory_complete',True)))
            result['by_node_role_component'][node].setdefault(role,{})[component]=_resource_window(
                {**node_records,'resource_series':selected},node_window)
    if window and snapshots and {row['clock_domain'] for row in snapshots}=={window.get('clock_domain')}:
        snapshots=[row for row in snapshots if window['start_s']<=row['t_s']<=window['end_s']]
    if snapshots:
        last=snapshots[-1];first=snapshots[0]
        ids=records.get('final_cohort',last.get('snapshot_cohort_ids',[]))
        per={sid:0 for sid in ids};seen=set();shared=0
        for p in last['processes']:
            sid=p.get('sandbox_id');value=p.get('pss_bytes')
            if sid in per:
                seen.add(sid)
                per[sid]=None if value is None or per[sid] is None else per[sid]+value
            elif not sid:shared=None if value is None or shared is None else shared+value
        per={sid:value for sid,value in per.items() if sid in seen}
        baseline=sum(p['pss_bytes'] for p in first['processes']) if first.get('inventory_complete',True) and all(p.get('pss_bytes') is not None for p in first['processes']) else None
        result['final_cohort']=cohort_pss(ids,per,shared_bytes=shared,baseline_bytes=baseline)
        complete=last.get('inventory_complete',True)
        if not complete:
            for name in ('direct_mean_bytes','with_shared_mean_bytes','incremental_mean_bytes'):
                result['final_cohort'][name]=None
        result['final_cohort'].update(scope='final_snapshot',inventory_complete=complete,
                                    status='valid' if complete and result['final_cohort']['coverage']==1 else 'partial',
                                    observed_at=last['t_s'],clock_domain=last['clock_domain'],
                                    inventory_source_ref=last.get('inventory_source_ref'))
        result['guest_net_pss']=guest_net_pss(result['final_cohort']['observed_total_bytes'],records.get('guest_pss_proof'),
            domain=last['clock_domain'],window=[last['t_s'],last['t_s']],coverage=result['final_cohort']['coverage'] if complete else 0)
        result['guest_net_pss'].update(scope='final_snapshot',window=[last['t_s'],last['t_s']],clock_domain=last['clock_domain'])
    else:
        result['final_cohort']={'status':'unavailable','reason':'missing_attributed_resource_evidence'}
        result['guest_net_pss']={'status':'unavailable','reason':'missing_guest_definition','raw_pss_bytes':None,'net_pss_bytes':None}
    return result


def _qualification_status(statuses):
    if 'invalid' in statuses:
        return 'invalid'
    return 'valid' if statuses and all(status=='valid' for status in statuses) else 'unverified'


def _qualifications(assertions,validation_policy=None):
    result={}
    for kind in ('post_validation','effective_spec'):
        evidence=[dict(row) for row in assertions if row.get('kind')==kind]
        result[kind]={'status':_qualification_status([row.get('status') for row in evidence]),
                      'assertions':evidence}
    if validation_policy=='none' and not result['post_validation']['assertions']:
        result['post_validation']['status']='not_applicable'
    result['status']=_qualification_status([value['status'] for value in result.values()
                                          if value['status']!='not_applicable'])
    return result


def _density_curve(records):
    density=records.get('density')
    if density is None:return None
    reference=next((row for row in records['raw_refs'] if row.get('logical_name')=='density.json'),
                   records.get('density_ref'))
    if not reference:return {'status':'unavailable','reason':'raw_density_reference_missing','points':[]}
    source={key:reference[key] for key in ('logical_name','path','sha256')}
    return {'status':'observed','capacity_measured':density.get('capacity_measured',False),
            'points':[{'index':index,'observation':row,
                       'evidence_ref':dict(source,json_pointer=f'/trace/{index}')}
                      for index,row in enumerate(density.get('trace',[]))]}


def analyze_case(records):
    records=_normalized(records)
    operations=[dict(row,repeat=row.get('repeat',1)) for row in records['operations']]
    window=records.get('window')
    result={'schema_version':'case_summary.v1','case_id':records['case_id'],
            'latency':summarize_repeats(operations),
            'quality':summarize_quality(operations,final_cohort=records.get('final_cohort')),
            'qualifications':_qualifications(records['assertions'],records.get('validation_policy')),
            'throughput':None,'resources':{'status':'unavailable','reason':'missing_resource_evidence'}}
    result['qualifications']['notes']=records.get('qualification_notes',[])
    result['qualifications']['gaps']=records.get('evidence_gaps',[])
    result['density_curve']=_density_curve(records)
    if window is not None:
        args={k:v for k,v in window.items() if k not in {'start_s','end_s'}}
        result['throughput']=throughput(records['events'],window['start_s'],window['end_s'],**args)
    if records['resource_series']:
        result['resources']=_attributed_resources(records,window)
        steady=records.get('steady')
        if isinstance(steady,dict) and steady.get('status')=='steady':
            result['resources']['steady']=_attributed_resources(records,steady)
        else:result['resources']['steady']={'status':'unavailable','reason':'eligible_steady_window_missing'}
    else:result['resources']['guest_net_pss']={'status':'unavailable','reason':'missing_guest_definition','net_pss_bytes':None}
    # JSON-normalize integer repeat keys so export/recompute are byte-equivalent.
    return json.loads(_json(result))


def _csv(summary):
    stream=io.StringIO(newline='');writer=csv.writer(stream)
    writer.writerow(['metric','value'])
    def visit(value,prefix=''):
        if isinstance(value,dict):
            for key in sorted(value):
                visit(value[key],f'{prefix}.{key}' if prefix else key)
        elif isinstance(value,list):
            writer.writerow([prefix,json.dumps(value,ensure_ascii=False,separators=(',',':'))])
        else:
            writer.writerow([prefix,'' if value is None else value])
    visit(summary)
    return stream.getvalue().encode()


def export_case(records,destination,*,xlsx=False,raw_payloads=None):
    """Seal a new directory atomically; never overwrite an existing case.

    Hashes detect accidental/raw-file changes, not a malicious actor rewriting
    both evidence and manifest. Store the returned manifest digest externally if
    authenticated provenance is required.
    """
    records=dict(records)
    raw_files={}
    if raw_payloads is not None:
        if not isinstance(raw_payloads,dict):
            raise ValueError('raw_payloads must map logical names to bytes')
        references=[]
        for logical_name,data in sorted(raw_payloads.items()):
            if not isinstance(logical_name,str) or not logical_name or not isinstance(data,bytes):
                raise ValueError('raw payload needs a logical name and immutable bytes')
            checksum=hashlib.sha256(data).hexdigest()
            path='raw/'+checksum
            raw_files[path]=data
            references.append({'logical_name':logical_name,'path':path,
                               'sha256':checksum,'size_bytes':len(data)})
        records['raw_refs']=references
    elif records.get('raw_refs'):
        raise ValueError('raw references require their payload bytes for immutable export')
    records=_normalized(records);summary=analyze_case(records)
    target=Path(destination)
    if target.exists():
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=Path(tempfile.mkdtemp(prefix='.case-',dir=target.parent))
    try:
        files={f'{key}.json':_json(records[key]) for key in ARRAYS}
        files.update(raw_files)
        files['summary.json']=_json(summary);files['summary.csv']=_csv(summary)
        if xlsx:
            try:
                import openpyxl
            except ImportError as exc:
                raise RuntimeError('XLSX export requires optional dependency openpyxl; JSON and CSV need no dependency') from exc
            workbook=openpyxl.Workbook();sheet=workbook.active;sheet.title='summary'
            for row in csv.reader(io.StringIO(files['summary.csv'].decode())):
                sheet.append(row)
            buffer=io.BytesIO();workbook.save(buffer);files['summary.xlsx']=buffer.getvalue()
        manifest={'schema_version':'case_manifest.v1','case_id':records['case_id'],
                  'metadata':{k:v for k,v in records.items() if k not in ARRAYS},
                  'files':{name:{'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data)} for name,data in files.items()}}
        for name,data in {**files,'case_manifest.json':_json(manifest)}.items():
            (temp/name).parent.mkdir(exist_ok=True)
            with (temp/name).open('xb') as handle:
                handle.write(data);handle.flush();os.fsync(handle.fileno())
        verify_case(temp)
        # A persistent lock file prevents concurrent exporters replacing a case.
        import fcntl
        with (target.parent/'.case-export.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if target.exists():
                raise FileExistsError(str(target))
            os.rename(temp,target)
            fd=os.open(target.parent,os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return manifest
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def verify_case(directory):
    root=Path(directory)
    try:
        if root.is_symlink() or (root/'case_manifest.json').is_symlink() or (root/'raw').is_symlink():
            raise ValueError('symlink evidence directory/manifest forbidden')
        manifest=json.loads((root/'case_manifest.json').read_bytes())
        if manifest.get('schema_version')!='case_manifest.v1' or not manifest.get('case_id'):
            raise ValueError('invalid manifest schema')
        files=manifest['files']
        required={f'{name}.json' for name in ARRAYS}|{'summary.json','summary.csv'}
        if not required <= files.keys():
            raise ValueError('missing evidence file')
        allowed=required|{'summary.xlsx'}
        raw_paths={name for name in files if re.fullmatch(r'raw/[0-9a-f]{64}',name)}
        if set(files)-allowed-raw_paths:
            raise ValueError('unexpected or unsafe evidence path')
        for name,info in files.items():
            path=root/name
            if path.is_symlink():
                raise ValueError('symlink evidence forbidden')
            data=path.read_bytes()
            if len(data)!=info['size_bytes'] or hashlib.sha256(data).hexdigest()!=info['sha256']:
                raise ValueError(f'evidence digest mismatch: {name}')
            if name in raw_paths and name!='raw/'+info['sha256']:
                raise ValueError('raw content-address does not match its digest')
        records=dict(manifest['metadata'])
        if records.get('case_id')!=manifest['case_id']:
            raise ValueError('manifest case identity mismatch')
        for name in ARRAYS:
            records[name]=json.loads((root/f'{name}.json').read_bytes())
            if any('seq' not in row for row in records[name]):
                raise ValueError(f'{name} sequence missing or out of order')
        _normalized(records)
        referenced=set();logical_names=set()
        for ref in records['raw_refs']:
            path=ref.get('path')
            name=ref.get('logical_name')
            if (not isinstance(name,str) or not name or name in logical_names
                    or path not in raw_paths or path!='raw/'+str(ref.get('sha256'))
                    or ref.get('sha256')!=files[path]['sha256']
                    or ref.get('size_bytes')!=files[path]['size_bytes']):
                raise ValueError('raw reference is missing, inconsistent, or unreachable')
            referenced.add(path);logical_names.add(name)
        if referenced!=raw_paths:
            raise ValueError('unreferenced raw payload in manifest')
        if records.get('density') is not None:
            density_ref=next((row for row in records['raw_refs'] if row.get('logical_name')=='density.json'),None)
            if density_ref is None or json.loads((root/density_ref['path']).read_bytes())!=records['density']:
                raise ValueError('density curve metadata does not match raw trace evidence')
        if json.loads((root/'summary.json').read_bytes())!=analyze_case(records):
            raise ValueError('sealed summary does not match evidence and window')
        return manifest
    except (OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
        raise ValueError('invalid or incomplete case evidence') from exc


def recompute_case(directory):
    manifest=verify_case(directory);root=Path(directory)
    records=dict(manifest['metadata'])
    records.update({name:json.loads((root/f'{name}.json').read_bytes()) for name in ARRAYS})
    return analyze_case(records)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case_directory')
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args(argv)
    result=verify_case(args.case_directory) if args.verify_only else recompute_case(args.case_directory)
    print(_json(result).decode())
    return 0


if __name__=='__main__':
    raise SystemExit(main())
