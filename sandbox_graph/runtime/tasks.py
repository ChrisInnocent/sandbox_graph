"""Durable jobs, bounded claims and idempotent result commits."""
from typing import Literal
from pydantic import Field
from ..domain.models import Model, canonical_hash
from ..persistence.repository import Conflict, StaleOwner,positive_duration

class JobSpec(Model):
    job_id: str
    job_kind: str
    scope_ref: str
    thread_id: str
    activation_id: str
    dedupe_key: str
    due_at: float
    deadline: float
    run_id: str = ''
    dispatch_generation: int = 1
    max_attempts: int = Field(default=3,ge=1)
    payload: dict = Field(default_factory=dict)

class TaskBackend:
    def __init__(self,repository): self.repository=repository
    @staticmethod
    def enqueue_in(tx,spec):
        # Dedupe binds the logical job input; the freshly generated job_id is identity, not input.
        body=spec.model_dump(mode='json')
        digest=canonical_hash({k:v for k,v in body.items() if k!='job_id'})
        old=tx.get('job_dedupe',spec.dedupe_key)
        if old:
            if old['body']['hash']!=digest: raise Conflict('dedupe key has different job input')
            return old['body']['job_id']
        if tx.get('jobs',spec.job_id): raise Conflict('job ID already exists')
        tx.put('job_dedupe',spec.dedupe_key,{'job_id':spec.job_id,'hash':digest},immutable=True)
        tx.put('jobs',spec.job_id,body|{'status':'pending','owner_epoch':0,'lease_until':0,'retry_count':0})
        tx.put('outbox',f'job:{spec.job_id}',{'kind':'job_ready','job_id':spec.job_id},immutable=True)
        return spec.job_id
    def enqueue(self,spec):
        with self.repository.transaction() as tx: return self.enqueue_in(tx,spec)
    def claim(self,owner,ttl):
        positive_duration(ttl)
        with self.repository.transaction() as tx:
            for row in sorted(tx.scan('jobs'),key=lambda r:(r['body']['due_at'],r['key'])):
                b=row['body']
                if b['status'] not in {'pending','running','draining'} or b['due_at']>tx.now: continue
                if b['status'] in {'running','draining'} and b['lease_until']>tx.now: continue
                if b['deadline']<=tx.now or b['retry_count']>=b['max_attempts']:
                    result={'execution_status':'failed','error':{'code':'DEADLINE_OR_ATTEMPT_BUDGET'}}
                    tx.put('jobs',row['key'],b|{'status':'failed','reason':'deadline_or_attempt_budget','result':result,'owner_epoch':b['owner_epoch']+1})
                    tx.put('outbox',f'job-complete:{row["key"]}',{'kind':'job_complete','job_id':row['key'],'result':result},immutable=True)
                    continue
                if b.get('run_id'):
                    gate=tx.get('dispatch_gates',b['run_id'])
                    if gate and (not gate['body']['open'] or gate['body']['generation']!=b['dispatch_generation']) and b['job_kind'] not in {'cleanup','reconcile','analysis','report','graph:analysis','graph:report'} and b['status']!='draining' and not b.get('payload',{}).get('continuation_of'):
                        result={'execution_status':'cancelled','error':{'code':'DISPATCH_SUPERSEDED'}}
                        tx.put('jobs',row['key'],b|{'status':'superseded','result':result,'owner_epoch':b['owner_epoch']+1})
                        tx.put('outbox',f'job-complete:{row["key"]}',{'kind':'job_complete','job_id':row['key'],'result':result},immutable=True)
                        continue
                claimed=b|{'status':'running','owner_id':owner,'owner_epoch':b['owner_epoch']+1,'lease_until':min(tx.now+ttl,b['deadline']),'retry_count':b['retry_count']+1}
                tx.put('jobs',row['key'],claimed)
                return claimed
        return None
    def renew_claim(self,job_id,*,owner_epoch,ttl):
        positive_duration(ttl)
        with self.repository.transaction() as tx:
            row=tx.get('jobs',job_id)
            if not row or row['body']['owner_epoch']!=owner_epoch or row['body']['status'] not in {'running','draining'} or row['body']['lease_until']<=tx.now:
                raise StaleOwner('job claim expired')
            tx.put('jobs',job_id,row['body']|{'lease_until':min(tx.now+ttl,row['body']['deadline'])})
    def finish(self,job_id,*,owner_epoch,result,status='complete'):
        if status not in {'complete','blocked','failed','cancelled'}: raise ValueError('invalid terminal job status')
        with self.repository.transaction() as tx:
            row=tx.get('jobs',job_id)
            if not row: raise KeyError(job_id)
            b=row['body']
            if b['owner_epoch']!=owner_epoch: raise StaleOwner('job epoch changed')
            if b['status'] in {'complete','blocked','failed','cancelled'}:
                if b['result']!=result: raise Conflict('conflicting job receipt')
                return
            if b['status'] not in {'running','draining'} or b['lease_until']<=tx.now: raise StaleOwner('job claim expired')
            tx.put('jobs',job_id,b|{'status':status,'result':result})
            tx.put('outbox',f'job-complete:{job_id}',{'kind':'job_complete','job_id':job_id,'result':result},immutable=True)
