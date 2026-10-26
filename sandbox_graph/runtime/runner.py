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
    sampler_receipts: tuple[dict, ...] = ()

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

    def _sampler_observer(self,record,method):
        request=record.get('request',{})
        node=request.get('node_id')
        if (getattr(self.gateway,'sampler_receipt_contract',None)!='bench-sampler-collection.v1'
                or node not in {'N04','N07'} or request.get('input',{}).get('step_id')!=node):
            return None
        return getattr(self.gateway,method,None)

    def _sampler_stop_intent_in(self,tx,operation_id,record,token,*,kind=None,safety_call_ref=None):
        """Freeze the original admitted action. Query only reads this record."""
        row=tx.get('sampler_stop_intents',operation_id)
        if row:
            intent=row['body']
            if (intent['request_hash']!=record.get('request_hash')
                    or intent['scope']!=record['scope'] or intent['run_id']!=record['run_id']
                    or intent['execution_binding_hash']!=canonical_hash(record.get('execution_binding'))
                    or tx.now>=intent['deadline']):
                raise Conflict('original sampler stop intent identity or deadline differs')
            authority=intent['authority']
            if authority['kind']=='safety':
                call=tx.get('reconciliation_calls',authority['call_ref'])
                reservation=tx.get('budget_reservations',authority['call_ref'])
                auth=tx.get('reconciliation_authorizations',authority['authorization_ref'])
                if (not call or call['body'].get('kind')!='stop' or call['body'].get('target')!=operation_id
                        or not call['body'].get('network_started') or not reservation
                        or reservation['body'].get('safety') is not True
                        or reservation['body'].get('operation_kind')!='stop'
                        or not auth or auth['body'].get('revoked')
                        or tx.now>=auth['body']['expires_at'] or 'stop' not in auth['body']['permissions']):
                    raise PermissionError('original sampler safety stop provenance missing')
            return intent
        if kind is None:return None
        from ..services.lifecycle_safety import LifecycleSafetyService
        LifecycleSafetyService.assert_work_in(tx,record['scope'],token)
        authority={'kind':kind}
        if kind=='safety':
            call=tx.get('reconciliation_calls',safety_call_ref) if safety_call_ref else None
            reservation=tx.get('budget_reservations',safety_call_ref) if safety_call_ref else None
            session=tx.get('lifecycle_reconciliations',call['body']['session_ref']) if call else None
            auth=tx.get('reconciliation_authorizations',session['body']['authorization_ref']) if session else None
            job=tx.get('jobs',call['body']['job_ref']) if call else None
            if (not call or call['body']['kind']!='stop' or call['body']['target']!=operation_id
                    or not call['body']['network_started'] or not reservation or not session or not auth or not job
                    or reservation['body'].get('safety') is not True
                    or reservation['body'].get('operation_kind')!='stop'
                    or tx.get('budget_settlements',safety_call_ref)
                    or session['body']['identity']['work_ref']!=record['scope']):
                raise PermissionError('reserved original sampler stop call required')
            deadline=min(token.lease_until,job['body']['deadline'],auth['body']['expires_at'])
            authority.update(call_ref=safety_call_ref,authorization_ref=session['body']['authorization_ref'])
        else:
            deadline=min(token.lease_until,record.get('request',{}).get('deadline',tx.now))
        if deadline<=tx.now:raise Conflict('sampler stop authority expired')
        binding=record.get('execution_binding',{})
        owner=({'session_id':binding['sampler_session_id'],
                'process_identity':binding['sampler_process_identity']} if 'sampler_session_id' in binding
               else self.gateway.sampler_owner(operation_id,record['scope']))
        intent={'contract':'bench-sampler-stop-intent.v1','operation_id':operation_id,
            'scope':record['scope'],'run_id':record['run_id'],'request_hash':record.get('request_hash'),
            'execution_binding_hash':canonical_hash(record.get('execution_binding')),'adapter_owner':owner,
            'authority':authority,'owner':LifecycleSafetyService._owner(token),
            'admitted_at':tx.now,'deadline':deadline}
        tx.put('sampler_stop_intents',operation_id,intent,immutable=True)
        return intent

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
            from ..services.measurement_generation import MeasurementGenerationService
            MeasurementGenerationService.validate_submission_in(tx,operation.run_id,operation.scope,
                kind=operation.submission_kind,token=token,node_id=operation.node_id)
            if operation.submission_kind=='ordinary':
                gate=tx.get('dispatch_gates',operation.run_id)
                if not gate or not gate['body']['open'] or gate['body']['generation']!=operation.dispatch_generation:
                    raise Conflict('dispatch gate closed or generation superseded before submission')
            RevisionService.permit_in(tx,operation.run_id,operation.operation_id,generation=operation.dispatch_generation,scope=operation.scope,kind=operation.submission_kind)
            current=current|{'phase':'submission_started','owner_epoch':token.owner_epoch}
            binder=getattr(self.gateway,'bind_execution',None)
            if binder is not None:
                work=tx.get('work_instances',operation.scope)
                if work:
                    from ..services.lifecycle_safety import LifecycleSafetyService
                    LifecycleSafetyService.assert_work_in(tx,operation.scope,token)
                    w=work['body']
                    binding={k:w[k] for k in ('run_id','group_id','plan_ref','graph_ref','contract_ref')}
                    binding.update(scope=operation.scope,work_id=w['work_id'],
                        measurement_generation=w.get('key',{}).get('measurement_generation',1),
                        owner=LifecycleSafetyService._owner(token),
                        job_refs=sorted(r['key'] for r in tx.scan('jobs')
                            if r['body'].get('thread_id')==token.thread_id and r['body'].get('status')=='running'))
                    current['execution_binding']=binder(operation,binding)
            tx.put('operations',operation.operation_id,current)
            stop_intent=None
            if (getattr(self.gateway,'sampler_receipt_contract',None)=='bench-sampler-collection.v1'
                    and operation.node_id=='N07' and current.get('execution_binding')):
                stop_intent=self._sampler_stop_intent_in(tx,operation.operation_id,current,token,kind='mandatory')
        if stop_intent is not None:
            self.gateway.authorize_sampler_stop(operation.operation_id,operation.scope,stop_intent)
        try:
            status=RunnerStatus.model_validate(await self.gateway.submit(operation))
        except Exception as exc:
            status=RunnerStatus(state='unknown',reason=f'{type(exc).__name__}: submission response unavailable')
        return self._record_status(operation.operation_id,operation.scope,status,token)

    def _record_status(self,operation_id,scope,status,token,*,safety_guard=None):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            if safety_guard is not None: safety_guard(tx)
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

    async def reconcile(self,operation_id,scope,token,*,safety_guard=None):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            row=tx.get('operations',operation_id)
            if not row or row['body']['scope']!=scope: raise PermissionError('operation scope mismatch')
            if safety_guard is not None: safety_guard(tx)
            observed=self._sampler_observer(row['body'],'reconcile_observed')
            intent=self._sampler_stop_intent_in(tx,operation_id,row['body'],token) if observed else None
        status=RunnerStatus.model_validate(await (observed(operation_id,scope,intent) if observed
            else self.gateway.reconcile(operation_id,scope)))
        status=self._record_status(operation_id,scope,status,token,safety_guard=safety_guard)
        if getattr(self.gateway,'sampler_receipt_contract',None)=='bench-sampler-collection.v1':
            await self.collect(operation_id,scope,token,safety_guard=safety_guard)
        return status

    async def cancel(self,operation_id,scope,reason,token,*,safety_guard=None,safety_call_ref=None):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            row=tx.get('operations',operation_id)
            if not row or row['body']['scope']!=scope: raise PermissionError('operation scope mismatch')
            if safety_guard is not None: safety_guard(tx)
            observed=self._sampler_observer(row['body'],'cancel_observed')
            intent=(self._sampler_stop_intent_in(tx,operation_id,row['body'],token,
                kind='safety' if safety_guard is not None else 'ordinary',safety_call_ref=safety_call_ref)
                if observed else None)
            tx.put('cancellations',operation_id,{'scope':scope,'reason':reason,'status':'requested'},immutable=True)
        status=RunnerStatus.model_validate(await (observed(operation_id,scope,reason,intent) if observed
            else self.gateway.cancel(operation_id,scope,reason)))
        # Cancel acknowledgement is recorded separately; never claim resource cleanup.
        self._record_status(operation_id,scope,status,token,safety_guard=safety_guard)
        if getattr(self.gateway,'sampler_receipt_contract',None)=='bench-sampler-collection.v1':
            await self.collect(operation_id,scope,token,safety_guard=safety_guard)
        return status

    async def collect(self,operation_id,scope,token,last_seq=0,*,safety_guard=None):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            if safety_guard is not None: safety_guard(tx)
            op=tx.get('operations',operation_id)
            if not op or op['body']['scope']!=scope: raise PermissionError('operation scope mismatch')
        result=RunnerCollection.model_validate(await self.gateway.collect(operation_id,scope,last_seq))
        # Validate the entire payload before any writes, including memory-backed
        # repositories whose test transactions do not implement rollback.
        verifier=getattr(self.gateway,'verify_sampler_receipt',None)
        for receipt in result.sampler_receipts:
            if verifier is None: raise Conflict('sampler receipt adapter not registered')
            verifier(receipt,operation_id,scope)
        seqs=[event.get('seq') for event in result.events]
        if any(type(seq) is not int or seq<=0 for seq in seqs): raise Conflict('event sequence missing')
        if set(range(last_seq+1,result.last_seq+1))-{seq for seq in seqs if seq>last_seq} != set(result.missing_sequences):
            raise Conflict('runner collection contains undeclared sequence gaps')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            if safety_guard is not None: safety_guard(tx)
            from .observation import ObservationService
            for receipt in result.sampler_receipts:
                ObservationService.consume_sampler_in(tx,receipt,
                    collection_ref=f'{operation_id}:{result.last_seq}',operation_id=operation_id,
                    token=token,validate_only=True)
            seqs=[]
            for event in result.events:
                seq=event.get('seq')
                if not isinstance(seq,int) or seq<=0: raise Conflict('event sequence missing')
                tx.put('runner_events',f'{operation_id}:{seq:020}',event,immutable=True)
                if seq>last_seq: seqs.append(seq)
            expected=set(range(last_seq+1,result.last_seq+1))
            if expected-set(seqs) != set(result.missing_sequences): raise Conflict('runner collection contains undeclared sequence gaps')
            collection_ref=f'{operation_id}:{result.last_seq}'
            tx.put('collections',collection_ref,result.model_dump(mode='json'),immutable=True)
            from .observation import ObservationService
            for receipt in result.sampler_receipts:
                ObservationService.consume_sampler_in(tx,receipt,collection_ref=collection_ref,
                    operation_id=operation_id,token=token)
        return result

    async def cleanup(self,scope,resource_ids,token,*,safety_guard=None):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            if safety_guard is not None: safety_guard(tx)
            for resource in resource_ids:
                record=tx.get('resources',resource)
                if not record or record['body']['scope']!=scope: raise PermissionError('resource not owned by scope')
        result=CleanupResult.model_validate(await self.gateway.cleanup(scope,tuple(resource_ids)))
        if set(result.resource_ids)!=set(resource_ids) or not set(result.remaining)<=set(resource_ids): raise Conflict('cleanup result resource mismatch')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            if safety_guard is not None: safety_guard(tx)
            tx.put('cleanup_records',f'{scope}:{canonical_hash(list(resource_ids))}',result.model_dump(mode='json'))
            for resource in resource_ids:
                old=tx.get('resources',resource)
                if old['body']['scope']!=scope: raise PermissionError('resource owner changed')
                tx.put('resources',resource,old['body']|{'status':'closed' if result.status=='complete' else 'unresolved'})
        return result
