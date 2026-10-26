"""Finite, opt-in installation/re-entry acceptance using the actual environment graph."""
import asyncio
import json
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace
from ..environment.cli import read_json,check_command,build_bundle,planner_for,prepare_worker
from ..environment.contracts import EnvironmentDeclaration,EnvironmentReport
from ..environment.declarations import load_declaration
from ..environment.workflow import EnvironmentRunService
from ..artifacts.store import FileArtifactStore
from ..domain.models import EvidenceRef,canonical_hash
from ..persistence.repository import PostgresRepository


def validate_contract(value,*,product,topology):
    required={'schema_version','environment','runtime','authority','artifacts','output','request_id','seconds','max_steps'}
    if (not isinstance(value,dict) or set(value)!=required or value['schema_version']!=1
            or type(value['seconds']) not in (float,int) or not math.isfinite(value['seconds']) or not 0<value['seconds']<=3600
            or type(value['max_steps']) is not int or not 1<=value['max_steps']<=64
            or not isinstance(value['request_id'],str) or not value['request_id']):
        raise ValueError('bounded explicit environment acceptance contract required')
    if product not in {'cubesandbox','kuasar','agentenv'} or topology not in {'single','multi'}:
        raise ValueError('fixed product/topology acceptance required')
    for name in ('environment','runtime','authority','artifacts','output'):
        if not isinstance(value[name],str) or not Path(value[name]).is_absolute():raise ValueError('absolute environment acceptance paths required')
    return value


async def run_environment_acceptance(path,*,product,topology,dsn=None):
    value=validate_contract(read_json(path),product=product,topology=topology)
    dsn=dsn or os.environ.get('SANDBOX_GRAPH_TEST_POSTGRES_DSN')
    if not dsn:raise ValueError('dedicated SANDBOX_GRAPH_TEST_POSTGRES_DSN required')
    declaration=EnvironmentDeclaration.model_validate(load_declaration(value['environment']))
    if declaration.product!=product or (len(declaration.nodes)==1)!=(topology=='single'):
        raise ValueError('declaration product/topology differs from selected acceptance')
    runtime=read_json(value['runtime']);authority=read_json(value['authority'])
    repository=PostgresRepository(dsn);store=FileArtifactStore(value['artifacts']);service=EnvironmentRunService(repository)
    output=Path(value['output']);output.mkdir(parents=True,exist_ok=False)
    result={'status':'failed','product':product,'topology':topology};pool=None;run=None
    try:
        args=SimpleNamespace(environment=Path(value['environment']),artifacts=Path(value['artifacts']),node=None,output=output/'initial-report.json')
        await check_command(args,runtime)
        ref=EvidenceRef.model_validate(read_json(args.output));store.verify(ref)
        report=EnvironmentReport.model_validate_json(store.path_for(ref).read_bytes())
        bundle=build_bundle(declaration,report,ref,runtime,planner=planner_for(runtime),node_ids=[n.node_id for n in declaration.nodes],
            actions=[row['action_ref'] for row in runtime['actions']],deadline=time.time()+value['seconds'],now=time.time())
        auth_ref='environment-authority:'+canonical_hash(authority)
        with repository.transaction() as tx:tx.put('environment_authorizations',auth_ref,authority,immutable=True)
        plan=service.plan(declaration,mode='prepare',node_ids=bundle['node_ids'],deadline=bundle['deadline'],
            authorization_ref=auth_ref,runtime_hash=bundle['runtime_hash'],approved_repair_hash=bundle['repair_plan']['plan_hash'],max_steps=value['max_steps'])
        with repository.transaction() as tx:tx.put('environment_scope_inputs',plan.plan_hash,bundle,immutable=True)
        run=service.start(value['request_id'],plan.plan_hash)
        worker,pool=prepare_worker(repository,runtime,store,'live-environment-acceptance')
        end=time.monotonic()+value['seconds']
        while time.monotonic()<end:
            await asyncio.wait_for(worker.run_once(),max(.01,end-time.monotonic()))
            status=service.status(run.run_id)
            ready=[row for row in status['deliveries'] if row.get('ready')]
            if ready:break
            if status['deliveries']:raise AssertionError('actual environment graph failed acceptance; inspect retained delivery')
            await asyncio.sleep(.1)
        else:raise TimeoutError('environment acceptance deadline expired')
        delivery=ready[-1]
        if delivery.get('cleanup_status')!='complete' or not delivery.get('acceptance_ref'):
            raise AssertionError('current functional/cleanup acceptance missing')
        with repository.transaction() as tx:
            operations=[row['body'] for row in tx.scan('operations') if row['body'].get('run_id')==run.run_id]
            if any(row['phase']!='terminal' or not row.get('status',{}).get('authoritative') for row in operations):
                raise AssertionError('environment contains unresolved original effects')
            before={row['key'] for row in tx.scan('operations')}
        assert service.start(value['request_id'],plan.plan_hash).run_id==run.run_id
        with repository.transaction() as tx:
            if {row['key'] for row in tx.scan('operations')}!=before:raise AssertionError('idempotent restart issued a new operation')
        for reference in delivery['artifacts'].values():store.verify(EvidenceRef.model_validate(reference))
        result.update(status='passed',run_id=run.run_id,delivery=delivery,operation_count=len(operations),
            reentry='same_request_same_run_no_new_operation')
        return result
    except BaseException as exc:
        result.update(error=type(exc).__name__)
        if run:
            result['run_id']=run.run_id
            result['ledger']=service.status(run.run_id)
            result['recovery']='env status/reconcile original run; unresolved effects retain reservation'
        raise
    finally:
        (output/'summary.json').write_text(json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2,default=str)+'\n')
        if pool is not None:pool.close()
