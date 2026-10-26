"""One transaction checks publication proofs, worker readiness and activation CAS."""
from ..domain.evolution import CandidateRevision,ChangeProposal,PromotionRecord,TARGET_FIELDS
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from .agent_lifecycle import LifecycleService,decode
from .candidate_reviews import CandidateReviewService
from .evolution import EvolutionService
from .gates import GateService
from .revisions import RevisionService


class PromotionService:
    def __init__(self,repository):self.repository=repository

    def promote(self,request,*,token):
        if not isinstance(request,PromotionRecord):raise ValueError('typed immutable promotion request required')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            old=tx.get('promotion_records',request.request_id)
            if old:
                if old['body']['request']!=request.model_dump(mode='json'):raise Conflict('promotion request input conflict')
                return old['body']['result']
            if request.created_at>tx.now or request.expires_at<=tx.now:raise PermissionError('publication window expired')
            candidate=decode(CandidateRevision,tx.get('candidate_revisions',request.candidate_ref)['body'])
            inputs=tx.get('change_proposals',candidate.proposal_id)
            if not inputs:raise ValueError('frozen proposal missing')
            proposal=decode(ChangeProposal,inputs['body']['proposal'])
            EvolutionService.authorize_in(tx,proposal,token)
            if candidate.candidate_hash!=request.candidate_hash or any(getattr(candidate,k)!=getattr(request,k) for k in TARGET_FIELDS):
                raise ValueError('publication candidate/target changed')
            EvolutionService.verify_targets_in(tx,candidate)
            gate=GateService.evaluate_in(tx,request.gate_manifest_ref,candidate)
            if gate['status']!='pass' or gate['manifest_hash']!=request.gate_manifest_hash:
                raise PermissionError('publication requires current passing gate')
            review=CandidateReviewService.approved_in(tx,request.review_ref,candidate)
            if canonical_hash(review)!=request.review_hash or review.gate_manifest_ref!=request.gate_manifest_ref:
                raise ValueError('publication review identity changed')
            row=tx.get('publication_authorizations',request.authorization_ref)
            if not row or row['revision']!=request.authorization_revision or canonical_hash(row['body'])!=request.authorization_hash:
                raise PermissionError('publication authorization changed')
            auth=row['body']
            if (auth.get('revoked') is not False or auth.get('expires_at',0)<=tx.now
                    or auth.get('candidate_hash')!=candidate.candidate_hash
                    or auth.get('predecessor_run_id')!=proposal.source_key.run_ref
                    or auth.get('gate_manifest_hash')!=request.gate_manifest_hash
                    or auth.get('review_hash')!=request.review_hash or not auth.get('principal')
                    or auth.get('disposition_policy') not in {'drain','cancel'}):
                raise PermissionError('publication scope authorization invalid')
            predecessor=tx.get('runs',proposal.source_key.run_ref)
            if not predecessor or predecessor['revision']!=request.expected_predecessor_revision:
                raise Conflict('predecessor revision conflict')
            if predecessor['body']['graph_ref']!=proposal.base_graph_ref or predecessor['body'].get('successor_run_id'):
                raise Conflict('predecessor version changed or already superseded')
            bundle=tx.get('worker_bundles',request.worker_bundle_ref)
            if not bundle or canonical_hash(bundle['body'])!=request.worker_bundle_hash:
                raise ValueError('immutable worker bundle changed')
            from .worker_bundles import WorkerBundleService
            from pathlib import Path
            if bundle['body'].get('schema_version')!=2:
                raise ValueError('candidate publication requires a built immutable worker bundle')
            WorkerBundleService(self.repository,root=Path(bundle['body']['path']).parent)._verify_body(request.worker_bundle_ref,bundle['body'])
            for field in ('candidate_hash','graph_ref','tree_hash','lock_hash'):
                expected=candidate.candidate_hash if field=='candidate_hash' else getattr(candidate,field)
                if bundle['body'].get(field)!=expected:raise ValueError('worker bundle candidate binding differs')
            from ..domain.agent_lifecycle import TransitionGuards
            life=EvolutionService.gate_observed_in(tx,candidate,request.gate_manifest_ref,token)
            if life.phase not in {'promotion_pending','verified_waiting_worker'}:
                raise Conflict('candidate lifecycle is not awaiting promotion')
            if candidate.risk=='R0':
                EvolutionService._event_in(tx,proposal,'nonruntime_candidate_accepted',request.gate_manifest_ref,token,
                    TransitionGuards(schema_version=1,promotion_proof_valid=True,authority_valid=True),resume_phase='diagnosing')
                result={'status':'nonruntime_candidate_accepted','candidate_ref':candidate.candidate_ref}
            else:
                if candidate.graph_ref==proposal.base_graph_ref:raise ValueError('executable candidate requires a new immutable graph version')
                ready=any(row['body'].get('bundle_ref')==request.worker_bundle_ref
                    and row['body'].get('bundle_hash')==request.worker_bundle_hash
                    and row['body'].get('graph_ref')==candidate.graph_ref
                    and row['body'].get('status')=='ready' and row['body'].get('expires_at',0)>tx.now
                    and WorkerBundleService.ready_in(tx,row['body'])
                    for row in tx.scan('worker_registrations'))
                if not ready:
                    if life.phase=='promotion_pending':
                        EvolutionService._event_in(tx,proposal,'worker_missing',request.worker_bundle_ref,token,
                            TransitionGuards(schema_version=1,proof_valid=True))
                    tx.put('promotion_waits',request.request_id,{'request':request.model_dump(mode='json'),
                        'reason':'no_ready_worker','observed_at':tx.now})
                    return {'status':'verified_waiting_worker','candidate_ref':candidate.candidate_ref}
                if life.phase=='verified_waiting_worker':
                    EvolutionService._event_in(tx,proposal,'ready',request.worker_bundle_ref,token,
                        TransitionGuards(schema_version=1,proof_valid=True,worker_ready=True))
                deployment=None
                if tx.get('evaluation_deployments',proposal.source_key.run_ref):
                    bound=tx.get('candidate_deployment_bindings',candidate.candidate_hash)
                    deployment=bound['body'] if bound else {}
                    expected={'candidate_hash':candidate.candidate_hash,'bundle_ref':request.worker_bundle_ref,
                        'bundle_hash':request.worker_bundle_hash,'plan_ref':candidate.target_plan_ref,
                        'contract_ref':candidate.target_contract_ref,'auth_ref':candidate.target_auth_ref,'graph_ref':candidate.graph_ref}
                    seconds=deployment.get('execution_seconds')
                    if (any(deployment.get(k)!=v for k,v in expected.items()) or not deployment.get('runtime_hash')
                            or type(seconds) not in (int,float) or not 0<seconds<=86400
                            or not WorkerBundleService.deployment_ready_in(tx,deployment)):
                        raise PermissionError('candidate evaluation deployment is missing or differs')
                args={'predecessor_run_id':proposal.source_key.run_ref,
                    'expected_revision':request.expected_predecessor_revision,'candidate_plan_hash':candidate.target_plan_ref,
                    'graph_ref':candidate.graph_ref,'disposition_policy':auth['disposition_policy']}
                successor=RevisionService._activate_in(tx,request_id='promotion:'+request.request_id,args=args,
                    digest=canonical_hash(request),target_contract_ref=candidate.target_contract_ref,target_auth_ref=candidate.target_auth_ref)
                if deployment is not None:
                    expiry=tx.get('execution_authorizations',candidate.target_auth_ref)['body']['expires_at']
                    tx.put('evaluation_deployments',successor.run_id,{'runtime_hash':deployment['runtime_hash'],
                        'graph_ref':candidate.graph_ref,'deadline':min(expiry,tx.now+deployment['execution_seconds'])},immutable=True)
                tx.put('run_bundle_bindings',successor.run_id,{'bundle_ref':request.worker_bundle_ref,
                    'bundle_hash':request.worker_bundle_hash,'candidate_ref':candidate.candidate_ref,
                    'promotion_ref':request.request_id},immutable=True)
                tx.put('lifecycle_promotion_bindings',life.key.lifecycle_id,{'successor_run_ref':successor.run_id,
                    'candidate_ref':candidate.candidate_ref,'promotion_ref':request.request_id,
                    'closure':'original_version_drain_required'},immutable=True)
                result={'status':'activated','candidate_ref':candidate.candidate_ref,'run_ref':successor.run_id}
            tx.put('promotion_records',request.request_id,{'request':request.model_dump(mode='json'),
                'result':result,'principal':auth['principal'],'activated_at':tx.now},immutable=True)
            tx.put('outbox','promotion:'+request.request_id,{'kind':'candidate_promoted',**result},immutable=True)
            return result
