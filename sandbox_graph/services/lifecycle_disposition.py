"""所有效果账本 writer 的事务钩子；旧证明保留，当前处置版本前进。"""
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict


class DispositionVersions:
    EFFECT_KINDS = frozenset({'operations', 'agent_provider_requests', 'resources', 'observations',
        'resource_leases', 'work_execution_leases', 'repair_isolations', 'candidate_isolations',
        'receipts', 'agent_lifecycles', 'jobs', 'repair_isolation_resolutions',
        'candidate_disposition_proofs', 'work_safety_facts'})

    @staticmethod
    def current_in(tx, run_ref):
        row = tx.get('disposition_revisions', run_ref)
        return row['body']['revision'] if row else 0

    @staticmethod
    def runs_in(tx, kind, key, body):
        if body is None:
            return set()
        runs, scopes = set(), set()
        identity = body.get('identity', {})
        life_key = body.get('key', {}) if kind == 'agent_lifecycles' else {}
        envelope = body.get('envelope', {})
        for data in (body, identity, life_key):
            runs.update(v for v in (data.get('run_ref'), data.get('run_id')) if v)
            scopes.update(v for v in (data.get('scope'), data.get('scope_ref'), data.get('work_ref')) if isinstance(v,str))
        if kind in {'work_execution_leases','work_safety_facts'}: scopes.add(key)
        scope = envelope.get('scope', {}).get('ref')
        if scope: scopes.add(scope)
        refs = {body.get('operation_id'), *body.get('operation_refs', ())} - {None}
        for ref in refs:
            operation = tx.get('operations', ref)
            if operation:
                value = operation['body']
                if value.get('run_id'): runs.add(value['run_id'])
                if value.get('scope'): scopes.add(value['scope'])
        ref = envelope.get('lifecycle_id') or body.get('lifecycle_id') or identity.get('lifecycle_id') or body.get('payload',{}).get('lifecycle_id')
        life = tx.get('agent_lifecycles', ref) if ref else None
        if life:
            runs.add(life['body']['key']['run_ref'])
        for scope in scopes:
            work = tx.get('work_instances', scope)
            if work: runs.add(work['body']['run_id'])
            if tx.get('runs', scope): runs.add(scope)
        if not runs and scopes:
            # Legacy shared labels affect every potentially owning run, without
            # conferring permission to delete an ambiguously owned resource.
            runs.update(r['body']['run_id'] for r in tx.scan('groups') if r['body']['group_id'] in scopes)
        return runs

    @classmethod
    def changed_in(cls, tx, kind, key, before, after):
        if kind not in cls.EFFECT_KINDS:
            return
        if kind == 'jobs':
            from .lifecycle_stop_jobs import StopJobs
            if StopJobs.valid_in(tx,after or before):
                return
            from ..runtime.tasks import business_job_kind
            if business_job_kind((after or before).get('job_kind','')) in {
                    'analysis','report','graph:analysis','graph:report'}:
                return
        runs = cls.runs_in(tx,kind,key,before) | cls.runs_in(tx,kind,key,after)
        for run_ref in sorted(runs):
            revision = cls.current_in(tx,run_ref) + 1
            tx.put('disposition_revisions',run_ref,{'revision':revision,
                'change':{'kind':kind,'key':key,'deleted':after is None}})
            targets = tx.get('run_stop_targets',run_ref)
            for target in targets['body']['refs'] if targets else ():
                status = tx.get('lifecycle_stop_status',target)
                if not status or status['body']['status'] not in {'closed','waiting_dependency'}:
                    continue
                tx.put('lifecycle_stop_status',target,status['body'] | {
                    'status':'waiting_dependency','reason':'disposition revision changed',
                    'disposition_revision':revision,'recovery_entry':'original stop target recheck'})
                tx.put('outbox',f'stop-recheck:{target}:{revision}',{
                    'kind':'lifecycle_stop_requested','request_ref':target},immutable=True)
            current = tx.get('current_archives',run_ref)
            if current:
                tx.put('current_archives',run_ref,current['body'] | {
                    'status':'stale','disposition_revision':revision})
        if kind == 'agent_lifecycles' and before is None and after is not None:
            from .lifecycle_stop import LifecycleStopService
            LifecycleStopService.attach_lifecycle_in(tx, key, after)

    @classmethod
    def verify_stop_in(cls, tx, proof):
        stored=tx.get('lifecycle_stop_proofs',proof.get('proof_ref',''))
        if not stored or stored['body']!=proof or proof.get('producer')!='lifecycle_stop.v1':
            raise Conflict('stop proof stored producer receipt differs')
        request = tx.get('lifecycle_stop_requests',proof['request_ref'])
        if not request or proof.get('command_hash') != canonical_hash(request['body']):
            raise Conflict('stop proof original command differs')
        run_ref = request['body']['run_ref']
        if proof.get('disposition_revision') != cls.current_in(tx,run_ref):
            raise Conflict('stop proof disposition revision changed')
        from .lifecycle_inventory import LifecycleInventory
        snapshot = LifecycleInventory.collect_in(tx,run_ref=run_ref,
            scope_kind=request['body']['scope_kind'],scope_ref=request['body']['scope_ref'])
        if proof.get('inventory_digest') != snapshot.digest:
            raise Conflict('stop proof current inventory differs')
        return proof
