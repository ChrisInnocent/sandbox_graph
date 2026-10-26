"""Transaction-bound work exclusion and ledger-derived resource disposition.

These are trusted service APIs. Runtime producers publish facts only after
recording actual node receipts; a model or command cannot supply guard booleans.
No external I/O runs under the repository transaction.
"""
from __future__ import annotations
from ..runtime.tasks import business_job_kind

import json

from ..domain.agent_lifecycle import ResourceDispositionReceipt
from ..domain.models import canonical_hash
from ..runtime.observation import ObservationService
from ..persistence.repository import Conflict, LeaseToken, StaleOwner, positive_duration


class LifecycleSafetyService:
    PRODUCER = 'lifecycle_safety'
    VERSION = '1'

    @staticmethod
    def scope_in(tx,scope,token):
        from .scope_safety import ScopeSafety
        return ScopeSafety.resolve_in(tx,scope,token)

    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _assert_thread_in(tx, token):
        if (not isinstance(token, LeaseToken) or type(token.owner_epoch) is not int
                or token.owner_epoch <= 0 or not isinstance(token.thread_id, str)
                or not token.thread_id or not isinstance(token.lease_id, str) or not token.lease_id):
            raise StaleOwner('actual thread lease identity required')
        tx.assert_fence(token)

    @staticmethod
    def _owner(token):
        return {'thread_id': token.thread_id, 'owner_epoch': token.owner_epoch,
                'lease_id': token.lease_id}

    @staticmethod
    def _identity_in(tx, scope):
        generic=tx.get('lifecycle_scope_identities',scope)
        if generic:return generic['body']
        row = tx.get('work_instances', scope)
        if not row or row['body'].get('work_id') != scope:
            raise Conflict('current work identity missing')
        body = row['body']
        if any(not body.get(k) for k in ('run_id', 'plan_ref', 'graph_ref', 'contract_ref')):
            raise Conflict('complete work identity required')
        return {'scope': scope, 'run_ref': body['run_id'], 'plan_ref': body['plan_ref'],
                'graph_ref': body['graph_ref'], 'contract_ref': body['contract_ref'],
                'work_revision': row['revision']}

    @staticmethod
    def _same_work(left, right):
        return all(left.get(k) == right.get(k) for k in
                   ('scope', 'run_ref', 'plan_ref', 'graph_ref', 'contract_ref'))

    @classmethod
    def acquire_work_in(cls, tx, scope, token, *, purpose='execution'):
        cls._assert_thread_in(tx, token)
        scope=cls.scope_in(tx,scope,token)
        if purpose not in {'execution', 'cleanup', 'reconcile', 'diagnosis', 'recovery'}:
            raise ValueError('unregistered work execution purpose')
        if purpose in {'execution', 'recovery'} and cls._repair_isolations_in(tx, scope):
            raise Conflict('unresolved repair isolation prevents work execution')
        identity = cls._identity_in(tx, scope)
        old = tx.get('work_execution_leases', scope)
        if old and old['body']['owner'] != cls._owner(token):
            owner = old['body']['owner']
            # Query the real lease table; a token expiry is not authoritative.
            try:
                tx.assert_fence(LeaseToken(owner['thread_id'], owner['owner_epoch'], owner['lease_id'], 0))
            except StaleOwner:
                cls._quarantine_in(tx, scope, 'previous work owner expired', force=True)
            else:
                raise Conflict('work already executing under another thread lease')
        elif old:
            if not cls._same_work(old['body']['identity'], identity):
                raise Conflict('leased work identity changed')
            return old['body']
        body = {'identity': identity, 'owner': cls._owner(token), 'purpose': purpose,
                'acquired_at': tx.now}
        tx.put('work_execution_leases', scope, body)
        return body

    @classmethod
    def assert_work_in(cls, tx, scope, token):
        cls._assert_thread_in(tx, token)
        scope=cls.scope_in(tx,scope,token)
        row = tx.get('work_execution_leases', scope)
        if not row or row['body']['owner'] != cls._owner(token):
            raise StaleOwner('work execution owner differs from thread owner')
        if not cls._same_work(row['body']['identity'], cls._identity_in(tx, scope)):
            raise Conflict('leased work identity changed')
        return row['body']

    @classmethod
    def release_work_in(cls, tx, scope, token):
        scope=cls.scope_in(tx,scope,token)
        cls.assert_work_in(tx, scope, token)
        # Releasing execution ownership never deletes resource reservations.
        cls._quarantine_in(tx, scope, 'work released with unresolved disposition')
        tx.delete('work_execution_leases', scope)

    @staticmethod
    def _rows_in(tx, kind, scope):
        generic=tx.get('lifecycle_scope_identities',scope)
        if generic:
            body=generic['body'];run=body['run_ref'];physical=body['scope_ref']
            scopes={physical}
            scopes.update(row['key'] for row in tx.scan('work_instances') if row['body'].get('run_id')==run
                and (body['scope_kind']=='run' or row['body'].get('group_id')==physical))
            if body['scope_kind']=='run':scopes.update(row['body']['group_id'] for row in tx.scan('groups') if row['body']['run_id']==run)
            registered=tx.get('runs',run)
            if registered and tx.get('environment_plans',registered['body']['active_plan_ref']):scopes.add(run)
            return sorted((r for r in tx.scan(kind) if r['body'].get('scope') in scopes
                and r['body'].get('run_id',run)==run),key=lambda row:row['key'])
        return sorted((r for r in tx.scan(kind) if r['body'].get('scope') == scope),
                      key=lambda row: row['key'])

    @classmethod
    def _repair_isolations_in(cls, tx, scope):
        # No domain/operation ID is needed to preserve uncertainty. A marker
        # after a lost action response must not disappear through an empty join.
        return sorted((r for r in tx.scan('repair_isolations')
                       if r['body'].get('work_ref') == scope
                       and not cls.isolation_resolved_in(tx,r)), key=lambda row: row['key'])

    @classmethod
    def reconciliation_closure_material_in(cls,tx,scope):
        operations=cls._rows_in(tx,'operations',scope)
        refs={r['key'] for r in operations}
        facts=tx.get('work_safety_facts',scope)
        receipt_refs=(facts or {}).get('body',{}).get('receipt_refs',{})
        return {'operations':operations,'resources':cls._rows_in(tx,'resources',scope),
            'observations':cls._rows_in(tx,'observations',scope),'facts':facts,
            'receipts':{node:tx.get('receipts',ref) for node,ref in sorted(receipt_refs.items())},
            'collections':sorted((r for r in tx.scan('collections') if r['key'].rsplit(':',1)[0] in refs),key=lambda r:r['key']),
            'runner_events':sorted((r for r in tx.scan('runner_events') if r['key'].rsplit(':',1)[0] in refs),key=lambda r:r['key'])}

    @classmethod
    def isolation_resolved_in(cls, tx, isolation):
        row=tx.get('repair_isolation_resolutions','repair-resolution:' + isolation['body'].get('order_ref',''))
        if not row:
            return False
        body=row['body']; identity=body.get('identity',{})
        session=tx.get('lifecycle_reconciliations',body.get('session_ref',''))
        job=tx.get('jobs',body.get('job_ref',''))
        order=tx.get('repair_orders',identity.get('target_ref',''))
        outcome=tx.get('repair_outcomes',identity.get('target_ref',''))
        if (body.get('status')!='closed' or body.get('producer')!='lifecycle_reconciliation'
                or body.get('version')!=1 or body.get('isolation_ref')!=isolation['key']
                or body.get('isolation_hash')!=canonical_hash(isolation['body'])
                or not session or session['body'].get('identity')!=identity
                or session['body'].get('job_ref')!=body.get('job_ref')
                or not job or business_job_kind(job['body']['job_kind'])!='reconcile'
                or job['body']['payload'].get('reconciliation_ref')!=body.get('session_ref')
                or identity.get('operation_kind')!='site' or not order or not outcome
                or identity.get('order_hash')!=canonical_hash(order['body'])
                or identity.get('outcome_hash')!=canonical_hash(outcome['body'])
                or identity.get('target_ref')!=isolation['body'].get('order_ref')
                or identity.get('work_ref')!=isolation['body'].get('work_ref')
                or body.get('operation_refs')!=isolation['body'].get('operation_refs')
                or not body.get('operation_refs')):
            return False
        current=body.get('closure_identity',{})
        if any(current.get(k)!=identity.get(v) for k,v in (('scope','work_ref'),('run_ref','run_ref'),
                ('plan_ref','plan_ref'),('graph_ref','graph_ref'),('contract_ref','contract_ref'),
                ('work_revision','work_revision'))):
            return False
        material=body.get('closure_material',{})
        if body.get('closure_hash')!=canonical_hash(material):
            return False
        ops=material.get('operations',[])
        if (sorted(r['key'] for r in ops)!=sorted(body['operation_refs'])
                or any(not cls._operation_closed(r['body']) or r['body'].get('scope')!=identity['work_ref']
                       or r['body'].get('run_id')!=identity['run_ref'] for r in ops)
                or any(r['body'].get('status')!='closed' or r['body'].get('operation_id') not in body['operation_refs']
                       for r in material.get('resources',[]))
                or any(not ObservationService.closed_in(tx,r) for r in material.get('observations',[]))
                or not cls._closure_valid({'identity':current,'facts':material.get('facts'),
                                          'receipts':material.get('receipts',{})})):
            return False
        # 持久关闭后，正常的新 revision/新操作不使历史隔离复活。
        if session['body'].get('state')=='closed':
            settled=tx.get('repair_reconciliation_outcomes',identity['target_ref'])
            return bool(settled and settled['body'].get('resolution_ref')==row['key']
                and settled['body'].get('original_outcome_hash')==identity['outcome_hash'])
        # 创建同一事务内部尚未写 settled outcome，只接受实际仍拥有全部 fences 的 producer。
        owner=body.get('owner',{})
        try:
            token=LeaseToken(owner['thread_id'],owner['owner_epoch'],owner['lease_id'],0)
            tx.assert_fence(token)
            from ..runtime.tasks import TaskBackend
            from .agent_lifecycle import LifecycleService
            TaskBackend.assert_claim_in(tx,job['body'])
            LifecycleService.assert_owner_in(tx,identity['lifecycle_id'],token)
            cls.assert_work_in(tx,identity['work_ref'],token)
        except (KeyError,Conflict):
            return False
        return job['body']['thread_id']==owner['thread_id'] and session['body'].get('state')=='queued'

    @classmethod
    def _resource_state_in(cls, tx, scope):
        operations = cls._rows_in(tx, 'operations', scope)
        resources = cls._rows_in(tx, 'resources', scope)
        uncertain = [r['key'] for r in operations if not cls._operation_closed(r['body'])]
        owned = {r['key'] for r in resources}
        residual = {r['key'] for r in resources if r['body'].get('status') != 'closed'}
        for operation in operations:
            reported = set(operation['body'].get('status', {}).get('resource_refs', ()))
            residual.update(reported - owned)
            owned.update(reported)
        return operations, resources, uncertain, sorted(owned), sorted(residual)

    @staticmethod
    def _operation_closed(body):
        status = body.get('status') or {}
        return (body.get('phase') == 'terminal' and status.get('authoritative') is True
                and status.get('state') in {'completed', 'failed', 'cancelled'})

    @classmethod
    def _quarantine_in(cls, tx, scope, reason, *, force=False):
        operations, resources, uncertain, owned, residual = cls._resource_state_in(tx, scope)
        if not uncertain and not residual and not force:
            return
        domains = cls._rows_in(tx, 'resource_leases', scope)
        for row in domains:
            if row['body'].get('status') != 'quarantine':
                tx.put('resource_leases', row['key'], row['body'] | {'status': 'quarantine', 'reason': reason})
        body = {'scope': scope, 'operation_refs': [r['key'] for r in operations],
                'unknown_operation_refs': uncertain, 'residual_resources': residual,
                'isolation_domains': [r['key'] for r in domains], 'reason': reason}
        previous = tx.get('work_safety_quarantines', scope)
        if not previous or previous['body'] != body:
            tx.put('work_safety_quarantines', scope, body)

    @classmethod
    def _snapshot_in(cls, tx, scope):
        identity = cls._identity_in(tx, scope)
        fact_row = tx.get('work_safety_facts', scope)
        facts = fact_row['body'] if fact_row else {}
        receipts = {}
        refs = facts.get('receipt_refs')
        if isinstance(refs, dict):
            for node, ref in sorted(refs.items()):
                if isinstance(ref, str):
                    receipts[node] = tx.get('receipts', ref)
        snapshot = {'identity': identity, 'facts': fact_row, 'receipts': receipts}
        for kind in ('operations', 'resources', 'resource_leases', 'observations'):
            snapshot[kind] = cls._rows_in(tx, kind, scope)
        snapshot['quarantine'] = tx.get('work_safety_quarantines', scope)
        snapshot['repair_isolations'] = cls._repair_isolations_in(tx, scope)
        return snapshot

    @classmethod
    def _closure_valid(cls, snapshot):
        facts = (snapshot['facts'] or {}).get('body', {})
        identity = snapshot['identity']
        if (facts.get('schema_version') != 1 or type(facts.get('schema_version')) is not int
                or facts.get('producer') != 'work_runtime' or facts.get('producer_version') != '1'
                or any(facts.get(k) != v for k, v in identity.items())
                or facts.get('stopped') is not True or facts.get('attempt_closed') is not True
                or facts.get('admission_status') != 'closed'
                or facts.get('sampler_status') not in {'stopped', 'not_started'}
                or not facts.get('activation_id')):
            return False
        refs = facts.get('receipt_refs', {})
        if (set(refs) != {'N07', 'N09', 'N10'}
                or facts.get('attempt_receipt_ref') != refs.get('N10')):
            return False
        for node in ('N07', 'N09', 'N10'):
            receipt = snapshot['receipts'].get(node)
            if not receipt:
                return False
            receipt_identity = receipt['body'].get('identity', {})
            expected = {k: identity[k] for k in ('scope', 'run_ref', 'plan_ref', 'graph_ref')}
            expected.update(node_id=node, activation_id=facts['activation_id'])
            if any(receipt_identity.get(k) != v for k, v in expected.items()):
                return False
            result = receipt['body'].get('result', {})
            if result.get('execution_status') not in {'succeeded', 'failed', 'blocked', 'cancelled'}:
                return False
        return True

    @classmethod
    def _receipt_in(cls, tx, snapshot, *, observed_at, expires_at):
        scope = snapshot['identity']['scope']
        facts = (snapshot['facts'] or {}).get('body', {})
        operations, resources, uncertain, owned, residual = cls._resource_state_in(tx, scope)
        observations = snapshot['observations']
        sampler = ObservationService.sampler_status_in(tx,observations,empty=facts.get('sampler_status','unknown'))
        if sampler not in {'running', 'stopped', 'not_started', 'unknown'}:
            sampler = 'unknown'
        return ResourceDispositionReceipt(schema_version=1, work_ref=scope,
            operation_refs=tuple(r['key'] for r in operations),
            stopped=cls._closure_valid(snapshot) and sampler in {'stopped','not_started'}
                and not uncertain and not snapshot['repair_isolations'],
            admission_status='closed' if facts.get('admission_status') == 'closed' else 'open',
            sampler_status=sampler, owned_resources=tuple(owned), residual_resources=tuple(residual),
            isolation_domains=tuple(r['key'] for r in snapshot['resource_leases']
                                    if r['body'].get('status') == 'quarantine'),
            observed_at=observed_at, expires_at=expires_at, clock='wall',
            producer=cls.PRODUCER, producer_version=cls.VERSION)

    @classmethod
    def produce_disposition_in(cls, tx, scope, token, *, ttl):
        positive_duration(ttl)
        scope=cls.scope_in(tx,scope,token)
        cls.assert_work_in(tx, scope, token)
        cls._quarantine_in(tx, scope, 'resource disposition unknown')
        if tx.get('lifecycle_scope_identities',scope):
            from .scope_safety import ScopeSafety
            return ScopeSafety.produce_in(tx,scope,token,ttl)
        snapshot = cls._snapshot_in(tx, scope)
        observed_at = tx.now
        receipt = cls._receipt_in(tx, snapshot, observed_at=observed_at, expires_at=observed_at + ttl)
        body = {'schema_version': 1, 'identity': snapshot['identity'],
                'producer': cls._owner(token), 'snapshot_hash': canonical_hash(snapshot),
                'receipt': receipt.model_dump(mode='json')}
        ref = 'disposition:' + canonical_hash(body)
        tx.put('resource_disposition_receipts', ref, body, immutable=True)
        return ref

    @classmethod
    def verify_disposition_in(cls, tx, receipt_ref, scope, token, *, require_closed=True):
        scope=cls.scope_in(tx,scope,token)
        cls.assert_work_in(tx, scope, token)
        if tx.get('lifecycle_scope_identities',scope):
            from .scope_safety import ScopeSafety
            return ScopeSafety.verify_in(tx,receipt_ref,scope,token,require_closed=require_closed)
        row = tx.get('resource_disposition_receipts', receipt_ref)
        if not row:
            raise Conflict('disposition receipt missing')
        body = row['body']
        if (receipt_ref != 'disposition:' + canonical_hash(body)
                or body.get('producer') != cls._owner(token)
                or body.get('schema_version') != 1):
            raise Conflict('disposition receipt producer or identity invalid')
        try:
            receipt = ResourceDispositionReceipt.model_validate_json(json.dumps(body['receipt']))
        except (ValueError, KeyError, TypeError) as exc:
            raise Conflict('invalid disposition receipt') from exc
        if (receipt.work_ref != scope or receipt.producer != cls.PRODUCER
                or receipt.producer_version != cls.VERSION
                or not receipt.observed_at <= tx.now < receipt.expires_at):
            raise Conflict('disposition receipt expired or scope/producer invalid')
        snapshot = cls._snapshot_in(tx, scope)
        if body.get('identity') != snapshot['identity'] or body.get('snapshot_hash') != canonical_hash(snapshot):
            raise Conflict('disposition underlying facts changed')
        rebuilt = cls._receipt_in(tx, snapshot, observed_at=receipt.observed_at, expires_at=receipt.expires_at)
        if rebuilt != receipt:
            raise Conflict('disposition receipt does not match authoritative ledger')
        operations = snapshot['operations']
        if any(row['body'].get('run_id') != snapshot['identity']['run_ref'] for row in operations):
            raise Conflict('operation belongs to another work run')
        if require_closed and (not receipt.stopped or receipt.admission_status != 'closed'
                or receipt.sampler_status not in {'stopped', 'not_started'}
                or receipt.residual_resources or receipt.isolation_domains):
            raise Conflict('resource disposition not conclusively closed')
        return receipt

    def acquire_work(self, scope, token, **kwargs):
        with self.repository.transaction() as tx:
            return self.acquire_work_in(tx, scope, token, **kwargs)

    def assert_work(self, scope, token):
        with self.repository.transaction() as tx:
            return self.assert_work_in(tx, scope, token)

    def release_work(self, scope, token):
        with self.repository.transaction() as tx:
            return self.release_work_in(tx, scope, token)

    def produce_disposition(self, scope, token, *, ttl):
        with self.repository.transaction() as tx:
            return self.produce_disposition_in(tx, scope, token, ttl=ttl)

    def verify_disposition(self, receipt_ref, scope, token, *, require_closed=True):
        with self.repository.transaction() as tx:
            return self.verify_disposition_in(tx, receipt_ref, scope, token, require_closed=require_closed)

    def offline_diagnostic_capabilities(self, receipt_ref, scope, token, *, evidence_refs):
        """Only sealed evidence reads; this method grants no site capability."""
        with self.repository.transaction() as tx:
            self.verify_disposition_in(tx, receipt_ref, scope, token, require_closed=False)
            identity = self._identity_in(tx, scope)
            if not evidence_refs:
                raise Conflict('frozen evidence required for offline diagnosis')
            for ref in evidence_refs:
                row = tx.get('frozen_lifecycle_evidence', ref)
                if not row:
                    raise Conflict('frozen evidence missing')
                body = row['body']
                digest = body.get('content_hash', '')
                if (body.get('scope') != scope or body.get('run_ref') != identity['run_ref']
                        or body.get('frozen') is not True or not body.get('artifact_refs')
                        or not isinstance(digest, str) or len(digest) != 64
                        or any(c not in '0123456789abcdef' for c in digest)):
                    raise Conflict('frozen evidence scope or seal invalid')
            return ('read_frozen_evidence',)
