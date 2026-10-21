"""Version-specific waits. Silence and expiry never count as approval."""
from pydantic import Field
from ..domain.models import Model, canonical_hash
from ..persistence.repository import Conflict

class WaitRecord(Model):
    wait_id: str
    activation_id: str
    thread_id: str
    scope: str
    plan_ref: str
    graph_ref: str
    version: int = Field(ge=1)
    deadline: float
    aggregate_id: str | None = None
    cursor: int = 0
    question_id: str | None = None
    interrupt_id: str | None = None
    retention_policy: str = 'quarantine'

class DecisionEnvelope(Model):
    decision_id: str
    wait_id: str
    wait_version: int
    scope: str
    plan_ref: str
    graph_ref: str
    question_id: str
    interrupt_id: str
    actor: str
    answer: dict

class WaitService:
    def __init__(self,repository): self.repository=repository
    def park(self,wait):
        with self.repository.transaction() as tx:
            old=tx.get('waits',wait.wait_id)
            body=wait.model_dump(mode='json')
            if old:
                if old['body']['record']!=body: raise Conflict('wait identity conflict')
                return old['body']['status']
            ready=bool(wait.aggregate_id and tx.get('events',f'{wait.aggregate_id}:{wait.cursor+1:020}'))
            status='expired' if wait.deadline<=tx.now else 'ready' if ready else 'waiting'
            tx.put('waits',wait.wait_id,{'record':body,'status':status})
            tx.put('wait_cursors',wait.wait_id,{'seq':wait.cursor})
            return status
    def wake(self,wait_id,*,version):
        with self.repository.transaction() as tx:
            row=tx.get('waits',wait_id)
            if not row: raise KeyError(wait_id)
            b=row['body']; w=WaitRecord.model_validate(b['record'])
            if w.version!=version: raise Conflict('stale wait version')
            if b['status'] in {'resumed','expired','cancelled'}: return None
            if w.deadline<=tx.now: return None
            if b['status']!='ready' and not (w.aggregate_id and tx.get('events',f'{w.aggregate_id}:{w.cursor+1:020}')): return None
            tx.put('waits',wait_id,b|{'status':'resumed'})
            tx.put('outbox',f'wakeup:{wait_id}:{version}',{'thread_id':w.thread_id,'wait_id':wait_id,'version':version},immutable=True)
            return w.thread_id
    def accept(self,decision):
        with self.repository.transaction() as tx:
            old=tx.get('decisions',decision.decision_id)
            body=decision.model_dump(mode='json')
            if old:
                if old['body']!=body: raise Conflict('decision ID conflict')
                return decision.answer
            row=tx.get('waits',decision.wait_id)
            if not row: raise Conflict('unknown wait')
            b=row['body']; w=WaitRecord.model_validate(b['record'])
            expected=(w.scope,w.plan_ref,w.graph_ref,w.question_id,w.interrupt_id,w.version)
            actual=(decision.scope,decision.plan_ref,decision.graph_ref,decision.question_id,decision.interrupt_id,decision.wait_version)
            if expected!=actual or w.deadline<=tx.now or b['status']!='waiting': raise Conflict('STALE_DECISION')
            tx.put('decisions',decision.decision_id,body,immutable=True)
            tx.put('waits',decision.wait_id,b|{'status':'ready','decision_id':decision.decision_id})
            return decision.answer
    def watchdog(self):
        expired=[]
        with self.repository.transaction() as tx:
            for row in tx.scan('waits'):
                b=row['body']; w=WaitRecord.model_validate(b['record'])
                if b['status'] in {'waiting','ready'} and w.deadline<=tx.now:
                    tx.put('waits',row['key'],b|{'status':'expired'})
                    tx.put('outbox',f'wait-expired:{row["key"]}',{'kind':'resource_disposition','scope':w.scope,'policy':w.retention_policy,'approved':False},immutable=True)
                    expired.append(row['key'])
        return expired
