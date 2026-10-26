"""停止与归档共用的只读清单；有歧义的身份只阻塞，不授予处置权限。"""
from dataclasses import asdict, dataclass

from ..domain.models import canonical_hash


@dataclass(frozen=True)
class InventoryItem:
    kind: str
    key: str
    revision: int
    body_hash: str
    ownership: str
    reasons: tuple[str, ...]
    closed: bool


@dataclass(frozen=True)
class InventorySnapshot:
    run_ref: str
    scope_kind: str
    scope_ref: str
    items: tuple[InventoryItem, ...]

    @property
    def blockers(self):
        return tuple(i for i in self.items if not i.closed or i.ownership == 'ambiguous')

    @property
    def digest(self):
        return canonical_hash(asdict(self))

    def rows_in(self, tx, kind, *, open_only=True):
        return [tx.get(kind, i.key) for i in self.items if i.kind == kind
            and (not open_only or not i.closed or i.ownership == 'ambiguous')]


class LifecycleInventory:
    KINDS = ('operations', 'jobs', 'agent_provider_requests', 'resources', 'observations',
             'resource_leases', 'work_execution_leases', 'repair_isolations', 'candidate_isolations')
    PUBLICATION_JOBS = frozenset({'analysis', 'report', 'graph:analysis', 'graph:report'})

    @classmethod
    def collect_in(cls, tx, *, run_ref, scope_kind, scope_ref, extra_scopes=(), exclude_jobs=(),include_publication=False):
        if scope_kind not in {'run', 'group', 'work', 'node'}:
            raise ValueError('typed inventory scope required')
        from .lifecycle_stop_jobs import StopJobs
        from ..runtime.agent_bridge import RequestRecord
        from ..runtime.observation import ObservationService
        from ..runtime.tasks import business_job_kind
        from .agent_lifecycle import decode
        from .lifecycle_safety import LifecycleSafetyService
        ledgers = {kind: {r['key']: r for r in tx.scan(kind)} for kind in cls.KINDS}
        works = {r['key']: r['body'] for r in tx.scan('work_instances')}
        lives = {r['key']: r['body']['key'] for r in tx.scan('agent_lifecycles')}
        groups = [r['body'] for r in tx.scan('groups')]
        own_works = {k for k, w in works.items() if w['run_id'] == run_ref
            and (scope_kind == 'run' or scope_kind == 'group' and w.get('group_id') == scope_ref
                 or scope_kind in {'work', 'node'} and k == scope_ref)}
        own_scopes = {scope_ref, *own_works, *extra_scopes}
        if scope_kind == 'run':
            own_scopes.update(w.get('group_id') for w in works.values() if w['run_id'] == run_ref)
            own_scopes.update(g['group_id'] for g in groups if g['run_id'] == run_ref)
            own_scopes.update(l['scope_ref'] for l in lives.values() if l['run_ref'] == run_ref)
            own_scopes.update(r['body'].get('scope') for r in ledgers['operations'].values()
                              if r['body'].get('run_id') == run_ref)
        own_scopes.discard(None)

        def ownership(kind, key, body):
            runs, scopes, reasons = set(), set(), []
            missing, mismatch = False, False
            identity = body.get('identity', {})
            envelope = body.get('envelope', {})
            for run in (body.get('run_id'), body.get('run_ref'), identity.get('run_ref'), identity.get('run_id')):
                if run: runs.add(run)
            explicit_scope = body.get('scope') or body.get('scope_ref') or body.get('work_ref') or identity.get('work_ref') or identity.get('scope')
            if kind == 'work_execution_leases': explicit_scope = explicit_scope or key
            if kind == 'agent_provider_requests': explicit_scope = envelope.get('scope', {}).get('ref')
            if explicit_scope: scopes.add(explicit_scope)
            for op_ref in {body.get('operation_id'), *body.get('operation_refs', ())} - {None}:
                op = ledgers['operations'].get(op_ref)
                if not op:
                    missing = True
                    continue
                op = op['body']
                if op.get('run_id'): runs.add(op['run_id'])
                if op.get('scope'):
                    scopes.add(op['scope'])
                    mismatch |= bool(explicit_scope and explicit_scope != op['scope'])
                reasons.append('operation:' + op_ref)
            life_ref = envelope.get('lifecycle_id') or body.get('lifecycle_id') or identity.get('lifecycle_id') or body.get('payload', {}).get('lifecycle_id')
            if life_ref:
                life = lives.get(life_ref)
                if life:
                    runs.add(life['run_ref']); scopes.add(life['scope_ref'])
                    if explicit_scope:
                        if kind=='agent_provider_requests' and envelope.get('scope',{}).get('kind')=='candidate':
                            proposal=tx.get('change_proposals',explicit_scope)
                            source=(proposal or {}).get('body',{}).get('proposal',{}).get('source_key',{})
                            mismatch |= source!=life
                            missing |= proposal is None
                        else:mismatch |= explicit_scope != life['scope_ref']
                    reasons.append('lifecycle:' + life_ref)
                else:
                    missing = True
            for scope in tuple(scopes):
                if scope in works:
                    runs.add(works[scope]['run_id'])
                    reasons.append('work:' + scope)
            relevant = run_ref in runs or bool(scopes & own_scopes)
            if not relevant:
                return None
            # A consistent explicit foreign run wins over shared group labels.
            if runs and run_ref not in runs and not mismatch and not missing and len(runs) == 1:
                return None
            if scope_kind != 'run' and not scopes & own_scopes:
                return None
            if len(runs) > 1 or mismatch or missing or not runs:
                return 'ambiguous', tuple(sorted(set(reasons + ['identity_missing_or_conflicting'])))
            if runs != {run_ref}:
                return None
            return 'owned', tuple(sorted(set(reasons + ['run:' + run_ref])))

        def closed(kind, key, row):
            body = row['body']
            if kind == 'operations': return LifecycleSafetyService._operation_closed(body)
            if kind == 'jobs': return body.get('status') not in {'pending', 'running', 'draining'}
            if kind == 'agent_provider_requests': return decode(RequestRecord, body).terminal
            if kind == 'observations': return ObservationService.closed_in(tx, row)
            if kind == 'resources': return body.get('status') == 'closed'
            if kind == 'repair_isolations': return LifecycleSafetyService.isolation_resolved_in(tx, row)
            if kind == 'candidate_isolations':
                proof = tx.get('candidate_disposition_proofs', key)
                return bool(proof and proof['body'].get('isolation_ref') == key
                    and proof['body'].get('children_stopped') is True
                    and proof['body'].get('isolation_hash') == canonical_hash(body))
            return False

        items = []
        for kind, rows in ledgers.items():
            for key, row in sorted(rows.items()):
                if kind == 'jobs' and (key in exclude_jobs or StopJobs.valid_in(tx,row['body']) or (not include_publication and business_job_kind(row['body'].get('job_kind', '')) in cls.PUBLICATION_JOBS)):
                    continue
                owner = ownership(kind, key, row['body'])
                if owner is not None:
                    items.append(InventoryItem(kind, key, row['revision'], canonical_hash(row['body']),
                        owner[0], owner[1], closed(kind, key, row)))
        return InventorySnapshot(run_ref, scope_kind, scope_ref, tuple(items))
