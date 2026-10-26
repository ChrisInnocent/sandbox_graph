"""Standalone environment check/plan/apply/worker/status/reconcile/report commands."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from .contracts import EnvironmentDeclaration,EnvironmentReport,RepairPlan
from .declarations import load_declaration,_unique_json_object
from .planner import ActionCatalog,ActionDefinition,RepairPlanner
from .remote_runner import RecipeRepairPlanner,RemoteRecipeRunner
from .recipe_catalog import load_recipe_catalog
from .workflow import EnvironmentRunService,EnvironmentScopePlan
from .handlers import EnvironmentWorkflowHandlers,build_environment_handler,timestamp
from .probes.environment import RemoteEnvironmentProbe
from .transport import SSHTransport
from .reporting import publish_report
from ..artifacts.store import FileArtifactStore
from ..domain.models import EvidenceRef,ScriptRef,canonical_hash


def parser():
    p=argparse.ArgumentParser(prog='python -m sandbox_graph env')
    commands=p.add_subparsers(dest='command',required=True)
    for name in ('check','plan','apply','worker','status','reconcile','report'):
        sub=commands.add_parser(name)
        sub.add_argument('--artifacts',type=Path,default=Path('artifacts/environment'))
        if name in {'check','plan','apply','worker'}:sub.add_argument('--runtime',type=Path,required=True)
        if name in {'check','plan'}:
            sub.add_argument('--environment',type=Path,required=True);sub.add_argument('--node',action='append')
            sub.add_argument('--output',type=Path,required=True)
        if name=='plan':
            sub.add_argument('--report',type=Path,required=True,help='EvidenceRef JSON from env check')
            sub.add_argument('--seconds',type=float,default=300)
        if name=='apply':
            sub.add_argument('--plan',type=Path,required=True);sub.add_argument('--authority',type=Path,required=True)
            sub.add_argument('--request-id',required=True)
        if name in {'status','report','reconcile'}:sub.add_argument('--run-id',required=True)
        if name=='reconcile':sub.add_argument('--request-id',required=True)
        if name=='worker':
            sub.add_argument('--once',action='store_true');sub.add_argument('--owner',default='environment-worker')
            sub.add_argument('--lifecycle-runtime',type=Path)
    return p


def read_json(path,limit=4*1024*1024):
    path=Path(path)
    if path.stat().st_size>limit:raise ValueError('input exceeds byte budget')
    return json.loads(path.read_bytes(),object_pairs_hook=_unique_json_object,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite JSON number')))


class LocalReferences:
    def __init__(self,rows):self.rows=rows
    def resolve(self,ref):
        item=self.rows.get(ref)
        if not isinstance(item,dict) or set(item)!={'path','sha256'}:raise ValueError('unresolved typed local reference')
        path=Path(item['path'])
        if not path.is_absolute() or not path.is_file() or path.resolve()!=path:raise ValueError('local reference path is unsafe')
        if ref.startswith('secret://') and stat.S_IMODE(path.stat().st_mode)&0o077:raise PermissionError('secret reference must be private')
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(65536),b''):digest.update(block)
        if digest.hexdigest()!=item['sha256']:raise ValueError('local reference digest differs')
        return path


def planner_for(runtime):
    actions=tuple(ActionDefinition(**{key:tuple(value) if key in {'repair_codes','roles','dependencies'} else value
        for key,value in row.items()}) for row in runtime.get('actions',()))
    planner=RepairPlanner(ActionCatalog(actions))
    bindings={}
    for row in runtime.get('bindings',()):
        recipes=load_recipe_catalog(row['catalog_path']);steps=recipes[row['recipe']]
        value={'recipe':row['recipe'],'recipe_hash':canonical_hash([asdict(step) for step in steps]),
            'steps':steps,'expected_before':row['expected_before']}
        if value['recipe_hash']!=row['recipe_hash']:raise ValueError('runtime recipe hash changed')
        key=(row['node_id'],row['action_ref'])
        if key in bindings:raise ValueError('duplicate runtime recipe binding')
        bindings[key]=value
    return RecipeRepairPlanner(planner,bindings,runtime.get('canaries',{}))


def build_bundle(declaration,report,report_ref,runtime,*,planner,node_ids,actions,deadline,now):
    declaration=EnvironmentDeclaration.model_validate(declaration);report=EnvironmentReport.model_validate(report)
    if not node_ids or not set(node_ids)<={node.node_id for node in declaration.nodes}:raise ValueError('plan node selection is undeclared')
    if any(row.node_id not in node_ids for row in report.observations):raise ValueError('report includes unselected nodes; run a scoped check')
    if not report.valid_until:raise ValueError('environment report has no expiry')
    expiry=datetime.fromisoformat(report.valid_until.replace('Z','+00:00')).timestamp()
    if expiry<=now or not now<deadline<=now+86400:raise ValueError('environment report or deadline expired/invalid')
    result=planner.plan(declaration,report,report_ref='artifact://'+report_ref.uri,
        deadline=timestamp(deadline),authorized_actions=set(actions))
    if result.status=='blocked':raise ValueError('environment plan is blocked: '+','.join(row.code for row in result.blockers))
    if not set(step.node_id for step in result.plan.steps)<=set(node_ids):raise ValueError('repair plan escaped selected nodes')
    body={'schema_version':1,'declaration':declaration.model_dump(mode='json'),'report':report_ref.model_dump(mode='json'),
        'runtime_hash':canonical_hash(runtime),'repair_plan':result.plan.model_dump(mode='json'),
        'node_ids':sorted(node_ids),'deadline':deadline}
    return body|{'bundle_hash':canonical_hash(body)}


def load_bundle(path,*,store,runtime,now,historical_replay=False):
    bundle=read_json(path)
    body={key:value for key,value in bundle.items() if key!='bundle_hash'}
    if canonical_hash(body)!=bundle.get('bundle_hash'):raise ValueError('local environment bundle digest differs')
    if canonical_hash(runtime)!=bundle['runtime_hash']:raise ValueError('frozen runtime binding changed')
    if not historical_replay and bundle['deadline']<=now:raise ValueError('local environment plan expired')
    ref=EvidenceRef.model_validate(bundle['report']);store.verify(ref)
    declaration=EnvironmentDeclaration.model_validate(bundle['declaration'])
    report=EnvironmentReport.model_validate_json(store.path_for(ref).read_bytes())
    plan=RepairPlan.model_validate(bundle['repair_plan'])
    if (report.declaration_hash!=declaration.declaration_hash or plan.declaration_hash!=declaration.declaration_hash
            or plan.report_ref!='artifact://'+ref.uri):raise ValueError('local plan/report declaration binding differs')
    hash_body=plan.model_dump(mode='json',exclude={'schema_version','plan_hash'})
    if canonical_hash(hash_body)!=plan.plan_hash:raise ValueError('local repair plan digest differs')
    if not historical_replay and (not report.valid_until or datetime.fromisoformat(report.valid_until.replace('Z','+00:00')).timestamp()<=now):
        raise ValueError('local report expired')
    return bundle


async def check_command(args,runtime):
    declaration=EnvironmentDeclaration.model_validate(load_declaration(args.environment));store=FileArtifactStore(args.artifacts)
    scope='environment-check:'+canonical_hash({'declaration':declaration.declaration_hash,'time':time.time_ns()})[:24]
    probe=RemoteEnvironmentProbe(transport=SSHTransport(helper_paths=runtime.get('helper_paths')),resolver=LocalReferences(runtime['references']),store=store,scope=scope)
    from .verification import ProductVerification
    from .probes.registration import RuntimeRegistrationProbe
    registered=RuntimeRegistrationProbe(probe=probe,bindings=runtime['registration']) if runtime.get('registration') else None
    probe.verification=ProductVerification(probe=probe,scope=scope,store=store,runtime=registered)
    report=await probe.check(declaration,args.node or [node.node_id for node in declaration.nodes],fresh=True)
    raw=store.commit([report.model_dump_json().encode()],scope=scope,media_type='application/json',producer='environment-check.v1')
    manifest=publish_report(report,store=store,scope=scope,resolve=probe.resolve,xlsx=True)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(raw.model_dump_json(indent=2)+'\n')
    ready=bool(report.readiness) and all(value=='ready' for value in report.readiness.values())
    return {'ready':ready,'report':raw.model_dump(mode='json'),'manifest':{key:value.model_dump(mode='json') for key,value in manifest.items()}},0 if ready else 2


def prepare_worker(repository,runtime,store,owner,*,lifecycle_runtime=None):
    from ..persistence.checkpointer import FencedPostgresSaver
    from ..runtime.worker import Worker,WorkerPolicy
    from ..runtime.runner import OperationService
    from .execution import EnvironmentRunnerGateway
    from .grant_service import FencedExecutionGrantService
    from .permits import ExecutionGrantIssuer
    from . import remote_agent,handshake
    from .products import executor
    from psycopg_pool import ConnectionPool
    resolver=LocalReferences(runtime['references']);transport=SSHTransport(helper_paths=runtime.get('helper_paths'));planner=planner_for(runtime)
    key_path=resolver.resolve(runtime['grant_signing_ref']);issuer=ExecutionGrantIssuer(key_path.read_bytes())
    root=Path(__file__).parent
    digest=canonical_hash({str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob('*.py'))})
    script=ScriptRef(script_id='environment.recipe.step',version='1',digest=digest)
    def factory(token,activation):
        with repository.transaction() as tx:
            plan=EnvironmentScopePlan.model_validate(tx.get('environment_plans',activation.plan_ref)['body'])
            if plan.runtime_hash!=canonical_hash(runtime):raise ValueError('worker runtime differs from frozen environment plan')
            declaration=EnvironmentDeclaration.model_validate(tx.get('environment_declarations',plan.declaration_hash)['body'])
        runner=RemoteRecipeRunner(repository,token=token,declaration=declaration,transport=transport,resolver=resolver,
            grants=FencedExecutionGrantService(repository,issuer),store=store)
        from .gateway import EnvironmentGateway
        from .canary import EnvironmentCanaryRunner
        probe=RemoteEnvironmentProbe(transport=transport,resolver=resolver,store=store,scope=activation.run_id)
        canaries={node_id:EnvironmentCanaryRunner(repository,token=token,config=value['config'],
            store=store,private_root=runtime['instance_store_path'],management_probe=probe.management_dependency)
            for node_id,value in runtime.get('canaries',{}).items()}
        combined=EnvironmentGateway(repository,token=token,remote=runner,canaries=canaries)
        operations=OperationService(repository,combined,{(script.script_id,script.version):script})
        gateway=EnvironmentRunnerGateway(operations,action_scripts={row['action_ref']:script for row in runtime['actions']})
        from .verification import ProductVerification
        from .probes.registration import RuntimeRegistrationProbe
        registered=RuntimeRegistrationProbe(probe=probe,bindings=runtime['registration']) if runtime.get('registration') else None
        probe.verification=ProductVerification(probe=probe,repository=repository,scope=activation.run_id,store=store,runtime=registered)
        async def cleanup(context,resources):
            await operations.cleanup(context.run_ref,tuple(row['key'] for row in resources),token)
        return EnvironmentWorkflowHandlers(repository,store=store,probe=probe,planner=planner,gateway=gateway,cleanup=cleanup)
    from ..runtime.bootstrap import build_job_handlers
    from ..runtime.lifecycle_assembly import assemble_lifecycle
    from .recovery import EnvironmentRecoveryOperations
    handlers=build_job_handlers(repository,graph_ref='environment:1',
        saver_factory=lambda token:FencedPostgresSaver(pool,token),node_service_factory=factory,
        review_service=None,deadline=None,lease_seconds=60,segment_seconds=3600,owner=owner,
        operation_service=EnvironmentRecoveryOperations(repository,factory),
        **assemble_lifecycle(repository,lifecycle_runtime,store=store,resolver=resolver))
    pool=ConnectionPool(repository.dsn,kwargs={'autocommit':True},min_size=1,max_size=4)
    try:
        worker=Worker(repository,handlers,policy=WorkerPolicy(claim_seconds=60,job_seconds=3650,poll_seconds=1,
            accepted_job_kinds=tuple(handlers)+('lifecycle_stop',)),owner=owner)
    except BaseException:
        pool.close();raise
    return worker,pool


async def execute(args):
    runtime=read_json(args.runtime) if hasattr(args,'runtime') else None
    store=FileArtifactStore(args.artifacts)
    if args.command=='check':return await check_command(args,runtime)
    if args.command=='plan':
        declaration=EnvironmentDeclaration.model_validate(load_declaration(args.environment));ref=EvidenceRef.model_validate(read_json(args.report))
        store.verify(ref);report=EnvironmentReport.model_validate_json(store.path_for(ref).read_bytes())
        bundle=build_bundle(declaration,report,ref,runtime,planner=planner_for(runtime),node_ids=args.node or [node.node_id for node in declaration.nodes],
            actions=[row['action_ref'] for row in runtime['actions']],deadline=time.time()+args.seconds,now=time.time())
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(bundle,ensure_ascii=False,indent=2)+'\n')
        return {'bundle_hash':bundle['bundle_hash'],'path':str(args.output),'steps':len(bundle['repair_plan']['steps'])},0
    from ..persistence.repository import PostgresRepository
    dsn=os.environ.get('SANDBOX_GRAPH_POSTGRES_DSN')
    if not dsn:raise ValueError('SANDBOX_GRAPH_POSTGRES_DSN is required; no fallback for environment mutations')
    repository=PostgresRepository(dsn);service=EnvironmentRunService(repository)
    if args.command=='apply':
        with repository.transaction() as tx:previous=tx.get('environment_commands',args.request_id)
        bundle=load_bundle(args.plan,store=store,runtime=runtime,now=time.time(),historical_replay=bool(previous))
        authority=read_json(args.authority)
        auth_ref='environment-authority:'+canonical_hash(authority)
        if previous:
            with repository.transaction() as tx:
                saved=tx.get('environment_scope_inputs',previous['body']['plan_hash'])
                old_plan=tx.get('environment_plans',previous['body']['plan_hash'])
                if (not saved or saved['body']!=bundle or not old_plan or old_plan['body']['authorization_ref']!=auth_ref):
                    raise ValueError('original environment apply input changed')
            run=service.start(args.request_id,previous['body']['plan_hash'])
            return {'run_id':run.run_id,'plan_hash':previous['body']['plan_hash'],'queued':True,'historical_replay':True},0
        with repository.transaction() as tx:tx.put('environment_authorizations',auth_ref,authority,immutable=True)
        plan=service.plan(bundle['declaration'],mode='prepare',node_ids=bundle['node_ids'],deadline=bundle['deadline'],
            authorization_ref=auth_ref,runtime_hash=bundle['runtime_hash'],approved_repair_hash=bundle['repair_plan']['plan_hash'])
        with repository.transaction() as tx:tx.put('environment_scope_inputs',plan.plan_hash,bundle,immutable=True)
        run=service.start(args.request_id,plan.plan_hash)
        return {'run_id':run.run_id,'plan_hash':plan.plan_hash,'queued':True},0
    if args.command=='reconcile':return {'job_id':service.reconcile(args.request_id,args.run_id)},0
    if args.command in {'status','report'}:
        result=service.status(args.run_id)
        return result if args.command=='status' else {'deliveries':result['deliveries']},0
    worker,pool=prepare_worker(repository,runtime,store,args.owner,
            lifecycle_runtime=read_json(args.lifecycle_runtime) if getattr(args,'lifecycle_runtime',None) else None)
    try:
        if args.once:return {'worked':await worker.run_once()},0
        while True:
            worked=await worker.run_once()
            if not worked:await asyncio.sleep(1)
    finally:pool.close()


def main(argv=None):
    args=parser().parse_args(argv)
    try:result,code=asyncio.run(execute(args))
    except Exception as exc:
        # Never echo input/configuration/credential values on CLI failures.
        result={'status':'blocked','error':type(exc).__name__};code=2
    print(json.dumps(result,sort_keys=True,ensure_ascii=False,default=str));return code

if __name__=='__main__':raise SystemExit(main())
