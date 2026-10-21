"""Local benchmark CLI; durable single-host session, with explicit real-environment inputs.

This executable does not install sandboxes or invent environment contracts. Production
multi-worker dispatch must use the fenced graph gateway, not share this local runner.
"""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from .adapters import Adapter
from .journal import FileJournal
from .load import LoadEngine, LoadSchedule, OperationResult
from .measure import create_sync, cleanup_owned, error_record, summarize
from .transport import validate_url


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def redact(value):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if any(x in str(k).lower() for x in ('token','password','secret','authorization','private'))
                    and not str(k).endswith('_ref') else redact(v)) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [redact(v) for v in value]
    return value


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(redact(value),stream,sort_keys=True,allow_nan=False);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def environment_identity(config):
    return digest({key:config.get(key) for key in ('group_id','product','api_url','data_url')})


def validate_config(config,scenario):
    if config.get('product') not in ('cubesandbox','kuasar','agentenv'):raise ValueError('unknown product')
    for key in ('api_url','data_url'):validate_url(config.get(key,''))
    for key in ('operation_timeout_s','run_timeout_s','cleanup_timeout_s','cleanup_budget_s','poll_interval_s'):
        value=config.get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('invalid '+key)
    for key in ('requests','concurrency'):
        if type(config.get(key)) is not int or config[key]<=0:raise ValueError('invalid '+key)
    ttl=config.get('create_body',{}).get('timeout')
    if type(ttl) is not int or ttl<=sum(config[k] for k in ('operation_timeout_s','run_timeout_s','cleanup_budget_s')):
        raise ValueError('TTL must cover load, drain and cleanup budget')
    source=config.get('source',{})
    if source.get('kind') not in ('template','snapshot','image') or not source.get('id'):raise ValueError('source required')
    spec=config.get('expected_spec',{})
    for k in ('vcpus','memory_mib'):
        if type(spec.get(k)) not in (int,float) or not math.isfinite(spec[k]) or spec[k]<=0:raise ValueError('expected spec required')
    if config.get('completion','STATE_READY') not in ('API_COMPLETE','STATE_READY','EXEC_COMPLETE'):
        raise ValueError('use a CaseContract session for audited log completion')
    if scenario!='create':raise ValueError('legacy entry supports create; use CaseContract for density/lifecycle with placement/sampling contracts')
    return config


def cleanup_run(config,folder):
    folder=Path(folder);saved=json.loads((folder/'run.json').read_text())
    if saved['environment']!=environment_identity(config):raise ValueError('cleanup environment mismatch')
    journal=FileJournal(folder/'ledger',saved['work_id'],child_budget=saved['budget'],parent_budget=saved['budget'])
    adapter=Adapter(config)
    handles=[{'sandbox_id':r['resource_id']} for r in journal.owned(saved['environment']) if r['kind']=='sandbox']
    rows=cleanup_owned(adapter,handles,config['cleanup_timeout_s'],total_timeout=config['cleanup_budget_s'])
    with (folder/'cleanup.jsonl').open('a') as stream:
        for row in rows:stream.write(json.dumps(row)+'\n')
    return rows


