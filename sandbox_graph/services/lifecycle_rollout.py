"""Audited staged admission; disabling never disables original-effect cleanup.

An absent record preserves explicitly configured legacy workers. New deployments
without lifecycle bindings remain off. This switch narrows, never grants, the
existing policy, scope, execution authorization and candidate feature checks.
"""
import math
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict

MODES={'off':(), 'actions':('action',), 'scoped':('action','model'),
       'candidate':('action','model','candidate')}


class LifecycleRolloutService:
    def __init__(self,repository):self.repository=repository

    def configure(self,command):
        required={'request_id','run_ref','expected_revision','principal','authorization_ref','mode','scopes'}
        if not isinstance(command,dict) or set(command)!=required or command['mode'] not in MODES:
            raise ValueError('explicit lifecycle rollout command required')
        if any(not isinstance(command[k],str) or not command[k] for k in ('request_id','run_ref','principal','authorization_ref')):
            raise ValueError('rollout identities required')
        revision=command['expected_revision']
        if revision is not None and (type(revision) is not int or revision<0):raise ValueError('rollout revision must be nonnegative')
        scopes=command['scopes']
        if not isinstance(scopes,list) or not 1<=len(scopes)<=1000:raise ValueError('finite explicit rollout scopes required')
        for scope in scopes:
            if (not isinstance(scope,dict) or set(scope)!={'scope_ref','node_ids'}
                    or not isinstance(scope['scope_ref'],str) or not scope['scope_ref']
                    or not isinstance(scope['node_ids'],list) or not 1<=len(scope['node_ids'])<=1000
                    or any(not isinstance(n,str) or not n for n in scope['node_ids'])
                    or len(set(scope['node_ids']))!=len(scope['node_ids'])):
                raise ValueError('exact rollout scope and node selection required')
        if len({s['scope_ref'] for s in scopes})!=len(scopes):raise ValueError('duplicate rollout scope')
        digest=canonical_hash(command)
        with self.repository.transaction() as tx:
            old=tx.get('lifecycle_rollout_commands',command['request_id'])
            if old:
                if old['body']['input_hash']!=digest:raise Conflict('rollout request input changed')
                return old['body']['result']
            row=tx.get('lifecycle_rollout_authorizations',command['authorization_ref']);auth=row['body'] if row else {}
            expiry=auth.get('expires_at')
            if (auth.get('principal')!=command['principal'] or auth.get('run_ref')!=command['run_ref']
                    or auth.get('revoked') is not False or command['mode'] not in auth.get('modes',())
                    or type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=tx.now
                    or not {s['scope_ref'] for s in scopes}<=set(auth.get('scope_refs',()))):
                raise PermissionError('current exact-scope rollout authorization required')
            if not tx.get('runs',command['run_ref']):raise ValueError('rollout run is not registered')
            current=tx.get('lifecycle_rollouts',command['run_ref'])
            if (current['revision'] if current else None)!=revision:raise Conflict('rollout revision changed')
            body={'mode':command['mode'],'scopes':scopes,'principal':command['principal'],'request_id':command['request_id']}
            tx.put('lifecycle_rollouts',command['run_ref'],body,expected_revision=revision,immutable=current is None)
            result=body|{'run_ref':command['run_ref'],'revision':0 if revision is None else revision+1}
            tx.put('lifecycle_rollout_commands',command['request_id'],{'command':command,'input_hash':digest,
                'result':result,'observed_at':tx.now},immutable=True)
            return result

    @staticmethod
    def assert_enabled_in(tx,record,capability):
        if capability not in {'action','model','candidate'}:raise ValueError('unknown rollout capability')
        row=tx.get('lifecycle_rollouts',record.key.run_ref)
        if row is None:return  # legacy explicit policy/binding still checked by caller
        value=row['body']
        selected=any(s['scope_ref']==record.key.scope_ref and record.key.node_id in s['node_ids'] for s in value['scopes'])
        if not selected or capability not in MODES.get(value.get('mode'),()):
            raise PermissionError('lifecycle rollout denies '+capability+' for this scope/node')
