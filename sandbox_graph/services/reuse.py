"""Explicit compatible evidence references and finite waits for old deliveries."""
from ..domain.models import Model
from ..runtime.tasks import business_job_kind
from ..persistence.repository import Conflict

class ReuseContract(Model):
    environment_ref: str
    spec_hash: str
    method_ref: str
    completion_event: str
    time_domain: str
    script_ref: str
    condition_hash: str = ""
    measurement_contract_hash: str = ""
    valid_until: float

class ReuseService:
    def __init__(self,repository,review_service=None): self.repository=repository; self.reviews=review_service
    def consider(self,work_id,delivery_ref,*,old_contract,new_contract,deadline,allowed_source_scopes=()):
        old=old_contract.model_dump();new=new_contract.model_dump()
        old.pop('valid_until');new.pop('valid_until')
        if old!=new: raise Conflict('evidence compatibility contract mismatch')
        with self.repository.transaction() as tx:
            if min(old_contract.valid_until,new_contract.valid_until,deadline)<=tx.now: raise Conflict('reuse evidence or candidate expired')
            started=tx.get('work_instances',work_id)
            if started and started['body']['status'] not in {'pending','blocked'}: raise Conflict('independent measurement already started; old evidence is historical only')
            delivery=tx.get('deliveries',delivery_ref);review=tx.get('delivery_reviews',delivery_ref)
            if review and review['body']['decision']!='accepted': raise Conflict('rejected evidence cannot be reused')
            if delivery and delivery['body'].get('scope') and delivery['body']['scope'] not in set(allowed_source_scopes): raise PermissionError('source evidence scope is not explicitly authorized')
            accepted=bool(delivery and review)
            candidate={'work_id':work_id,'delivery_ref':delivery_ref,'old_contract':old_contract.model_dump(mode='json'),'new_contract':new_contract.model_dump(mode='json'),'deadline':deadline,'status':'accepted' if accepted else 'waiting','authorized_source_scopes':sorted(allowed_source_scopes)}
            previous=tx.get('reuse_candidates',work_id)
            if previous:
                immutable_keys=('work_id','delivery_ref','old_contract','new_contract','deadline','authorized_source_scopes')
                if any(previous['body'][k]!=candidate[k] for k in immutable_keys): raise Conflict('reuse candidate identity changed')
                if previous['body']['status'] in {'blocked','remeasure'}: raise Conflict('reuse candidate already disposed')
            tx.put('reuse_candidates',work_id,candidate)
            if accepted and not tx.get('reuse_refs',work_id): tx.put('reuse_refs',work_id,{'reuse_from':delivery_ref,'review_ref':review['body']['review_id'],'compatibility':old},immutable=True)
            return candidate
    def can_measure(self,work_id):
        with self.repository.transaction() as tx:
            row=tx.get('reuse_candidates',work_id)
            return row is None or row['body']['status']=='remeasure'
    def expire(self,work_id,*,policy):
        if policy not in {'blocked','remeasure'}: raise ValueError('explicit expiry disposition required')
        with self.repository.transaction() as tx:
            row=tx.get('reuse_candidates',work_id)
            if not row: raise KeyError(work_id)
            body=row['body']
            if body['status']!='waiting' or body['deadline']>tx.now: raise Conflict('candidate is not an expired pending wait')
            updated=body|{'status':policy};tx.put('reuse_candidates',work_id,updated)
            return updated

    def _validate_source(self,tx,delivery_ref,review_ref,old_contract,new_contract,target,allowed_scopes):
        from ..domain.models import DeliveryManifest,canonical_hash
        row=tx.get('deliveries',delivery_ref) or tx.get('work_deliveries',delivery_ref)
        review=tx.get('reviews',review_ref)
        current=tx.get('delivery_reviews',delivery_ref)
        if not row or not review or not current or current['body']['decision']!='accepted': raise Conflict('source delivery requires an accepted immutable review')
        source=DeliveryManifest.model_validate(row['body'])
        if source.scope not in allowed_scopes: raise PermissionError('source evidence scope is not explicitly authorized')
        if review['body'].get('decision')!='accepted' or review['body'].get('delivery_hash')!=canonical_hash(source): raise Conflict('source review does not bind the original delivery')
        if source.result.execution_status!='succeeded' or source.result.data_status!='valid': raise Conflict('source data is not verified reusable evidence')
        if source.result.output.get('reuse_projection_ref'): raise Conflict('reuse must name the original measurement delivery, not an alias')
        if source.result.output.get('reuse_contract')!=old_contract.model_dump(mode='json'): raise Conflict('source has no matching producer-sealed compatibility contract')
        old=old_contract.model_dump();new=new_contract.model_dump();old.pop('valid_until');new.pop('valid_until')
        if old!=new: raise Conflict('source and target compatibility contracts differ')
        if len(source.work_refs)!=1: raise Conflict('one target work requires one original measurement identity')
        original=tx.get('work_instances',source.work_refs[0])
        if not original: raise Conflict('original work identity is unavailable')
        original=original['body'];key=target['key']
        if original['plan_ref']!=source.plan_ref: raise Conflict('original work does not bind source plan')
        for work,contract in ((original,old_contract),(target,new_contract)):
            binding=tx.get('measurement_bindings',work['work_id'])
            if not binding or not contract.measurement_contract_hash: raise Conflict('producer measurement contract binding required')
            binding=binding['body']
            if (binding['work_id'],binding['run_id'],binding['plan_ref'],binding['measurement_contract_hash'],binding['script_ref'])!=(work['work_id'],work['run_id'],work['plan_ref'],contract.measurement_contract_hash,contract.script_ref): raise Conflict('producer measurement contract or script differs')
            if work['work_id']==source.work_refs[0] and binding['case_hash']!=source.result.output.get('case_hash'): raise Conflict('source case hash does not match producer binding')
        if any(original['key'][field]!=key[field] for field in ('scenario_id','method_id','method_version','condition_hash','repeat_id')): raise Conflict('measurement identity/repeat is not compatible')
        if target.get('environment_ref')!=new_contract.environment_ref or original.get('environment_ref')!=old_contract.environment_ref: raise Conflict('environment identity differs')
        if new_contract.method_ref!=f'{key["method_id"]}:{key["method_version"]}' or not new_contract.condition_hash or new_contract.condition_hash!=key['condition_hash']: raise Conflict('method or condition contract differs')
        if self.reviews is None: raise Conflict('independent artifact review service required for reuse')
        for evidence in source.evidence:
            if evidence.scope not in allowed_scopes: raise PermissionError('source artifact scope is not authorized')
            self.reviews.artifacts.verify(evidence)
        return source,review['body']

    def validate_projection(self,projection_ref):
        """Revalidate a frozen alias without changing original measurement identity.

        Freshness is evaluated at the persisted acceptance time; historical
        replay does not pretend it accepted evidence at today's time.
        """
        from ..domain.models import canonical_hash
        with self.repository.transaction() as tx:
            row=tx.get('reuse_projections',projection_ref)
            if not row: raise Conflict('reuse projection is unavailable')
            proof=row['body']
            core={k:v for k,v in proof.items() if k not in {'projection_ref','delivery_ref'}}
            if proof['projection_ref']!=projection_ref or canonical_hash(core)!=projection_ref: raise Conflict('reuse projection digest mismatch')
            target=tx.get('work_instances',proof['target_work_id'])
            if not target or (target['body']['run_id'],target['body']['plan_ref'],target['body']['group_id'])!=(proof['target_run_id'],proof['target_plan_ref'],proof['target_scope']): raise Conflict('reuse target identity changed')
            old=ReuseContract.model_validate(proof['compatibility']['old']);new=ReuseContract.model_validate(proof['compatibility']['new'])
            if proof['accepted_at']>=min(old.valid_until,new.valid_until,proof['deadline']): raise Conflict('reuse was accepted after its evidence expired')
            source,review=self._validate_source(tx,proof['source_delivery_ref'],proof['source_review_ref'],old,new,target['body'],set(proof['authorized_source_scopes']))
            if canonical_hash(source)!=proof['source_delivery_hash'] or source.plan_ref!=proof['source_plan_ref'] or source.contract_ref!=proof['source_contract_ref'] or list(source.work_refs)!=proof['source_work_refs']: raise Conflict('frozen source identity changed')
            return {'projection':proof,'source_delivery':source.model_dump(mode='json'),'source_review':review}

    def project_accepted(self,work_id,*,token):
        """Publish a reviewed reference delivery; never copy raw files or run work."""
        from ..domain.models import DeliveryManifest,NodeResult,canonical_hash
        from ..runtime.events import EventService,EventEnvelope
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            existing=tx.get('work_reuse_projections',work_id)
            if existing:
                proof=tx.get('reuse_projections',existing['body']['projection_ref'])['body']
                alias=DeliveryManifest.model_validate(tx.get('work_deliveries',proof['delivery_ref'])['body'])
            else:
                candidate=tx.get('reuse_candidates',work_id);work=tx.get('work_instances',work_id)
                if not candidate or candidate['body']['status']!='accepted' or not work: raise Conflict('accepted candidate and target work required')
                candidate=candidate['body'];work=work['body']
                run=tx.get('runs',work['run_id'])
                if not run or (run['body']['active_plan_ref'],run['body']['graph_ref'])!=(work['plan_ref'],work['graph_ref']): raise Conflict('reuse target is no longer in the active execution plan')
                if work['status'] not in {'pending','blocked'} or any(business_job_kind(j['body']['job_kind'])=='work' and j['body']['scope_ref']==work_id for j in tx.scan('jobs')): raise Conflict('independent measurement already started')
                old=ReuseContract.model_validate(candidate['old_contract']);new=ReuseContract.model_validate(candidate['new_contract'])
                if min(old.valid_until,new.valid_until,candidate['deadline'])<=tx.now: raise Conflict('reuse evidence or candidate expired')
                source_review=tx.get('delivery_reviews',candidate['delivery_ref'])
                if not source_review: raise Conflict('source has not been accepted')
                accepted=tx.get('reuse_refs',work_id)
                review_ref=accepted['body']['review_ref'] if accepted else source_review['body']['review_id']
                allowed=set(candidate.get('authorized_source_scopes',()))
                source,review=self._validate_source(tx,candidate['delivery_ref'],review_ref,old,new,work,allowed)
                proof={'target_work_id':work_id,'target_run_id':work['run_id'],'target_plan_ref':work['plan_ref'],'target_scope':work['group_id'],'source_delivery_ref':candidate['delivery_ref'],'source_review_ref':review_ref,'source_delivery_hash':canonical_hash(source),'source_plan_ref':source.plan_ref,'source_contract_ref':source.contract_ref,'source_work_refs':list(source.work_refs),'authorized_source_scopes':sorted(allowed),'compatibility':{'old':old.model_dump(mode='json'),'new':new.model_dump(mode='json')},'accepted_at':tx.now,'deadline':candidate['deadline'],'sample_identity':canonical_hash({'source_delivery_ref':candidate['delivery_ref'],'source_work_refs':list(source.work_refs)})}
                identity=canonical_hash(proof)
                alias=DeliveryManifest(delivery_id=f'reuse:{identity}',version=1,scope=work['group_id'],plan_ref=work['plan_ref'],contract_ref=work['contract_ref'],work_refs=(work_id,),evidence=(),result=NodeResult(execution_status='succeeded',data_status='valid',cleanup_status=source.result.cleanup_status,coverage_status='completed',output={'reuse_projection_ref':identity,'source_sample_identity':proof['sample_identity']}))
                key=f'{alias.delivery_id}:1';proof=proof|{'projection_ref':identity,'delivery_ref':key}
                tx.put('reuse_projections',identity,proof,immutable=True)
                tx.put('work_reuse_projections',work_id,{'projection_ref':identity},immutable=True)
                tx.put('work_deliveries',key,alias.model_dump(mode='json'),immutable=True)
                tx.put('work_delivery_owners',key,{'run_id':work['run_id'],'group_id':work['group_id'],'work_id':work_id},immutable=True)
        self.validate_projection(proof['projection_ref'])
        reviewed=self.reviews.review_delivery(alias,expected_scope=alias.scope,plan_ref=alias.plan_ref,contract_ref=alias.contract_ref)
        if reviewed['decision']!='accepted': raise Conflict('projected evidence failed independent review')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            work=tx.get('work_instances',work_id)['body']
            run=tx.get('runs',work['run_id'])
            if not run or (run['body']['active_plan_ref'],run['body']['graph_ref'])!=(work['plan_ref'],work['graph_ref']): raise Conflict('reuse target changed before publication')
            if work['status'] not in {'pending','blocked','delivered'}: raise Conflict('target measurement started during reuse publication')
            tx.put('work_instances',work_id,work|{'status':'delivered','reuse_from':proof['source_delivery_ref'],'reuse_projection_ref':proof['projection_ref'],'result_ref':proof['delivery_ref'],'queue_reason':None})
            identity=f'reuse-delivered:{proof["projection_ref"]}'
            if not tx.get('event_ids',identity):
                aggregate=f'group:{work["run_id"]}:{work["group_id"]}'
                seq=max((r['body']['aggregate_seq'] for r in tx.scan('events') if r['body']['aggregate_id']==aggregate),default=0)+1
                EventService.append_in(tx,EventEnvelope(event_id=identity,producer_scope=work_id,producer_epoch=token.owner_epoch,aggregate_id=aggregate,aggregate_seq=seq,run_ref=work['run_id'],plan_ref=work['plan_ref'],graph_ref=work['graph_ref'],kind='reuse_delivered',payload={'delivery_ref':proof['delivery_ref'],'projection_ref':proof['projection_ref']}),token)
        return alias
