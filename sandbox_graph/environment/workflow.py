"""Environment-only immutable plan and graph, independent of benchmark Cases."""
from __future__ import annotations
from typing import Literal
from uuid import uuid4
from pydantic import Field
from langgraph.graph import END

from .contracts import EnvironmentModel,EnvironmentDeclaration
from ..domain.models import RunRecord,canonical_hash
from ..graphs.factory import GraphDefinition
from ..persistence.repository import Conflict,PostgresRepository
from ..runtime.tasks import TaskBackend,JobSpec


def environment_definition():
    return GraphDefinition(graph_id='environment',version='1',
        nodes=('E1','E2','C1','C2','C3','T8','GE'),entry='E1',
        edges={'C3':('T8',),'T8':('GE',),'GE':(END,)},
        dynamic_routes={'E1':('E2','T8'),'E2':('C1','T8'),'C1':('C2','T8'),'C2':('C3','T8')},
        required_cleanup=('T8','GE'))


def environment_routers():
    def route(node,next_node):
        def choose(state):
            result=state['results'][node]
            if result['execution_status']!='succeeded':return 'T8'
            return result.get('continuation') or next_node
        return choose
    return {node:route(node,next_node) for node,next_node in [('E1','E2'),('E2','C1'),('C1','C2'),('C2','C3')]}


class EnvironmentScopePlan(EnvironmentModel):
    kind: Literal['environment-scope.v1']='environment-scope.v1'
    graph_ref: Literal['environment:1']='environment:1'
    graph_hash: str=Field(pattern=r'^[a-f0-9]{64}$')
    declaration_hash: str=Field(pattern=r'^[a-f0-9]{64}$')
    environment_id: str
    group_id: str
    node_ids: tuple[str,...]=Field(min_length=1)
    mode: Literal['check','prepare']
    authorization_ref: str|None=None
    authorization_hash: str|None=None
    authorization_revision: int|None=None
    allowed_actions: tuple[str,...]=()
    max_repair_revisions: int=Field(default=2,ge=1,le=10)
    max_steps: int=Field(default=64,ge=1,le=256)
    revision: int=Field(default=1,ge=1)
    runtime_hash: str|None=None
    approved_repair_hash: str|None=None
    deadline: float=Field(gt=0)

    @property
    def plan_hash(self):return canonical_hash(self)


