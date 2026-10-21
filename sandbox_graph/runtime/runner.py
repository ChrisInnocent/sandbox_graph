"""Registered structured runner protocol and persisted submission/reconciliation.

The gateway is responsible for external idempotency and durable handles. A missing
response is never evidence that submission did not happen.
"""
from typing import Literal, Protocol
from pydantic import Field, model_validator
from ..domain.models import Model, Operation, ScriptRef, canonical_hash
from ..persistence.repository import Conflict
from ..services.revisions import RevisionService

class RunnerStatus(Model):
    state: Literal['not_submitted','running','completed','failed','cancelled','unknown']
    task_handle: str | None = None
    authoritative: bool = False
    resource_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    output: dict = Field(default_factory=dict)
    reason: str | None = None

class RunnerCollection(Model):
    events: tuple[dict, ...] = ()
    last_seq: int = Field(default=0,ge=0)
    artifact_refs: tuple[str, ...] = ()
    interrupted: bool = False
    missing_sequences: tuple[int, ...] = ()

class CleanupResult(Model):
    status: Literal['complete','incomplete','unknown']
    resource_ids: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode='after')
    def check_complete(self):
        if self.status=='complete' and self.remaining:
            raise ValueError('complete cleanup cannot have remaining resources')
        return self

class RunnerGateway(Protocol):
    async def submit(self,operation: Operation) -> RunnerStatus: ...
    async def reconcile(self,operation_id: str,scope: str) -> RunnerStatus: ...
    async def cancel(self,operation_id: str,scope: str,reason: str) -> RunnerStatus: ...
    async def collect(self,operation_id: str,scope: str,last_seq: int=0) -> RunnerCollection: ...
    async def cleanup(self,scope: str,resource_ids: tuple[str,...]) -> CleanupResult: ...