def run(config,scenario,folder):
    validate_config(config,scenario)
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    work_id=uuid.uuid4().hex;environment=environment_identity(config)
    atomic_json(folder/'run.json',{'work_id':work_id,'environment':environment,
                'budget':config['requests'],'config_hash':digest(config),'scenario':scenario})
    journal=FileJournal(folder/'ledger',work_id,child_budget=config['requests'],parent_budget=config['requests'])
    adapter=Adapter(config);rows=[];mismatches=[];unknown_specs=[]
    def owned(handle):
        journal.own('sandbox',handle['sandbox_id'],{'environment':environment})
    async def operation(index):
        row,handle=await asyncio.to_thread(create_sync,adapter,work_id,config['operation_timeout_s'],
            config['poll_interval_s'],on_created=owned,completion=config.get('completion','STATE_READY'))
        row['request_index']=index
        if handle is not None:
            try:
                actual=await asyncio.to_thread(adapter.effective_spec,handle,config['operation_timeout_s'])
                row['effective_spec']=actual
                if any(actual.get(k) is None for k in config['expected_spec']):unknown_specs.append(index)
                elif any(actual.get(k)!=v for k,v in config['expected_spec'].items()):mismatches.append(index)
            except Exception as exc:
                row['spec_error']=error_record(exc);unknown_specs.append(index)
        rows.append(row)
        return OperationResult(row['status'],int(row['unknown_create_outcome']),row.get('reason'),row)
    schedule=LoadSchedule('WINDOW',config['requests'],config['concurrency'],
                         config['operation_timeout_s']*2,deadline=config['run_timeout_s'])
    report=None
    try:
        report=asyncio.run(LoadEngine().run(schedule,operation,journal=journal))
    finally:
        if report is not None:
            recorded={row['request_index'] for row in rows}
            for index,outcome in report.results.items():
                if index not in recorded:
                    rows.append({'request_index':index,'status':outcome.status,'elapsed_s':None,
                                 'reason':outcome.reason,'unresolved_external':outcome.unresolved_external})
        with (folder/'operations.jsonl').open('w') as stream:
            for row in sorted(rows,key=lambda x:x['request_index']):stream.write(json.dumps(redact(row))+'\n')
        cleanup=cleanup_owned(adapter,[{'sandbox_id':r['resource_id']} for r in journal.owned(environment)],
                              config['cleanup_timeout_s'],total_timeout=config['cleanup_budget_s'])
        with (folder/'cleanup.jsonl').open('w') as stream:
            for row in cleanup:stream.write(json.dumps(row)+'\n')
    successful = [row for row in rows if row['status']=='success']
    missing_specs = [row['request_index'] for row in successful
                     if not row.get('effective_spec') or any(
                         row['effective_spec'].get(k) is None for k in config['expected_spec'])]
    unknown_specs = sorted(set(unknown_specs) | set(missing_specs))
    result={'status':'completed' if report.submitted==config['requests'] and not report.unresolved_external else 'incomplete',
            'data_validity':'invalid' if mismatches else 'unverified' if unknown_specs or not successful else 'valid',
            'spec_mismatches':len(mismatches),'spec_unverified':len(unknown_specs),
            'summary':summarize(rows,report.wall_seconds),'load':asdict(report),'cleanup':cleanup,
            'evidence_kind':'runtime_observation','physical_release':'unverified'}
    atomic_json(folder/'summary.json',result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    for name in ('run','cleanup'):
        p=sub.add_parser(name);p.add_argument('--config',required=True);p.add_argument('--output',required=True)
        if name=='run':p.add_argument('--scenario',default='create')
    p=sub.add_parser('verify');p.add_argument('directory')
    p=sub.add_parser('recompute');p.add_argument('directory')
    p=sub.add_parser('case')
    p.add_argument('--case',required=True);p.add_argument('--config',required=True)
    p.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:
        if args.action in ('verify','recompute'):
            from .analysis import verify_case,recompute_case
            result=(verify_case if args.action=='verify' else recompute_case)(args.directory)
        elif args.action=='case':
            import yaml
            from .contracts import CaseContract
            from .wiring import configured_session
            case=CaseContract.model_validate_json(json.dumps(yaml.safe_load(Path(args.case).read_text())))
            config=yaml.safe_load(Path(args.config).read_text())
            session=configured_session(case,config,args.output)
            async def execute_case():
                results={};failed=False
                for i in range(11):
                    node=f'N{i:02}'
                    if failed and node not in ('N07','N09','N10'):continue
                    value=await session.apply_step(node,case.digest())
                    results[node]={k:v for k,v in value.items() if k in {
                        'status','reason','artifact_ref','data_status','submitted','successes',
                        'stop_reason','unresolved_external','physical_release','unknown_operations'}}
                    if value.get('gaps'):results[node]['gap_count']=len(value['gaps'])
                    if value.get('status') not in ('completed','ready','valid'):failed=True
                return {'status':'incomplete' if failed else 'completed','steps':results,
                        'backend':'local-file-single-host','environment_verified':False}
            result=asyncio.run(execute_case())
        else:
            import yaml
            config=yaml.safe_load(Path(args.config).read_text())
            result=run(config,args.scenario,args.output) if args.action=='run' else cleanup_run(config,args.output)
        print(json.dumps(redact(result),sort_keys=True,allow_nan=False))
        return 2 if isinstance(result,dict) and result.get('status') in ('incomplete','blocked','failed') else 0
    except Exception as exc:
        print(json.dumps({'status':'failed',**error_record(exc)}));return 1

if __name__=='__main__':raise SystemExit(main())
