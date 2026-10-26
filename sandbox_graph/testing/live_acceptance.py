"""Opt-in finite product acceptance; no fixture data or successful fallback.

The input is operator-owned JSON. A dedicated, migrated test database and prepared
EnvironmentAcceptance records are required. Unknown effects remain in the ledger.
"""
import asyncio
import json
import math
import os
from pathlib import Path
import time
from ..domain.models import EvidenceRef
from ..environment.cli import read_json
from ..runtime.evaluation_cli import assemble


def validate_contract(value):
    required={'schema_version','runtime','selection','request_id','seconds','max_work_items','expected_groups','output'}
    if not isinstance(value,dict) or set(value)!=required or value['schema_version']!=1:
        raise ValueError('explicit versioned live acceptance contract required')
    if (isinstance(value['seconds'],bool) or not isinstance(value['seconds'],(int,float))
            or not math.isfinite(value['seconds']) or not 0<value['seconds']<=3600
            or type(value['max_work_items']) is not int or not 1<=value['max_work_items']<=100
            or not isinstance(value['selection'],dict) or not value['expected_groups']
            or not all(isinstance(x,str) and x for x in value['expected_groups'])
            or len(set(value['expected_groups']))!=len(value['expected_groups'])
            or not isinstance(value['request_id'],str) or not value['request_id']):
        raise ValueError('bounded live selection, request identity and groups required')
    for key in ('runtime','output'):
        if not isinstance(value[key],str) or not Path(value[key]).is_absolute():raise ValueError('absolute live paths required')
    return value


def verify_delivery(deployment,status,groups):
    if {row['scope'] for row in status['deliveries']}!=groups:raise ValueError('actual group delivery coverage differs')
    if not status['archives'] or not status['analyses']:raise ValueError('analysis and final archive required')
    with deployment.repository.transaction() as tx:
        work={row['key'] for row in tx.scan('work_instances') if row['body']['run_id']==status['run']['run_id']}
        if not work or work!={ref for row in status['deliveries'] for ref in row['work_refs']}:
            raise ValueError('every selected work requires a delivery')
        for identity in work:
            if not tx.get('scope_dispositions',identity):raise ValueError('work has no final disposition')
        if any(row['body'].get('scope') in work and row['body']['status']!='closed' for row in tx.scan('resources')):
            raise ValueError('owned resources have not been cleaned')
        for row in status['operations']:
            if row['phase']!='terminal' or not row.get('status',{}).get('authoritative',False):raise ValueError('operation effects remain unknown')
        for analysis in status['analyses']:
            export=tx.get('analysis_exports',analysis['analysis_ref'])
            if not export or export['body']['analysis_input_ref']!=analysis['analysis_input_ref']:
                raise ValueError('export analysis version differs')
            files=export['body']['files']
            if not {'facts.json','facts.xlsx'}<=set(files):raise ValueError('factual JSON and Excel export required')
            for reference in files.values():deployment.store.verify(EvidenceRef.model_validate(reference))
        for archive in status['archives']:
            if archive['analysis_ref'] not in {row['analysis_ref'] for row in status['analyses']}:
                raise ValueError('archive and analysis versions differ')
    return {'status':'passed','run_id':status['run']['run_id'],'groups':sorted(groups),
        'work_count':len(work),'analyses':status['analyses'],'archives':status['archives']}


async def run_live_acceptance(path,*,dsn=None):
    value=validate_contract(read_json(path));dsn=dsn or os.environ.get('SANDBOX_GRAPH_TEST_POSTGRES_DSN')
    if not dsn:raise ValueError('SANDBOX_GRAPH_TEST_POSTGRES_DSN is required; no default database')
    runtime=read_json(value['runtime']);deployment,pool=assemble(runtime,dsn)
    output=Path(value['output']);output.parent.mkdir(parents=True,exist_ok=True)
    run=None;result={'status':'failed','phase':'admission'}
    try:
        plan=deployment.preview(value['selection'])
        if plan.stage!='all' or {case.group_id for case in plan.cases}!=set(value['expected_groups']):
            raise ValueError('live plan must select the exact expected groups and all stages')
        count=sum(len(case.conditions) for case in plan.cases)
        if not 0<count<=value['max_work_items']:raise ValueError('selected work exceeds acceptance budget')
        run=await deployment.start(value['request_id'],plan.hash,seconds=value['seconds'])
        end=time.monotonic()+value['seconds'];result={'status':'running','run_id':run.run_id}
        while time.monotonic()<end:
            remaining=end-time.monotonic()
            if remaining<=0:break
            worked=await asyncio.wait_for(deployment.worker.run_once(),timeout=remaining)
            status=deployment.status(run.run_id)
            if status['archives']:
                result=verify_delivery(deployment,status,set(value['expected_groups']));return result
            if not worked:await asyncio.sleep(min(.25,max(0,end-time.monotonic())))
        raise TimeoutError('live acceptance execution budget exhausted')
    except BaseException as exc:
        result=result|{'status':'failed','error':type(exc).__name__}
        if run is not None:
            deployment.api.cancel(value['request_id']+':cleanup',run.run_id,'acceptance finished without verified archive')
            # Cancellation queues the existing bounded stop/cleanup protocol.
            # Never drop the original IDs or claim that a cancel ACK proves cleanup.
            cleanup_end=time.monotonic()+30
            while time.monotonic()<cleanup_end:
                try:worked=await asyncio.wait_for(deployment.worker.run_once(),timeout=max(.01,cleanup_end-time.monotonic()))
                except (Exception,asyncio.CancelledError):break
                if not worked:break
            status=deployment.status(run.run_id)
            result.update(jobs=status['jobs'],operations=status['operations'],cleanup='inspect_original_ledger')
        raise
    finally:
        output.write_text(json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2,default=str)+'\n')
        pool.close()
