"""Independent review assignments and authenticated, immutable decisions."""
from ..domain.evolution import CandidateRevision,ReviewRecord,TARGET_FIELDS
from ..domain.models import canonical_hash
from .agent_lifecycle import decode
from .gates import GateService


class CandidateReviewService:
    @staticmethod
    def takeover_in(tx,review_ref,*,token):
        from .agent_lifecycle import LifecycleService
        row=tx.get('candidate_review_assignments',review_ref)
        if not row:raise PermissionError('review assignment missing')
        candidate=tx.get('candidate_revisions',row['body']['candidate_ref'])['body']
        proposal=tx.get('change_proposals',candidate['proposal_id'])['body']['proposal']
        LifecycleService.assert_owner_in(tx,proposal['source_key']['lifecycle_id'],token)
        tx.put('candidate_review_owners',review_ref,{'thread_id':token.thread_id,
            'owner_epoch':token.owner_epoch,'lease_id':token.lease_id})

    @staticmethod
    def assign_in(tx,candidate_ref,*,review_ref,coder_identity,reviewer_identity,
                  coder_context,reviewer_context,gate_manifest_ref,token):
        tx.assert_fence(token)
        if not coder_context or not reviewer_context or coder_context==reviewer_context or coder_identity==reviewer_identity:
            raise PermissionError('independent reviewer and context required')
        candidate=decode(CandidateRevision,tx.get('candidate_revisions',candidate_ref)['body'])
        from .agent_lifecycle import LifecycleService
        proposal=tx.get('change_proposals',candidate.proposal_id)
        if not proposal:raise ValueError('review proposal missing')
        LifecycleService.assert_owner_in(tx,proposal['body']['proposal']['source_key']['lifecycle_id'],token)
        checked=GateService.evaluate_in(tx,gate_manifest_ref,candidate)
        if checked['status']!='pass':raise PermissionError('independent review requires passing trusted gate')
        body={'candidate_ref':candidate_ref,'candidate_hash':candidate.candidate_hash,
            'coder_identity':coder_identity,'reviewer_identity':reviewer_identity,
            'coder_context':coder_context,'reviewer_context':reviewer_context,
            'gate_manifest_ref':gate_manifest_ref,'gate_manifest_hash':checked['manifest_hash'],
            'thread_id':token.thread_id}
        tx.put('candidate_review_assignments',review_ref,body,immutable=True)
        return body

    @staticmethod
    def _verify_in(tx,review,candidate):
        assignment=tx.get('candidate_review_assignments',review.review_ref)
        if not assignment:raise PermissionError('independent review has no trusted assignment')
        for key in ('candidate_ref','candidate_hash','coder_identity','reviewer_identity','gate_manifest_ref','gate_manifest_hash'):
            if assignment['body'].get(key)!=getattr(review,key):raise PermissionError('review assignment changed')
        if review.candidate_hash!=candidate.candidate_hash or any(getattr(review,key)!=getattr(candidate,key) for key in TARGET_FIELDS):
            raise ValueError('review target/candidate binding changed')
        producer=tx.get('candidate_reviewers',review.reviewer_identity)
        if (not producer or producer['body'].get('revoked') is not False
                or producer['body'].get('expires_at',0)<=tx.now
                or producer['body'].get('version')!=review.producer_version
                or producer['body'].get('kind')!=review.reviewer_kind):
            raise PermissionError('independent reviewer registration expired or changed')
        if review.observed_at>tx.now or review.expires_at<=tx.now:raise ValueError('review expired or from future')
        checked=GateService.evaluate_in(tx,review.gate_manifest_ref,candidate)
        manifest=tx.get('gate_manifests',review.gate_manifest_ref)['body']
        if checked['status']!='pass' or checked['manifest_hash']!=review.gate_manifest_hash or manifest['policy_hash']!=review.policy_hash:
            raise ValueError('review trusted gate/policy changed')

    @classmethod
    def record_in(cls,tx,review,*,authenticated_identity,token):
        tx.assert_fence(token)
        if not isinstance(review,ReviewRecord) or authenticated_identity!=review.reviewer_identity:
            raise PermissionError('authenticated independent reviewer required')
        candidate=decode(CandidateRevision,tx.get('candidate_revisions',review.candidate_ref)['body'])
        assignment=tx.get('candidate_review_assignments',review.review_ref)
        owner=tx.get('candidate_review_owners',review.review_ref)
        actual=owner['body'] if owner else assignment['body'] if assignment else {}
        if (not assignment or actual['thread_id']!=token.thread_id
                or (owner and (actual['owner_epoch'],actual['lease_id'])!=(token.owner_epoch,token.lease_id))):
            raise PermissionError('review execution thread differs from assignment')
        cls._verify_in(tx,review,candidate)
        tx.put('candidate_reviews',review.review_ref,review.model_dump(mode='json'),immutable=True)
        tx.put('candidate_review_attestations',review.review_ref,{'review_hash':canonical_hash(review),
            'authenticated_identity':authenticated_identity,'thread_id':token.thread_id,
            'owner_epoch':token.owner_epoch,'lease_id':token.lease_id},immutable=True)

    @classmethod
    def approved_in(cls,tx,review_ref,candidate):
        row=tx.get('candidate_reviews',review_ref);proof=tx.get('candidate_review_attestations',review_ref)
        if not row or not proof:raise PermissionError('review has no authenticated receipt')
        review=decode(ReviewRecord,row['body']);cls._verify_in(tx,review,candidate)
        if proof['body']['review_hash']!=canonical_hash(review) or proof['body']['authenticated_identity']!=review.reviewer_identity:
            raise PermissionError('review receipt identity changed')
        if review.result!='approved' or any(f.severity=='blocking' for f in review.findings):
            raise PermissionError('independent review did not approve candidate')
        return review
