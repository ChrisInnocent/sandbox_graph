"""Persisted events are authoritative; notifications are optional accelerators."""
from pydantic import Field
from ..domain.models import Model, canonical_hash
from ..persistence.repository import Conflict

class EventEnvelope(Model):
    event_id: str
    producer_scope: str
    producer_epoch: int = Field(ge=1)
    aggregate_id: str
    aggregate_seq: int = Field(ge=1)
    run_ref: str
    plan_ref: str
    graph_ref: str
    kind: str
    payload: dict
    causation_id: str | None = None

class EventService:
    def __init__(self,repository): self.repository=repository
    @staticmethod
    def append_in(tx,event,token=None):
        if token:
            tx.assert_fence(token)
            if event.producer_epoch!=token.owner_epoch: raise Conflict('producer epoch mismatch')
        body=event.model_dump(mode='json')
        key=f'{event.aggregate_id}:{event.aggregate_seq:020}'
        existing=tx.get('events',key)
        if existing and existing['body']!=body: raise Conflict('aggregate sequence conflict')
        by_id=tx.get('event_ids',event.event_id)
        if by_id and by_id['body']['hash']!=canonical_hash(body): raise Conflict('event ID conflict')
        tx.put('events',key,body,immutable=True)
        tx.put('event_ids',event.event_id,{'hash':canonical_hash(body)},immutable=True)
        tx.put('outbox',f'event:{event.event_id}',body,immutable=True)
    def append(self,event,token=None):
        with self.repository.transaction() as tx: self.append_in(tx,event,token)
    def consume(self,consumer,aggregate_id,*,allowed_scopes,projection=None):
        with self.repository.transaction() as tx:
            key=f'{consumer}:{aggregate_id}'
            old=tx.get('cursors',key); cursor=old['body']['seq'] if old else 0
            accepted=[]
            while row:=tx.get('events',f'{aggregate_id}:{cursor+1:020}'):
                event=EventEnvelope.model_validate(row['body'])
                if event.producer_scope not in allowed_scopes: raise PermissionError('event producer scope not authorized')
                if projection: projection(tx,event)
                tx.put('inbox',f'{consumer}:{event.event_id}',{'event_id':event.event_id},immutable=True)
                cursor+=1; accepted.append(event)
            tx.put('cursors',key,{'seq':cursor})
            return accepted
