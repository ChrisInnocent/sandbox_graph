"""Trusted explicit new-measure authority and atomic, replay-safe generation work.

The public authorize API is an operator boundary, never exposed as a model tool.
It grants measurement only; lifecycle and lineage limits are inherited unchanged.
"""
from uuid import uuid4
from ..domain.models import RunRecord, WorkKey, canonical_hash
from ..planning.compiler import Plan
from ..planning.measurement_generation import MeasurementGenerationPlan
from ..planning.identity import condition_identity
from ..persistence.repository import Conflict
from .agent_lifecycle import LifecycleService
from .lifecycle_safety import LifecycleSafetyService
from .lifecycle_budgets import LifecycleBudgetService


class MeasurementGenerationService:
    def __init__(self,repository): self.repository=repository

    @staticmethod
    def _material_in(tx,work):
        scope=work['work_id']
        return {'work':tx.get('work_instances',scope),
            'contract':tx.get('contracts',work['contract_ref']),
            'graph':tx.get('graph_definitions',work['graph_ref']),
            'binding':tx.get('measurement_bindings',scope),
            'receipts':[r for r in tx.scan('receipts') if r['body'].get('identity',{}).get('scope')==scope],
            **{kind:LifecycleSafetyService._rows_in(tx,kind,scope)
               for kind in ('operations','resources','observations')},
            'artifacts':[r for r in tx.scan('artifacts') if r['body'].get('scope')==scope]}

    @staticmethod
    def _load_in(tx,ref):
        row=tx.get('plans',ref)
        if not row: raise Conflict('generation plan missing')
        plan=MeasurementGenerationPlan.model_validate(row['body'])
        if plan.hash!=ref: raise Conflict('generation plan digest mismatch')
        return plan

    @classmethod
    def _failure_source_in(cls,tx,record,failure_ref):
        """Resolve one trusted child receipt through the actual successor chain.

        Historic grants need not remain live: this is provenance, not permission
        to execute. A fresh exact envelope/grant authorizes the next generation.
        """
        row=tx.get('generation_failure_links',failure_ref)
        if not row: raise Conflict('exact generation failure link missing')
        link=row['body']; scope=link.get('work_ref'); lifecycle=record.key.lifecycle_id
        work=tx.get('work_instances',scope) if scope else None
        receipt=tx.get('receipts',failure_ref)
        if (not work or not receipt or scope==record.key.scope_ref
            or link.get('receipt_ref')!=failure_ref or link.get('source_lifecycle_ref')!=lifecycle
            or link.get('status')!='blocked_needs_decision'
            or link.get('node_id') not in {'N04','N05','N06','N07','N08'}):
            raise Conflict('child failure link identity or state differs')
        body=work['body']; identity=receipt['body']['identity']; result=receipt['body']['result']
        expected={'scope':scope,'run_ref':body['run_id'],'plan_ref':body['plan_ref'],
                  'graph_ref':body['graph_ref'],'node_id':link['node_id']}
        review=tx.get('node_reviews',failure_ref)
        if (any(identity.get(k)!=v for k,v in expected.items())
            or link.get('run_ref')!=body['run_id'] or link.get('plan_ref')!=body['plan_ref']
            or link.get('source_work_ref')!=body.get('source_work_ref')
            or not (result.get('execution_status') in {'failed','blocked'}
                    or result.get('review_status')=='rejected'
                    or review and review['body'].get('decision')=='reject')):
            raise Conflict('child link does not identify an actual failed receipt')
        ancestry=[]; seen=set(); current=body
        while current['work_id']!=record.key.scope_ref:
            if current['work_id'] in seen: raise Conflict('generation ancestry cycle')
            seen.add(current['work_id'])
            prior=cls._load_in(tx,current.get('measurement_plan_ref',''))
            if prior.source_lifecycle_ref!=lifecycle or prior.lineage_id!=record.key.lineage_budget_ref:
                raise Conflict('generation ancestry belongs to another original lifecycle')
            cls.validate_work_in(tx,prior,current)
            cls.validate_job_in(tx,{'job_kind':'measurement-v1:work','run_id':current['run_id'],
                'scope_ref':current['work_id'],'payload':{'plan_ref':prior.hash,'graph_ref':current['graph_ref']}})
            successor=tx.get('measurement_successors',prior.source_work_ref)
            expected_successor={'run_id':current['run_id'],'work_id':current['work_id'],'plan_ref':prior.hash}
            previous=tx.get('work_instances',prior.source_work_ref)
            if (not successor or successor['body']!=expected_successor or not previous
                or previous['body'].get('key')!=prior.source_key.model_dump(mode='json')):
                raise Conflict('generation immediate predecessor/successor identity differs')
            ancestry.append({'plan_ref':prior.hash,'successor':successor})
            current=previous['body']
        if (current['run_id'],current['plan_ref'],current['graph_ref'],current['contract_ref'])!=(
            record.key.run_ref,record.plan_ref,record.graph_ref,record.contract_ref):
            raise Conflict('generation root lifecycle identity differs')
        provenance={'kind':'generation-failure.v1','failure_link':row,'ancestry':ancestry,
            'root':{'key':record.key.model_dump(mode='json'),'source_receipt':record.source_receipt,
                    'plan_ref':record.plan_ref,'graph_ref':record.graph_ref,'contract_ref':record.contract_ref}}
        return work,receipt,provenance

    @classmethod
    def _source_material_in(cls,tx,work,provenance=None):
        material=cls._material_in(tx,work)
        # Preserve the original v1 root-source encoding byte for byte.
        return material if provenance is None else {'source':material,'provenance':provenance}

    def compile(self,lifecycle_ref,*,target_plan_ref,authorization_ref,token,source_failure_ref=None):
        with self.repository.transaction() as tx:
            LifecycleService.assert_owner_in(tx,lifecycle_ref,token)
            record=LifecycleService.load_in(tx,lifecycle_ref)
            provenance=None
            if source_failure_ref is not None:
                source,receipt,provenance=self._failure_source_in(tx,record,source_failure_ref)
                source_receipt_ref=source_failure_ref
                source_node=receipt['body']['identity']['node_id']
            else:
                source=tx.get('work_instances',record.key.scope_ref)
                source_receipt_ref=record.source_receipt
                source_node=record.key.node_id
                receipt=tx.get('receipts',source_receipt_ref)
            if not source: raise Conflict('source work missing')
            LifecycleSafetyService.assert_work_in(tx,source['key'],token)
            run=tx.get('runs',source['body']['run_id'])
            target=tx.get('plans',target_plan_ref)
            if not target: raise Conflict('target plan missing')
            target=Plan.model_validate(target['body'])
            if target.hash!=target_plan_ref: raise Conflict('target plan digest mismatch')
            work=source['body']; key=WorkKey.model_validate(work['key'])
            from .recovery import RecoveryService
            RecoveryService._versions_in(tx,work)
            if run['body']['active_plan_ref']!=work['plan_ref'] or run['body']['graph_ref']!=work['graph_ref']:
                raise Conflict('source run no longer owns frozen plan/graph')
            original=tx.get('plans',work['plan_ref'])
            if not original: raise Conflict('original experiment plan missing')
            from ..planning.measurement_generation import load_plan
            original=load_plan(original['body'])
            if original.hash!=work['plan_ref']: raise Conflict('source plan digest mismatch')
            identity=receipt['body']['identity'] if receipt else {}
            expected={'scope':work['work_id'],'run_ref':work['run_id'],'plan_ref':work['plan_ref'],
                'graph_ref':work['graph_ref'],'node_id':source_node}
            if any(identity.get(k)!=v for k,v in expected.items()): raise Conflict('source receipt identity changed')
            if source_node not in {'N04','N05','N06','N07','N08'}: raise Conflict('measurement issue required')
            result=receipt['body']['result']
            if result.get('execution_status')=='succeeded' and result.get('data_status')=='valid' and result.get('review_status')!='rejected':
                raise Conflict('valid performance result does not authorize remeasurement')
            if target.graph_ref!=work['graph_ref'] or target.contract_ref!=work['contract_ref']:
                raise Conflict('target graph/contract differs from frozen experiment')
            matches=[c for c in target.cases if (c.group_id,c.method_id,c.method_version,c.scenario_id,c.repeat_id)==
                (key.group_id,key.method_id,key.method_version,key.scenario_id,key.repeat_id)
                and work['condition'] in c.conditions
                and canonical_hash(condition_identity(c,work['condition']))==key.condition_hash
                and sorted(c.conflict_domains)==sorted(work['domains'])
                and c.contract_ref==work['contract_ref'] and c.environment_ref==work['environment_ref']]
            if len(matches)!=1: raise Conflict('exact original method/resource/condition required')
            prior=[c for c in original.cases if c.case_id==matches[0].case_id]
            if len(prior)!=1 or prior[0].model_dump(exclude={'conditions'})!=matches[0].model_dump(exclude={'conditions'}):
                raise Conflict('frozen method/resource/endpoint contract changed')
            budget=LifecycleBudgetService.snapshot_in(tx,lifecycle_ref)
            if budget['lifecycle']['lineage_budget_ref']!=run['body']['lineage_id']: raise Conflict('source lineage differs')
            plan=MeasurementGenerationPlan(target_plan=target,target_plan_hash=target.hash,target_case_id=matches[0].case_id,
                source_work_ref=work['work_id'],source_work_revision=source['revision'],source_run_ref=work['run_id'],
                source_run_revision=run['revision'],source_plan_ref=work['plan_ref'],source_graph_ref=work['graph_ref'],
                source_contract_ref=work['contract_ref'],source_receipt_ref=source_receipt_ref,source_node=source_node,
                source_lifecycle_ref=lifecycle_ref,source_lifecycle_revision=record.revision,lineage_id=run['body']['lineage_id'],
                source_key=key,source_material_hash=canonical_hash(self._source_material_in(tx,work,provenance)),condition=work['condition'],
                authorization_ref=authorization_ref,measurement_generation=key.measurement_generation+1)
            if provenance is not None:
                tx.put('generation_source_bindings',plan.hash,provenance,immutable=True)
            tx.put('plans',plan.hash,plan.model_dump(mode='json'),immutable=True)
            return plan

    @classmethod
    def _source_in(cls,tx,plan):
        source=tx.get('work_instances',plan.source_work_ref); run=tx.get('runs',plan.source_run_ref)
        record=LifecycleService.load_in(tx,plan.source_lifecycle_ref)
        provenance=None
        if plan.source_work_ref!=record.key.scope_ref:
            source,receipt,provenance=cls._failure_source_in(tx,record,plan.source_receipt_ref)
            frozen=tx.get('generation_source_bindings',plan.hash)
            if (not frozen or frozen['body']!=provenance or source['key']!=plan.source_work_ref
                or receipt['body']['identity']['node_id']!=plan.source_node):
                raise Conflict('frozen child source provenance changed')
        elif (record.source_receipt!=plan.source_receipt_ref or record.key.node_id!=plan.source_node
              or (record.key.run_ref,record.plan_ref,record.graph_ref,record.contract_ref)!=(
                  plan.source_run_ref,plan.source_plan_ref,plan.source_graph_ref,plan.source_contract_ref)):
            raise Conflict('frozen original lifecycle identity changed')
        if (not source or source['revision']!=plan.source_work_revision or not run or run['revision']!=plan.source_run_revision
            or record.revision!=plan.source_lifecycle_revision or record.key.lineage_budget_ref!=plan.lineage_id
            or any(source['body'].get(k)!=v for k,v in {'run_id':plan.source_run_ref,
                'plan_ref':plan.source_plan_ref,'graph_ref':plan.source_graph_ref,'contract_ref':plan.source_contract_ref}.items())
            or canonical_hash(cls._source_material_in(tx,source['body'],provenance))!=plan.source_material_hash):
            raise Conflict('frozen generation source revision/evidence changed')
        return source['body']

    def authorize(self,authorization_ref,plan_ref,*,principal,expires_at,permissions):
        if not principal or tuple(permissions)!=('measure',): raise PermissionError('explicit measure permission required')
        with self.repository.transaction() as tx:
            plan=self._load_in(tx,plan_ref); self._source_in(tx,plan)
            budget=LifecycleBudgetService.snapshot_in(tx,plan.source_lifecycle_ref)
            if authorization_ref!=plan.authorization_ref or not tx.now<expires_at<=min(b['deadline'] for b in budget.values()):
                raise PermissionError('authorization identity or absolute deadline differs')
            tx.put('measurement_authorizations',authorization_ref,{'plan_ref':plan_ref,'principal':principal,
                'expires_at':expires_at,'permissions':['measure'],'revoked':False},immutable=True)
        return authorization_ref

    def revoke(self,authorization_ref):
        with self.repository.transaction() as tx:
            row=tx.get('measurement_authorizations',authorization_ref)
            if not row: raise KeyError(authorization_ref)
            tx.put('measurement_authorizations',authorization_ref,row['body']|{'revoked':True})

    @staticmethod
    def _authority_in(tx,plan,*,principal=None):
        auth=tx.get('measurement_authorizations',plan.authorization_ref)
        if (not auth or auth['revision']!=plan.authorization_revision or auth['body'].get('revoked') is not False
            or auth['body'].get('expires_at',0)<=tx.now or auth['body'].get('permissions')!=['measure']
            or auth['body'].get('plan_ref')!=plan.hash or (principal is not None and auth['body'].get('principal')!=principal)):
            raise PermissionError('explicit generation authorization missing, changed or revoked')
        return auth['body']

    def request(self,request_id,plan_ref,*,authorization_ref,principal,token):
        inputs={'plan_ref':plan_ref,'authorization_ref':authorization_ref,'principal':principal}
        with self.repository.transaction() as tx:
            plan=self._load_in(tx,plan_ref)
            LifecycleService.assert_owner_in(tx,plan.source_lifecycle_ref,token)
            LifecycleSafetyService.assert_work_in(tx,plan.source_work_ref,token)
            if authorization_ref!=plan.authorization_ref: raise PermissionError('generation authorization differs')
            auth=self._authority_in(tx,plan,principal=principal)
            self._source_in(tx,plan)
            old=tx.get('measurement_generation_requests',request_id)
            if old:
                if old['body']['inputs']!=inputs: raise Conflict('generation request input changed')
                return old['body']['result']
            from .lifecycle_stop import LifecycleStopService
            LifecycleStopService.assert_admission_in(tx,LifecycleService.load_in(tx,plan.source_lifecycle_ref))
            if tx.get('measurement_successors',plan.source_work_ref): raise Conflict('source generation already has a successor')
            proof=LifecycleSafetyService.produce_disposition_in(tx,plan.source_work_ref,token,ttl=auth['expires_at']-tx.now)
            LifecycleSafetyService.verify_disposition_in(tx,proof,plan.source_work_ref,token,require_closed=True)
            if any(tx.get('resource_leases',domain) for domain in plan.selected_case.conflict_domains):
                raise Conflict('original conflict domains are not released')
            identity=str(uuid4())
            run=RunRecord(run_id=identity,root_thread_id=f'run:{identity}',graph_ref=plan.target_plan.graph_ref,
                contract_ref=plan.target_plan.contract_ref,active_plan_ref=plan.hash,auth_ref=authorization_ref,
                lineage_id=plan.lineage_id,supersedes_run_id=plan.source_run_ref)
            tx.put('runs',identity,run.model_dump(mode='json'),immutable=True)
            tx.put('dispatch_gates',identity,{'open':True,'generation':1},immutable=True)
            tx.put('closure_revisions',identity,{'plan_ref':plan.hash,'revision':0},immutable=True)
            tx.put('run_generation_bindings',identity,{'plan_ref':plan.hash,'run_revision':0,
                'authorization_ref':authorization_ref,'authorization_revision':plan.authorization_revision,
                'source_lifecycle_ref':plan.source_lifecycle_ref,'closure_proof_ref':proof},immutable=True)
            from .scheduling import SchedulingService
            ids=SchedulingService.expand_work_in(tx,identity,plan.source_key.group_id,plan_ref=plan.hash)
            if len(ids)!=1: raise Conflict('generation must expand exactly one work')
            result={'run_id':identity,'work_id':ids[0],'plan_ref':plan.hash}
            tx.put('measurement_generation_requests',request_id,{'inputs':inputs,'result':result},immutable=True)
            tx.put('measurement_successors',plan.source_work_ref,result,immutable=True)
            return result

    @classmethod
    def validate_run_in(cls,tx,run_id,*,plan_ref=None):
        binding=tx.get('run_generation_bindings',run_id)
        if not binding:
            run=tx.get('runs',run_id)
            stored=tx.get('plans',run['body']['active_plan_ref']) if run else None
            if stored and stored['body'].get('kind'):
                raise Conflict('generation plan requires immutable authorized run binding')
            return None
        plan=cls._load_in(tx,binding['body']['plan_ref']); cls._authority_in(tx,plan); cls._source_in(tx,plan)
        run=tx.get('runs',run_id); gate=tx.get('dispatch_gates',run_id)
        if (not run or run['revision']!=binding['body']['run_revision'] or run['body']['active_plan_ref']!=plan.hash
            or run['body']['graph_ref']!=plan.target_plan.graph_ref or run['body']['auth_ref']!=plan.authorization_ref
            or run['body']['lineage_id']!=plan.lineage_id or (plan_ref is not None and plan_ref!=plan.hash)
            or not gate or not gate['body']['open'] or gate['body']['generation']!=run['body']['dispatch_generation']):
            raise Conflict('generation run version or dispatch gate changed')
        # The original closure remains authoritative after its producer releases
        # its lease; any new operation/resource/observation changes source hash.
        snapshot=LifecycleSafetyService._snapshot_in(tx,plan.source_work_ref)
        disposition=LifecycleSafetyService._receipt_in(tx,snapshot,observed_at=tx.now,expires_at=tx.now+1)
        if not disposition.stopped or disposition.residual_resources or disposition.isolation_domains:
            raise Conflict('source site no longer closed')
        return plan

    @staticmethod
    def validate_work_in(tx,plan,work):
        key=plan.source_key.model_copy(update={'measurement_generation':plan.measurement_generation})
        expected_id=f"{work['run_id']}:{plan.hash}:{key.identity}"
        case=plan.selected_case
        from ..planning.identity import binding_payload
        expected={'work_id':expected_id,'plan_ref':plan.hash,'graph_ref':plan.target_plan.graph_ref,
            'contract_ref':case.contract_ref,'environment_ref':case.environment_ref,
            'key':key.model_dump(mode='json'),'condition':plan.condition,'group_id':case.group_id,
            'domains':list(case.conflict_domains),'source_work_ref':plan.source_work_ref,
            'source_lifecycle_ref':plan.source_lifecycle_ref,'measurement_plan_ref':plan.hash,
            **binding_payload(case)}
        if any(work.get(k)!=v for k,v in expected.items()):
            raise Conflict('generation work differs from frozen experiment')

    @classmethod
    def validate_job_in(cls,tx,job):
        """Wire normalization is not authority. Safety retains exact identities.

        Gate/measure revocation does not revoke a queued original-ID safety job;
        its actual handler still enforces job/thread/work and operation scopes.
        """
        from .lifecycle_stop_jobs import StopJobs
        if StopJobs.valid_in(tx,job): return
        encoded=job['job_kind'].startswith('measurement-v1:')
        binding=tx.get('run_generation_bindings',job.get('run_id',''))
        if not encoded and not binding: return
        if not encoded or not binding: raise Conflict('generation job version/binding missing')
        plan=cls._load_in(tx,binding['body']['plan_ref'])
        run=tx.get('runs',job['run_id'])
        if (not run or run['revision']!=binding['body']['run_revision']
            or run['body']['active_plan_ref']!=plan.hash or run['body']['auth_ref']!=plan.authorization_ref
            or run['body']['lineage_id']!=plan.lineage_id or run['body']['graph_ref']!=plan.source_graph_ref
            or job['payload'].get('plan_ref')!=plan.hash or job['payload'].get('graph_ref')!=plan.source_graph_ref):
            raise Conflict('generation job frozen run/plan/graph identity differs')
        work=tx.get('work_instances',job['scope_ref'])
        if work:
            if work['body']['run_id']!=job['run_id']: raise Conflict('generation job work belongs to another run')
            cls.validate_work_in(tx,plan,work['body'])
        elif job['scope_ref'] not in {job['run_id'],plan.source_key.group_id}:
            raise Conflict('generation job scope outside frozen target')

    @classmethod
    def validate_submission_in(cls,tx,run_id,scope,*,kind='ordinary',token=None,node_id=None):
        """Check each new effect in the permit/submission_started transaction.

        A winning admission may finish after revocation. Reconciliation of that
        original ID does not pass here; fresh IDs always do, even with a permit
        previously read via another entrypoint.
        """
        binding=tx.get('run_generation_bindings',run_id)
        run=tx.get('runs',run_id)
        stored=tx.get('plans',run['body']['active_plan_ref']) if run else None
        if not binding and not (stored and stored['body'].get('kind')): return
        if not binding: raise Conflict('generation submission binding missing')
        work=tx.get('work_instances',scope)
        if not work or work['body']['run_id']!=run_id: raise Conflict('generation submission work scope differs')
        if kind=='ordinary':
            plan=cls.validate_run_in(tx,run_id,plan_ref=work['body']['plan_ref'])
            cls.validate_work_in(tx,plan,work['body'])
        else:
            if node_id not in {'N07','N09','N10'}:
                raise PermissionError('generation safety submission requires a mandatory tail node')
            cls.validate_job_in(tx,{'job_kind':'measurement-v1:'+kind,'run_id':run_id,'scope_ref':scope,
                'payload':{'plan_ref':work['body']['plan_ref'],'graph_ref':work['body']['graph_ref']}})
        if token is not None: LifecycleSafetyService.assert_work_in(tx,scope,token)
