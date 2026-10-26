"""Gate provenance and coverage checks under the same database transaction.

This service consumes observations from a registered, leased trusted runner.
It does not turn local pytest output or a model response into a trusted proof.
"""
from ..domain.evolution import CandidateRevision,GateManifest
from ..domain.models import canonical_hash
from .agent_lifecycle import decode
from .gate_policy import gate_plan


class GateService:
    @staticmethod
    def _producer_in(tx,manifest):
        row=tx.get('trusted_gate_producers',manifest.producer_identity)
        if not row:raise PermissionError('gate producer is not registered')
        producer=row['body']
        expected={'version':manifest.producer_version,'runner_ref':manifest.runner_ref,
            'runner_version':manifest.runner_version,'bundle_hash':manifest.runner_bundle_hash}
        if (producer.get('revoked') is not False or producer.get('expires_at',0)<=tx.now
                or any(producer.get(key)!=value for key,value in expected.items())):
            raise PermissionError('gate producer identity/bundle expired or changed')
        return row

    @staticmethod
    def _plan_in(tx,job,candidate):
        profile=tx.get('gate_profiles',job['profile_ref'])
        if not profile:raise ValueError('trusted gate profile missing')
        return gate_plan(candidate,profile['body'])

    @classmethod
    def record_result_in(cls,tx,job_id,manifest,*,producer_identity,token):
        tx.assert_fence(token)
        if not isinstance(manifest,GateManifest):raise ValueError('typed gate manifest required')
        job=tx.get('gate_executions',job_id)
        if not job or job['body'].get('thread_id')!=token.thread_id:
            raise PermissionError('gate execution does not belong to this lease')
        body=job['body']
        if producer_identity!=manifest.producer_identity or body.get('producer_identity')!=producer_identity:
            raise PermissionError('gate producer differs from authenticated execution')
        cls._producer_in(tx,manifest)
        row=tx.get('candidate_revisions',body['candidate_ref'])
        if not row:raise ValueError('frozen candidate missing')
        candidate=decode(CandidateRevision,row['body'])
        plan=cls._plan_in(tx,body,candidate)
        cls._bindings(tx,manifest,candidate,plan,body)
        existing=tx.get('gate_manifests',manifest.manifest_ref)
        if existing:
            if existing['body']!=manifest.model_dump(mode='json'):raise ValueError('gate manifest identity conflict')
            return manifest.manifest_ref
        if body.get('status')!='running':raise PermissionError('gate execution is no longer active')
        tx.put('gate_manifests',manifest.manifest_ref,manifest.model_dump(mode='json'),immutable=True)
        tx.put('gate_attestations',manifest.manifest_ref,{'job_id':job_id,'manifest_hash':manifest.manifest_hash,
            'plan_hash':plan['plan_hash'],'producer_identity':producer_identity,'thread_id':token.thread_id,
            'owner_epoch':token.owner_epoch,'lease_id':token.lease_id},immutable=True)
        tx.put('gate_executions',job_id,body|{'status':'completed','manifest_ref':manifest.manifest_ref},expected_revision=job['revision'])
        return manifest.manifest_ref

    @staticmethod
    def _bindings(tx,manifest,candidate,plan,execution):
        if manifest.candidate_ref!=candidate.candidate_ref or not manifest.matches_candidate(candidate):
            raise ValueError('gate candidate/target identity differs')
        if manifest.observed_at>tx.now or manifest.expires_at<=tx.now:raise ValueError('gate observation is expired or in the future')
        for key in ('trusted_suite_hash','policy_hash'):
            if getattr(manifest,key)!=plan[key]:raise ValueError('trusted suite/policy digest differs')
        for key in ('environment_ref','environment_hash'):
            if getattr(manifest,key)!=execution.get(key):raise ValueError('gate environment binding differs')
        if set(manifest.required_gates)!=set(plan['required_gates']) or set(manifest.expected_case_ids)!=set(plan['expected_case_ids']):
            raise ValueError('gate expected set was changed')

    @classmethod
    def evaluate_in(cls,tx,manifest_ref,candidate):
        stored=tx.get('gate_manifests',manifest_ref);attested=tx.get('gate_attestations',manifest_ref)
        if not stored or not attested:raise ValueError('gate has no trusted stored attestation')
        manifest=decode(GateManifest,stored['body']);attestation=attested['body']
        if manifest.manifest_hash!=attestation['manifest_hash']:raise ValueError('stored gate manifest digest differs')
        cls._producer_in(tx,manifest)
        execution=tx.get('gate_executions',attestation['job_id'])
        if not execution or execution['body'].get('manifest_ref')!=manifest_ref:raise ValueError('gate execution receipt missing')
        plan=cls._plan_in(tx,execution['body'],candidate)
        cls._bindings(tx,manifest,candidate,plan,execution['body'])
        if plan['plan_hash']!=attestation['plan_hash']:raise ValueError('gate plan changed after observation')
        expected=set(plan['expected_case_ids']);actual=set(manifest.actual_case_ids)
        by_case={row.case_id:row for row in manifest.case_results}
        by_gate={row.gate_id:row for row in manifest.results}
        binding={'manifest_ref':manifest_ref,'manifest_hash':manifest.manifest_hash,'candidate_hash':candidate.candidate_hash}
        if actual!=expected or set(by_case)!=expected or set(by_gate)!=set(plan['required_gates']):
            return {'status':'blocked','reason':'required_coverage_missing','manifest_ref':manifest_ref}
        for gate,ids in plan['case_assignments'].items():
            result=by_gate[gate]
            if set(result.case_ids)!=set(ids):return binding|{'status':'blocked','reason':'gate_case_assignment_changed'}
            if result.status!='pass':return binding|{'status':result.status if result.status in {'fail','blocked'} else 'blocked','reason':'gate_not_passed','gate':gate}
        if any(row.status!='pass' for row in by_case.values()):
            return binding|{'status':'fail' if any(row.status=='fail' for row in by_case.values()) else 'blocked',
                'reason':'required_test_did_not_pass'}
        return {'status':'pass','manifest_ref':manifest_ref,'manifest_hash':manifest.manifest_hash,
            'candidate_hash':candidate.candidate_hash,'plan_hash':plan['plan_hash']}