class OperationService:
    def __init__(self,repository,gateway: RunnerGateway,scripts: dict[tuple[str,str],ScriptRef]):
        self.repository=repository; self.gateway=gateway; self.scripts=dict(scripts)

    def _validate(self,operation):
        Operation.model_validate(operation.model_dump())
        key=(operation.script_ref.script_id,operation.script_ref.version)
        if self.scripts.get(key)!=operation.script_ref: raise ValueError('only registered versioned scripts may execute')
        if not operation.run_id: raise ValueError('run_id and dispatch gate required')

    async def submit(self,operation: Operation,token) -> RunnerStatus:
        self._validate(operation)
        body=operation.model_dump(mode='json'); digest=canonical_hash(body)
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            existing=tx.get('operations',operation.operation_id)
            if existing:
                if existing['body']['request_hash']!=digest: raise Conflict('operation ID input conflict')
                record=existing['body']
                if record['phase']=='terminal': return RunnerStatus.model_validate(record['status'])
            else:
                if operation.deadline<=tx.now: raise Conflict('operation deadline exceeded')
                record={'request':body,'request_hash':digest,'phase':'intent','scope':operation.scope,'run_id':operation.run_id,'thread_id':token.thread_id}
                tx.put('operations',operation.operation_id,record,immutable=True)
        if record['phase']!='intent':
            return await self.reconcile(operation.operation_id,operation.scope,token)
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            current=tx.get('operations',operation.operation_id)['body']
            if current['phase']!='intent': raise Conflict('submission already advanced; reconcile')
            if operation.deadline<=tx.now: raise Conflict('operation deadline exceeded')
            RevisionService.permit_in(tx,operation.run_id,operation.operation_id,generation=operation.dispatch_generation,scope=operation.scope,kind=operation.submission_kind)
            tx.put('operations',operation.operation_id,current|{'phase':'submission_started','owner_epoch':token.owner_epoch})
        try:
            status=RunnerStatus.model_validate(await self.gateway.submit(operation))
        except Exception as exc:
            status=RunnerStatus(state='unknown',reason=f'{type(exc).__name__}: submission response unavailable')
        return self._record_status(operation.operation_id,operation.scope,status,token)

    def _record_status(self,operation_id,scope,status,token):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            row=tx.get('operations',operation_id)
            if not row or row['body']['scope']!=scope: raise PermissionError('operation scope mismatch')
            phase='terminal' if status.state in {'completed','failed','cancelled'} and status.authoritative else 'running' if status.state=='running' else 'uncertain'
            tx.put('operations',operation_id,row['body']|{'phase':phase,'status':status.model_dump(mode='json')})
            for resource in status.resource_refs:
                old=tx.get('resources',resource)
                owner={'scope':scope,'operation_id':operation_id,'status':'active'}
                if old and (old['body']['scope'],old['body']['operation_id'])!=(scope,operation_id): raise Conflict('resource ownership conflict')
                tx.put('resources',resource,owner)
            if phase=='uncertain':
                for domain in tx.scan('resource_leases'):
                    if domain['body']['scope']==scope and domain['body'].get('run_id',row['body']['run_id'])==row['body']['run_id']:
                        tx.put('resource_leases',domain['key'],domain['body']|{'status':'quarantine','reason':'external effect unknown'})
        return status

    async def reconcile(self,operation_id,scope,token):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            row=tx.get('operations',operation_id)
            if not row or row['body']['scope']!=scope: raise PermissionError('operation scope mismatch')
        status=RunnerStatus.model_validate(await self.gateway.reconcile(operation_id,scope))
        return self._record_status(operation_id,scope,status,token)

    async def cancel(self,operation_id,scope,reason,token):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            row=tx.get('operations',operation_id)
            if not row or row['body']['scope']!=scope: raise PermissionError('operation scope mismatch')
            tx.put('cancellations',operation_id,{'scope':scope,'reason':reason,'status':'requested'},immutable=True)
        status=RunnerStatus.model_validate(await self.gateway.cancel(operation_id,scope,reason))
        # Cancel acknowledgement is recorded separately; never claim resource cleanup.
        self._record_status(operation_id,scope,status,token)
        return status

    async def collect(self,operation_id,scope,token,last_seq=0):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            op=tx.get('operations',operation_id)
            if not op or op['body']['scope']!=scope: raise PermissionError('operation scope mismatch')
        result=RunnerCollection.model_validate(await self.gateway.collect(operation_id,scope,last_seq))
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            seqs=[]
            for event in result.events:
                seq=event.get('seq')
                if not isinstance(seq,int) or seq<=0: raise Conflict('event sequence missing')
                tx.put('runner_events',f'{operation_id}:{seq:020}',event,immutable=True)
                if seq>last_seq: seqs.append(seq)
            expected=set(range(last_seq+1,result.last_seq+1))
            if expected-set(seqs) != set(result.missing_sequences): raise Conflict('runner collection contains undeclared sequence gaps')
            tx.put('collections',f'{operation_id}:{result.last_seq}',result.model_dump(mode='json'),immutable=True)
        return result

    async def cleanup(self,scope,resource_ids,token):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            for resource in resource_ids:
                record=tx.get('resources',resource)
                if not record or record['body']['scope']!=scope: raise PermissionError('resource not owned by scope')
        result=CleanupResult.model_validate(await self.gateway.cleanup(scope,tuple(resource_ids)))
        if set(result.resource_ids)!=set(resource_ids) or not set(result.remaining)<=set(resource_ids): raise Conflict('cleanup result resource mismatch')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            tx.put('cleanup_records',f'{scope}:{canonical_hash(list(resource_ids))}',result.model_dump(mode='json'))
            for resource in resource_ids:
                old=tx.get('resources',resource)
                if old['body']['scope']!=scope: raise PermissionError('resource owner changed')
                tx.put('resources',resource,old['body']|{'status':'closed' if result.status=='complete' else 'unresolved'})
        return result
