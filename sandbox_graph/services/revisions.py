"""Linearized dispatch permits and immutable graph-version succession."""
from uuid import uuid4
from ..domain.models import RunRecord, canonical_hash
from ..persistence.repository import Conflict

class RevisionService:
    def __init__(self,repository): self.repository=repository
    def create(self,run):
        with self.repository.transaction() as tx:
            tx.put('runs',run.run_id,run.model_dump(mode='json'),immutable=True)
            tx.put('dispatch_gates',run.run_id,{'open':True,'generation':run.dispatch_generation},immutable=True)
            tx.put('closure_revisions',run.run_id,{'plan_ref':run.active_plan_ref,'revision':0},immutable=True)
    @staticmethod
    def permit_in(tx,run_id,operation_id,*,generation,scope,kind='ordinary'):
        request={'run_id':run_id,'operation_id':operation_id,'generation':generation,'scope':scope,'kind':kind}
        existing=tx.get('submission_permits',operation_id)
        if existing:
            if existing['body']!=request: raise Conflict('operation permit identity conflict')
            return operation_id
        gate=tx.get('dispatch_gates',run_id)
        if not gate: raise Conflict('run dispatch gate missing')
        if kind=='ordinary' and (not gate['body']['open'] or gate['body']['generation']!=generation): raise Conflict('dispatch gate closed or generation superseded')
        if kind not in {'ordinary','drain','cleanup'}: raise Conflict('unregistered submission kind')
        if kind in {'drain','cleanup'}:
            owned=any(r['body'].get('scope')==scope and r['body'].get('run_id')==run_id for r in tx.scan('submission_permits'))
            if not owned: raise Conflict('drain/cleanup scope has no prior submission permit')
        tx.put('submission_permits',operation_id,request,immutable=True)
        return operation_id
    def permit(self,run_id,operation_id,*,generation,scope,kind='ordinary'):
        with self.repository.transaction() as tx: return self.permit_in(tx,run_id,operation_id,generation=generation,scope=scope,kind=kind)
    def activate_revision(self,*,request_id,predecessor_run_id,expected_revision,candidate_plan_hash,graph_ref,disposition_policy):
        if disposition_policy not in {'drain','cancel'}: raise ValueError('explicit drain/cancel disposition required')
        args={'predecessor_run_id':predecessor_run_id,'expected_revision':expected_revision,'candidate_plan_hash':candidate_plan_hash,'graph_ref':graph_ref,'disposition_policy':disposition_policy}
        digest=canonical_hash(args)
        with self.repository.transaction() as tx:
            replay=tx.get('revision_activations',request_id)
            if replay:
                if replay['body']['hash']!=digest: raise Conflict('request_id input mismatch')
                return RunRecord.model_validate(replay['body']['result'])
            row=tx.get('runs',predecessor_run_id)
            if not row or row['revision']!=expected_revision: raise Conflict('predecessor revision conflict')
            old=RunRecord.model_validate(row['body'])
            if old.successor_run_id: raise Conflict('predecessor already has successor')
            generation=old.dispatch_generation+1
            if graph_ref==old.graph_ref:
                successor=old.model_copy(update={'active_plan_ref':candidate_plan_hash,'revision':expected_revision+1,'dispatch_generation':generation})
                tx.put('runs',old.run_id,successor.model_dump(mode='json'),expected_revision=expected_revision)
                tx.put('dispatch_gates',old.run_id,{'open':True,'generation':generation})
            else:
                identity=str(uuid4())
                successor=RunRecord(run_id=identity,root_thread_id=f'run:{identity}',graph_ref=graph_ref,contract_ref=old.contract_ref,active_plan_ref=candidate_plan_hash,auth_ref=old.auth_ref,lineage_id=old.lineage_id,supersedes_run_id=old.run_id)
                tx.put('runs',old.run_id,old.model_copy(update={'revision':expected_revision+1,'successor_run_id':identity,'dispatch_generation':generation}).model_dump(mode='json'),expected_revision=expected_revision)
                tx.put('dispatch_gates',old.run_id,{'open':False,'generation':generation,'policy':disposition_policy})
                tx.put('runs',identity,successor.model_dump(mode='json'),immutable=True)
                tx.put('dispatch_gates',identity,{'open':True,'generation':1},immutable=True)
            for job in tx.scan('jobs'):
                b=job['body']
                if b.get('run_id')==old.run_id and b['job_kind'] not in {'cleanup','reconcile'} and b['status'] in {'pending','running'}:
                    permits=[p for p in tx.scan('submission_permits') if p['body']['run_id']==old.run_id and p['body']['scope']==b['scope_ref']]
                    tx.put('jobs',job['key'],b|{'status':'draining' if permits else 'superseded','disposition':disposition_policy})
            tx.put('revision_activations',request_id,{'hash':digest,'result':successor.model_dump(mode='json'),'policy':disposition_policy},immutable=True)
            closure=tx.get('closure_revisions',successor.run_id)
            tx.put('closure_revisions',successor.run_id,{'plan_ref':candidate_plan_hash,'revision':closure['body']['revision']+1 if closure else 0})
            tx.put('outbox',f'revision:{request_id}',{'kind':'activate','run_id':successor.run_id,'plan_ref':candidate_plan_hash},immutable=True)
            return successor
