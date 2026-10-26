"""Rollback creates a fresh run; original checkpoints and effects never rewind."""
from pathlib import Path
from ..domain.evolution import RollbackRecord,CandidateRevision,TARGET_FIELDS
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from .agent_lifecycle import decode
from .candidate_reviews import CandidateReviewService
from .evolution import EvolutionService
from .gates import GateService
from .revisions import RevisionService
from .worker_bundles import WorkerBundleService


class RollbackService:
    def __init__(self,repository):self.repository=repository

    def rollback(self,request,*,token):
        if not isinstance(request,RollbackRecord):raise ValueError('typed frozen rollback request required')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            pending=tx.get('rollback_requests',request.request_id)
            if pending and pending['body']['request']!=request.model_dump(mode='json'):
                raise Conflict('rollback request input changed')
            old=tx.get('rollback_records',request.request_id)
            if old:
                if old['body']['request']!=request.model_dump(mode='json'):raise Conflict('rollback request input changed')
                return old['body']['result']
            if not request.created_at<=tx.now<request.expires_at:raise PermissionError('rollback publication window expired')
            row=tx.get('runs',request.failed_run_ref)
            if not row or row['revision']!=request.expected_predecessor_revision or row['body'].get('successor_run_id'):
                raise Conflict('rollback predecessor revision changed')
            failure=tx.get('release_observations',request.failed_run_ref)
            if not failure or failure['body'].get('status')!='fail':raise PermissionError('rollback requires actual failed release observation')
            observed=failure['body'];history=tx.get('release_observation_history',observed.get('observation_ref',''))
            if (not history or history['body']!={key:value for key,value in observed.items() if key!='observation_ref'}
                    or observed.get('producer')!='release-observer.v1' or observed.get('graph_ref')!=row['body']['graph_ref']):
                raise PermissionError('rollback release failure provenance differs')
            record=tx.get('candidate_revisions',request.candidate_ref)
            if not record:raise ValueError('rollback target candidate is not registered')
            candidate=decode(CandidateRevision,record['body'])
            if candidate.candidate_hash!=request.candidate_hash or any(getattr(candidate,k)!=getattr(request,k) for k in TARGET_FIELDS):
                raise PermissionError('rollback target binding differs')
            if candidate.graph_ref==row['body']['graph_ref']:raise ValueError('rollback requires a different immutable graph version')
            EvolutionService.verify_targets_in(tx,candidate)
            gate=GateService.evaluate_in(tx,request.gate_manifest_ref,candidate)
            review=CandidateReviewService.approved_in(tx,request.review_ref,candidate)
            if (gate['status']!='pass' or gate['manifest_hash']!=request.gate_manifest_hash
                    or canonical_hash(review)!=request.review_hash or review.gate_manifest_ref!=request.gate_manifest_ref):
                raise PermissionError('rollback requires current passing gate and independent review')
            authorization=tx.get('rollback_authorizations',request.authorization_ref)
            if (not authorization or authorization['revision']!=request.authorization_revision
                    or canonical_hash(authorization['body'])!=request.authorization_hash):
                raise PermissionError('rollback authorization changed')
            auth=authorization['body'];expected={
                'failed_run_ref':request.failed_run_ref,'failed_run_revision':request.expected_predecessor_revision,
                'candidate_hash':request.candidate_hash,'target_plan_ref':request.target_plan_ref,
                'gate_manifest_hash':request.gate_manifest_hash,'review_hash':request.review_hash,
                'worker_bundle_hash':request.worker_bundle_hash,'reason':request.reason}
            if (auth.get('revoked') is not False or auth.get('expires_at',0)<=tx.now or not auth.get('principal')
                    or any(auth.get(key)!=value for key,value in expected.items())):
                raise PermissionError('exact rollback scope authorization required')
            bundle=tx.get('worker_bundles',request.worker_bundle_ref)
            if (not bundle or canonical_hash(bundle['body'])!=request.worker_bundle_hash
                    or any(bundle['body'].get(key)!=getattr(candidate,key) for key in ('candidate_hash','graph_ref','tree_hash','lock_hash'))):
                raise PermissionError('rollback immutable worker bundle differs')
            WorkerBundleService(self.repository,root=Path(bundle['body']['path']).parent)._verify_body(request.worker_bundle_ref,bundle['body'])
            ready=[worker for worker in tx.scan('worker_registrations') if worker['body'].get('bundle_ref')==request.worker_bundle_ref
                and WorkerBundleService.ready_in(tx,worker['body'])]
            if not ready:
                result={'status':'verified_waiting_worker','candidate_ref':candidate.candidate_ref,'failed_run_ref':request.failed_run_ref}
                tx.put('rollback_requests',request.request_id,{'request':request.model_dump(mode='json'),'result':result,
                    'principal':auth['principal'],'observed_at':tx.now})
                return result
            deployment=None
            if tx.get('evaluation_deployments',request.failed_run_ref):
                bound=tx.get('candidate_deployment_bindings',candidate.candidate_hash);deployment=bound['body'] if bound else {}
                required={'candidate_hash':candidate.candidate_hash,'bundle_ref':request.worker_bundle_ref,
                    'bundle_hash':request.worker_bundle_hash,'plan_ref':candidate.target_plan_ref,
                    'contract_ref':candidate.target_contract_ref,'auth_ref':candidate.target_auth_ref,'graph_ref':candidate.graph_ref}
                if (any(deployment.get(k)!=v for k,v in required.items()) or not deployment.get('runtime_hash')
                        or not 0<deployment.get('execution_seconds',0)<=86400
                        or not WorkerBundleService.deployment_ready_in(tx,deployment)):
                    raise PermissionError('rollback deployment is not prepared')
            args={'predecessor_run_id':request.failed_run_ref,'expected_revision':request.expected_predecessor_revision,
                'candidate_plan_hash':candidate.target_plan_ref,'graph_ref':candidate.graph_ref,'disposition_policy':'cancel'}
            successor=RevisionService._activate_in(tx,request_id='rollback:'+request.request_id,args=args,
                digest=canonical_hash(request),target_contract_ref=candidate.target_contract_ref,target_auth_ref=candidate.target_auth_ref)
            if deployment:
                expiry=tx.get('execution_authorizations',candidate.target_auth_ref)['body']['expires_at']
                tx.put('evaluation_deployments',successor.run_id,{'runtime_hash':deployment['runtime_hash'],
                    'graph_ref':candidate.graph_ref,'deadline':min(expiry,tx.now+deployment['execution_seconds'])},immutable=True)
            tx.put('run_bundle_bindings',successor.run_id,{'bundle_ref':request.worker_bundle_ref,
                'bundle_hash':request.worker_bundle_hash,'candidate_ref':candidate.candidate_ref,
                'rollback_ref':request.request_id,'promotion_ref':'rollback:'+request.request_id},immutable=True)
            result={'status':'activated','run_ref':successor.run_id,'failed_run_ref':request.failed_run_ref,
                'candidate_ref':candidate.candidate_ref,'cleanup':'original_run_protocol'}
            tx.put('rollback_records',request.request_id,{'request':request.model_dump(mode='json'),'result':result,
                'principal':auth['principal'],'failure_observation_ref':observed['observation_ref'],'activated_at':tx.now},immutable=True)
            tx.put('rollback_requests',request.request_id,{'request':request.model_dump(mode='json'),'result':result,
                'principal':auth['principal'],'observed_at':tx.now})
            tx.put('outbox','rollback:'+request.request_id,{'kind':'candidate_rollback',**result},immutable=True)
            return result
