"""Append-only reviews and frozen analysis input manifests."""
from uuid import uuid4
from ..domain.models import DeliveryManifest,canonical_hash
from ..persistence.repository import Conflict

class ReviewService:
    def __init__(self,repository,artifacts): self.repository=repository; self.artifacts=artifacts
    def review_delivery(self,delivery: DeliveryManifest,*,expected_scope,plan_ref,contract_ref,rules_ref='manifest:1',allowed_evidence_scopes=None):
        findings=[]
        projection_verified=False
        projection_ref=delivery.result.output.get('reuse_projection_ref')
        if projection_ref:
            try:
                from .reuse import ReuseService
                resolved=ReuseService(self.repository,self).validate_projection(projection_ref)
                proof=resolved['projection']
                if proof['delivery_ref']!=f'{delivery.delivery_id}:{delivery.version}' or proof['target_work_id'] not in delivery.work_refs or (proof['target_scope'],proof['target_plan_ref'])!=(delivery.scope,delivery.plan_ref): raise Conflict('alias delivery does not match its projection')
                projection_verified=True
            except (OSError,ValueError,PermissionError) as exc: findings.append(f'reuse projection unavailable: {type(exc).__name__}')
        if not delivery.evidence and not projection_ref:
            with self.repository.transaction() as tx:
                group=tx.get('group_deliveries',f'{delivery.delivery_id}:{delivery.version}')
                sources=tx.get('group_delivery_sources',f'{delivery.delivery_id}:{delivery.version}')
                children=[tx.get('work_deliveries',key) for key in sources['body']['child_delivery_refs']] if sources and group and group['body']==delivery.model_dump(mode='json') else []
            if children and all(child and child['body']['result']['output'].get('reuse_projection_ref') for child in children):
                try:
                    from .reuse import ReuseService
                    for child in children: ReuseService(self.repository,self).validate_projection(child['body']['result']['output']['reuse_projection_ref'])
                    projection_verified=True
                except (OSError,ValueError,PermissionError) as exc: findings.append(f'group reuse projection unavailable: {type(exc).__name__}')
        if (delivery.scope,delivery.plan_ref,delivery.contract_ref)!=(expected_scope,plan_ref,contract_ref): findings.append('identity/contract mismatch')
        for ref in delivery.evidence:
            if ref.scope not in (set(allowed_evidence_scopes) if allowed_evidence_scopes is not None else {expected_scope}): findings.append('cross-scope evidence'); continue
            try: self.artifacts.verify(ref)
            except (OSError,ValueError) as exc: findings.append(f'evidence unavailable: {type(exc).__name__}')
        if delivery.result.coverage_status!='unsupported' and delivery.result.data_status=='valid' and not delivery.evidence and not projection_verified: findings.append('valid data requires raw evidence')
        if delivery.result.data_status in {'invalid','insufficient','unverified'} and delivery.result.coverage_status!='unsupported': findings.append(f'data {delivery.result.data_status}')
        decision='rejected' if findings else 'accepted'
        review={'delivery_id':delivery.delivery_id,'version':delivery.version,'scope':expected_scope,'rules_ref':rules_ref,'findings':findings,'decision':decision,'delivery_hash':canonical_hash(delivery)}
        with self.repository.transaction() as tx:
            key=f'{delivery.delivery_id}:{delivery.version}'
            tx.put('deliveries',key,delivery.model_dump(mode='json'),immutable=True)
            projection=tx.get('delivery_reviews',key)
            previous=tx.get('reviews',projection['body']['review_id']) if projection else None
            if previous and all(previous['body'].get(k)==v for k,v in review.items()): return previous['body']
            review=review|{'previous_review_ref':previous['body']['review_id'] if previous else None}
            review={'review_id':canonical_hash(review)}|review
            tx.put('reviews',review['review_id'],review,immutable=True)
            tx.put('delivery_reviews',key,{'review_id':review['review_id'],'decision':decision})
            tx.put('analysis_dirty',plan_ref,{'dirty':True,'delivery_ref':key})
            for run in tx.scan('runs'):
                if run['body']['active_plan_ref']==plan_ref:
                    closure=tx.get('closure_revisions',run['key'])
                    tx.put('closure_revisions',run['key'],{'plan_ref':plan_ref,'revision':closure['body']['revision']+1 if closure else 1})
            tx.put('outbox',f'review:{review["review_id"]}',{'kind':'delivery_reviewed','delivery_ref':key,'plan_ref':plan_ref},immutable=True)
        return review
    def freeze_analysis(self,plan_hash,deliveries,*,rules_ref,missing_scope=(),source_kind='deliveries'):
        if source_kind not in {'deliveries','group_deliveries'}: raise ValueError('invalid frozen delivery source')
        with self.repository.transaction() as tx:
            records={};reviews={}
            for scope,(identity,version) in sorted(deliveries.items()):
                key=f'{identity}:{version}'; row=tx.get(source_kind,key); review=tx.get('delivery_reviews',key)
                if not row or not review: raise Conflict('delivery has not been reviewed')
                if row['body']['scope']!=scope or row['body']['plan_ref']!=plan_hash: raise Conflict('analysis delivery identity mismatch')
                records[scope]=[identity,version];reviews[scope]=review['body']['review_id']
            manifest={'plan_hash':plan_hash,'deliveries':records,'reviews':reviews,'rules_ref':rules_ref,'missing_scope':list(missing_scope),'delivery_kind':source_kind}
            identity=canonical_hash(manifest)
            tx.put('analysis_inputs',identity,manifest,immutable=True)
            tx.put('analysis_dirty',plan_hash,{'dirty':False,'input_ref':identity})
            return identity
    def get_analysis(self,identity):
        with self.repository.transaction() as tx:
            row=tx.get('analysis_inputs',identity)
            if not row: raise KeyError(identity)
            return row['body']
    def resolve_analysis(self,identity):
        manifest=self.get_analysis(identity)
        groups={}
        with self.repository.transaction() as tx:
            for scope,(delivery_id,version) in manifest['deliveries'].items():
                key=f'{delivery_id}:{version}'
                delivery=tx.get(manifest['delivery_kind'],key)
                review=tx.get('reviews',manifest['reviews'][scope])
                if not delivery or not review: raise Conflict('frozen delivery/review is unavailable')
                sources=tx.get('group_delivery_sources',key) if manifest['delivery_kind']=='group_deliveries' else None
                children=[]
                child_reviews=[]
                if sources:
                    for ref in sources['body']['child_delivery_refs']:
                        child=tx.get('work_deliveries',ref)
                        child_review=tx.get('reviews',sources['body']['review_refs'][ref])
                        if not child or not child_review: raise Conflict('frozen child delivery/review is unavailable')
                        children.append(child['body'])
                        child_reviews.append(child_review['body'])
                groups[scope]={'delivery':delivery['body'],'review':review['body'],'work_deliveries':children,'work_reviews':child_reviews,'source':sources['body'] if sources else None}
        for group in groups.values():
            logical=group['work_deliveries'] if group['source'] else [group['delivery']]
            reviews=group['work_reviews'] if group['source'] else [group['review']]
            group['measurement_sources']=[]
            for child,review in zip(logical,reviews):
                projection_ref=child['result']['output'].get('reuse_projection_ref')
                if projection_ref:
                    from .reuse import ReuseService
                    resolved=ReuseService(self.repository,self).validate_projection(projection_ref)
                else: resolved={'projection':None,'source_delivery':child,'source_review':review}
                group['measurement_sources'].append({'logical_delivery':child,'logical_review':review}|resolved)
        return {'manifest':manifest,'groups':groups}