class EnvironmentRunService:
    def __init__(self,repository):self.repository=repository

    @staticmethod
    def verify_authority_in(tx,plan):
        if plan.mode=='check':
            if plan.allowed_actions or plan.authorization_ref:raise PermissionError('check-only plan cannot contain mutation authority')
            return
        row=tx.get('environment_authorizations',plan.authorization_ref or '')
        if (not row or row['revision']!=plan.authorization_revision
                or canonical_hash(row['body'])!=plan.authorization_hash):
            raise PermissionError('environment authorization missing or changed')
        body=row['body']
        if (body.get('revoked') is not False or body.get('expires_at',0)<=tx.now
                or body.get('declaration_hash')!=plan.declaration_hash
                or not set(plan.node_ids)<=set(body.get('node_ids',[]))
                or not set(plan.allowed_actions)<=set(body.get('actions',[]))):
            raise PermissionError('environment authority does not cover frozen plan')

    def plan(self,declaration,*,mode,node_ids,deadline,authorization_ref=None,
             max_repair_revisions=2,max_steps=64,runtime_hash=None,approved_repair_hash=None):
        declaration=EnvironmentDeclaration.model_validate(declaration)
        nodes={node.node_id:node for node in declaration.nodes}
        if not node_ids or len(set(node_ids))!=len(node_ids) or not set(node_ids)<=set(nodes):
            raise ValueError('explicit distinct declared environment nodes required')
        if any(set(nodes[node].depends_on)-set(node_ids) for node in node_ids):
            raise ValueError('selected nodes omit a declared deployment dependency')
        with self.repository.transaction() as tx:
            if isinstance(deadline,bool) or deadline<=tx.now:raise ValueError('environment deadline must be in the future')
            auth=tx.get('environment_authorizations',authorization_ref) if authorization_ref else None
            value=EnvironmentScopePlan(graph_hash=environment_definition().hash,
                declaration_hash=declaration.declaration_hash,environment_id=declaration.environment_id,
                group_id=declaration.group_id,node_ids=tuple(sorted(node_ids)),mode=mode,
                authorization_ref=authorization_ref,authorization_hash=canonical_hash(auth['body']) if auth else None,
                authorization_revision=auth['revision'] if auth else None,
                allowed_actions=tuple(sorted(auth['body'].get('actions',()))) if auth else (),
                deadline=deadline,max_repair_revisions=max_repair_revisions,max_steps=max_steps,
                runtime_hash=runtime_hash,approved_repair_hash=approved_repair_hash)
            self.verify_authority_in(tx,value)
            tx.put('environment_declarations',declaration.declaration_hash,declaration.model_dump(mode='json'),immutable=True)
            tx.put('environment_plans',value.plan_hash,value.model_dump(mode='json'),immutable=True)
            tx.put('graph_definitions','environment:1',environment_definition().model_dump(mode='json'),immutable=True)
            return value

    def start(self,request_id,plan_hash):
        with self.repository.transaction() as tx:
            old=tx.get('environment_commands',request_id)
            if old:
                if old['body']['plan_hash']!=plan_hash:raise Conflict('environment request ID changed plan')
                return RunRecord.model_validate(old['body']['run'])
            row=tx.get('environment_plans',plan_hash)
            if not row:raise ValueError('frozen environment plan missing')
            plan=EnvironmentScopePlan.model_validate(row['body'])
            if plan.plan_hash!=plan_hash or plan.graph_hash!=environment_definition().hash:
                raise ValueError('environment plan/graph digest changed')
            if tx.now>=plan.deadline:raise ValueError('environment plan expired')
            self.verify_authority_in(tx,plan)
            if plan.mode=='prepare' and not isinstance(self.repository,PostgresRepository) and not getattr(self.repository,'is_test_fixture',False):
                raise PermissionError('real environment mutation requires PostgreSQL persistence')
            if plan.mode=='prepare' and isinstance(self.repository,PostgresRepository) and not plan.runtime_hash:
                raise PermissionError('real environment preparation requires a frozen runtime binding')
            run_id='environment:'+str(uuid4())
            run=RunRecord(run_id=run_id,root_thread_id=run_id+':root',graph_ref=plan.graph_ref,
                contract_ref='environment-contract:'+plan.plan_hash,active_plan_ref=plan.plan_hash,
                auth_ref=plan.authorization_ref or 'read-only',lineage_id=run_id)
            tx.put('runs',run_id,run.model_dump(mode='json'),immutable=True)
            tx.put('dispatch_gates',run_id,{'open':True,'generation':1},immutable=True)
            tx.put('closure_revisions',run_id,{'plan_ref':plan.plan_hash,'revision':0},immutable=True)
            tx.put('groups',run_id+':'+plan.group_id,{'run_id':run_id,'group_id':plan.group_id,
                'plan_ref':plan.plan_hash,'status':'pending','environment_ref':plan.declaration_hash},immutable=True)
            job_id=run_id+':prepare'
            TaskBackend.enqueue_in(tx,JobSpec(job_id=job_id,job_kind='environment',scope_ref=plan.group_id,
                thread_id=job_id,activation_id=job_id,dedupe_key=job_id,due_at=tx.now,deadline=plan.deadline,
                run_id=run_id,payload={'plan_ref':plan.plan_hash,'graph_ref':plan.graph_ref,
                    'environment_ref':plan.declaration_hash}))
            tx.put('environment_commands',request_id,{'plan_hash':plan_hash,'run':run.model_dump(mode='json')},immutable=True)
            return run

    def status(self,run_id):
        with self.repository.transaction() as tx:
            run=tx.get('runs',run_id)
            if not run or run['body']['graph_ref']!='environment:1':raise ValueError('environment run not found')
            return {'run':run['body'],'jobs':[row['body'] for row in tx.scan('jobs') if row['body'].get('run_id')==run_id],
                'deliveries':[row['body'] for row in tx.scan('environment_deliveries') if row['body'].get('run_id')==run_id]}

    def reconcile(self,request_id,run_id):
        """New observation activation, same frozen plan and original remote IDs."""
        with self.repository.transaction() as tx:
            old=tx.get('environment_reconcile_commands',request_id)
            if old:
                if old['body']['run_id']!=run_id:raise Conflict('environment reconcile request changed run')
                return old['body']['job_id']
            run=tx.get('runs',run_id)
            if not run or run['body']['graph_ref']!='environment:1':raise ValueError('environment run missing')
            jobs=[row['body'] for row in tx.scan('jobs') if row['body'].get('run_id')==run_id]
            if any(job['status'] in {'pending','running','waiting'} for job in jobs):
                raise Conflict('original environment activation must finish before reconciliation')
            if len(jobs)>=16:raise Conflict('environment observation activation budget exhausted')
            plan=EnvironmentScopePlan.model_validate(tx.get('environment_plans',run['body']['active_plan_ref'])['body'])
            identity=run_id+':reconcile:'+canonical_hash({'request_id':request_id})[:20]
            TaskBackend.enqueue_in(tx,JobSpec(job_id=identity,job_kind='environment',scope_ref=plan.group_id,
                thread_id=run_id+':prepare',activation_id=identity,dedupe_key=identity,due_at=tx.now,
                deadline=tx.now+300,run_id=run_id,payload={'plan_ref':plan.plan_hash,'graph_ref':plan.graph_ref,
                    'environment_ref':plan.declaration_hash,'original_run_ref':run_id,'environment_reconcile_ref':request_id}))
            tx.put('environment_reconcile_commands',request_id,{'run_id':run_id,'job_id':identity},immutable=True)
            return identity

    @staticmethod
    def reconciliation_job_in(tx,job):
        ref=job.get('payload',{}).get('environment_reconcile_ref')
        row=tx.get('environment_reconcile_commands',ref) if ref else None
        run=tx.get('runs',job.get('run_id',''))
        return bool(row and run and job['job_kind']=='environment'
            and row['body']=={'run_id':job['run_id'],'job_id':job['job_id']}
            and run['body']['graph_ref']=='environment:1'
            and run['body']['active_plan_ref']==job['payload'].get('plan_ref'))
