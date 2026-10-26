"""Immutable T6 observations. Only the work supervisor may authorize recovery."""
from ..catalog.registry import NODE_GROUPS
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from ..runtime.tasks import business_job_kind


class DeliveryReworkService:
    @staticmethod
    def provenance_in(tx, delivery_ref, delivery, *, run_ref, group_ref):
        owner = tx.get('work_delivery_owners', delivery_ref)
        if not owner or (owner['body']['run_id'], owner['body']['group_id']) != (run_ref, group_ref):
            raise Conflict('delivery owner does not match run/group')
        work_ref = owner['body']['work_id']
        work = tx.get('work_instances', work_ref)
        if (not work or work_ref not in delivery['work_refs']
                or (work['body']['run_id'], work['body']['group_id'], work['body']['plan_ref'])
                != (run_ref, group_ref, delivery['plan_ref'])):
            raise Conflict('delivery work identity mismatch')
        source = tx.get('work_delivery_sources', delivery_ref)
        if not source:
            raise Conflict('delivery execution provenance missing; text findings cannot select a node')
        source = source['body']
        job = tx.get('jobs', source['job_id'])
        if not job:
            raise Conflict('delivery source job missing')
        job = job['body']
        refs = job.get('result', {}).get('graph_result', {}).get('result_refs', {})
        activation = tx.get('activations', job['activation_id'])
        metadata = tx.get('activation_jobs', job['activation_id'])
        if (source['delivery_hash'] != canonical_hash(delivery)
                or business_job_kind(job['job_kind']) != 'work'
                or (job['run_id'], job['scope_ref'], job['activation_id'])
                != (run_ref, work_ref, source['activation_id'])
                or job['payload'].get('plan_ref') != delivery['plan_ref']
                or job['status'] not in {'complete', 'failed', 'blocked'}
                or not tx.get('event_ids', 'completion:' + job['job_id'])
                or not metadata or metadata['body'].get('job_id') != job['job_id']
                or not activation or activation['body'].get('status') != 'complete'
                or activation['body']['result'].get('result_refs') != refs
                or source['result_refs'] != refs):
            raise Conflict('delivery execution provenance mismatch')
        reuse = {}
        recovery_ref = activation['body']['record'].get('recovery_plan_ref')
        if recovery_ref:
            from .recovery import RecoveryService
            _, recovery = RecoveryService._load_in(tx, recovery_ref)
            assigned = tx.get('recovery_plan_activations', recovery_ref)
            started = tx.get('recovery_starts', job['activation_id'])
            if (not assigned or assigned['body'] != {'activation_id':job['activation_id'],'thread_id':job['thread_id']}
                    or not started or started['body']['plan_ref'] != recovery_ref
                    or (recovery['run_ref'],recovery['scope'],recovery['plan_ref'])
                    != (run_ref,work_ref,delivery['plan_ref'])):
                raise Conflict('delivery recovery provenance mismatch')
            reuse = recovery['result_refs']
        for node, ref in refs.items():
            receipt = tx.get('receipts', ref)
            expected = {'run_ref':run_ref, 'scope':work_ref, 'node_id':node,
                'plan_ref':delivery['plan_ref'], 'graph_ref':job['payload']['graph_ref']}
            if reuse.get(node) == ref:
                if canonical_hash(receipt) != recovery['receipt_hashes'].get(ref):
                    raise Conflict('delivery reused receipt changed')
            else:
                expected.update(activation_id=job['activation_id'], thread_id=job['thread_id'])
            if not receipt or any(receipt['body']['identity'].get(k) != v for k, v in expected.items()):
                raise Conflict('delivery source receipt identity mismatch')
        return work, job, refs

    @classmethod
    def source_in(cls, tx, delivery_ref, delivery, *, run_ref, group_ref):
        work, job, refs = cls.provenance_in(tx, delivery_ref, delivery,
            run_ref=run_ref, group_ref=group_ref)
        work_ref = work['body']['work_id']
        # Independent failures cannot be guessed from prose. Multiple candidates
        # need an explicit causal mapping before any automatic repair.
        failures = [(node, refs[node]) for node in NODE_GROUPS['work']
                    if node in refs and tx.get('agent_supervision', refs[node])]
        if len(failures) != 1:
            raise Conflict('delivery has no unique failed source node')
        node, receipt_ref = failures[0]
        receipt = tx.get('receipts', receipt_ref)
        expected = {'run_ref':run_ref, 'scope':work_ref, 'node_id':node,
            'activation_id':job['activation_id'], 'thread_id':job['thread_id'],
            'plan_ref':delivery['plan_ref'], 'graph_ref':job['payload']['graph_ref']}
        if not receipt or any(receipt['body']['identity'].get(k) != v for k, v in expected.items()):
            raise Conflict('delivery source receipt identity mismatch')
        return {'work_ref':work_ref, 'work_revision':work['revision'],
            'job_id':job['job_id'], 'node_id':node, 'receipt_ref':receipt_ref,
            'receipt_hash':canonical_hash(receipt['body']), 'visit_id':receipt['body']['visit_id'],
            'activation_id':job['activation_id'], 'thread_id':job['thread_id']}

    @classmethod
    def resolve_in(cls, tx, context, delivery_ref, delivery, review):
        """A successful current work attempt plus artifact review closes a gap.

        An accepted review of the old failed attempt cannot erase its failure.
        Separate immutable resolution records preserve both sides of the decision.
        """
        try:
            work, job, refs = cls.provenance_in(tx, delivery_ref, delivery,
                run_ref=context.run_ref, group_ref=context.scope)
        except Conflict:
            return
        if (work['body'].get('result_ref') != 'job:' + job['job_id']
                or job['result'].get('execution_status') != 'succeeded'
                or delivery['result']['data_status'] != 'valid'
                or not {'N07','N09','N10'} <= refs.keys()
                or any(tx.get('agent_supervision', ref) for ref in refs.values())):
            return
        for row in tx.scan('delivery_rework_gaps'):
            gap = row['body']
            owner = tx.get('work_delivery_owners', gap['delivery_ref'])
            if ((gap['run_ref'],gap['group_ref'],gap['plan_ref'])
                    != (context.run_ref,context.scope,context.plan_ref)
                    or not owner or owner['body']['work_id'] != work['body']['work_id']):
                continue
            resolution = {'gap_ref':row['key'], 'delivery_ref':delivery_ref,
                'delivery_hash':canonical_hash(delivery), 'review_ref':review['review_id'],
                'job_id':job['job_id']}
            resolution_ref = canonical_hash(resolution)
            tx.put('delivery_rework_resolutions', resolution_ref, resolution, immutable=True)
            tx.put('delivery_rework_status', row['key'], {'status':'resolved',
                'resolution_ref':resolution_ref, 'resolution_review_ref':review['review_id']})

    @classmethod
    def record_in(cls, tx, context, deliveries, reviews, *, token):
        tx.assert_fence(token)
        recorded = []
        for delivery, review in zip(deliveries, reviews):
            ref = f"{delivery['delivery_id']}:{delivery['version']}"
            frozen = tx.get('reviews', review['review_id'])
            if not frozen or frozen['body'] != review or review['delivery_hash'] != canonical_hash(delivery):
                raise Conflict('T6 review is not bound to immutable delivery')
            if review['decision'] != 'rejected':
                cls.resolve_in(tx, context, ref, delivery, review)
                continue
            identity = {'schema_version':1, 'run_ref':context.run_ref,
                'group_ref':context.scope, 'plan_ref':context.plan_ref,
                'delivery_ref':ref, 'delivery_hash':review['delivery_hash'],
                'review_ref':review['review_id'], 'review_hash':canonical_hash(review)}
            gap_ref = 'delivery-gap:' + canonical_hash(identity)
            if tx.get('delivery_rework_gaps', gap_ref):
                recorded.append(gap_ref)
                continue
            try:
                source = cls.source_in(tx, ref, delivery, run_ref=context.run_ref, group_ref=context.scope)
                reason = None
            except Conflict as exc:
                source, reason = None, str(exc)
            tx.put('delivery_rework_gaps', gap_ref, identity | {'source':source,
                'reason':reason}, immutable=True)
            tx.put('delivery_rework_status', gap_ref, {'status':'needs_input' if reason else 'pending',
                'reason':reason})
            if source:
                tx.put('outbox', gap_ref, {'kind':'delivery_rework_requested', 'gap_ref':gap_ref}, immutable=True)
            recorded.append(gap_ref)
        return recorded
