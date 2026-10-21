"""Shared single-host source references and conservative, replay-safe cleanup.

The caller must register an authoritative usage inspector. Borrowed templates,
active/unknown consumers and missing usage evidence are retained. This local
ledger is not a distributed lock or a substitute for deployment ownership proof.
"""
from contextlib import contextmanager
import copy
import fcntl
import json
from pathlib import Path
import time
import uuid
import asyncio
from .runner import atomic_json, digest


class SourceLedger:
    def __init__(self, path, environment_ref):
        if not environment_ref:
            raise ValueError('source ledger environment identity required')
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.environment=environment_ref
        self._active_callbacks={}
        with self._transaction() as state:
            if state and state['environment_ref']!=environment_ref:
                raise ValueError('source ledger belongs to another environment')
            if not state:state.update(environment_ref=environment_ref,sources={})

    @contextmanager
    def _transaction(self):
        with self.path.with_suffix(self.path.suffix+'.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state=json.loads(self.path.read_text()) if self.path.exists() else {}
            before=copy.deepcopy(state)
            yield state
            if state!=before:atomic_json(self.path,state)

    def register(self, source_id, *, owner_work, creation_evidence_ref, borrowed=False):
        if not source_id or not owner_work or not creation_evidence_ref or type(borrowed) is not bool:
            raise ValueError('source ownership and evidence required')
        identity={'source_id':source_id,'owner_work':owner_work,
                  'creation_evidence_ref':creation_evidence_ref,'borrowed':borrowed}
        with self._transaction() as state:
            previous=state['sources'].get(source_id)
            if previous:
                if previous['identity']!=identity:raise ValueError('source ownership conflict')
            else:state['sources'][source_id]={'identity':identity,'consumers':{},'cleanup':None}

    def pin(self, source_id, consumer_ref):
        if not consumer_ref:raise ValueError('consumer identity required')
        with self._transaction() as state:
            row=state['sources'][source_id]
            if row['cleanup'] is not None:raise ValueError('source cleanup is pending or completed')
            old=row['consumers'].get(consumer_ref)
            if old and old['status']!='active':raise ValueError('released consumer identity cannot be reused')
            row['consumers'][consumer_ref]={'status':'active'}

    def release(self, source_id, consumer_ref, *, evidence_ref):
        if not evidence_ref:raise ValueError('consumer release evidence required')
        with self._transaction() as state:
            row=state['sources'][source_id]['consumers'][consumer_ref]
            released={'status':'released','evidence_ref':evidence_ref}
            if row['status']=='released' and row!=released:raise ValueError('consumer release proof conflict')
            row.clear();row.update(released)

    def snapshot(self):
        with self._transaction() as state:return copy.deepcopy(state)

    def _proof(self, source_id, proof, *, max_age_s, not_before_s=None):
        if type(max_age_s) not in (int,float) or not 0<max_age_s<float('inf'):
            raise ValueError('finite positive proof age required')
        if not isinstance(proof,dict):raise ValueError('source usage proof required')
        if (proof.get('source_id')!=source_id or proof.get('environment_ref')!=self.environment
                or proof.get('complete') is not True or not proof.get('evidence_ref')
                or type(proof.get('exists')) is not bool
                or not isinstance(proof.get('consumer_refs'),list)
                or any(not isinstance(x,str) or not x for x in proof['consumer_refs'])):
            raise ValueError('incomplete or foreign source usage proof')
        observed=proof.get('observed_wall_s')
        if type(observed) not in (int,float) or not 0<=time.time()-observed<=max_age_s:
            raise ValueError('source usage proof is stale')
        if not_before_s is not None and observed<not_before_s:
            raise ValueError('source proof predates the operation observation boundary')
        if not proof['exists'] and proof['consumer_refs']:
            raise ValueError('absent source has conflicting consumers')
        return proof

    async def _bounded_callback(self, source_id, operation_id, awaitable, timeout_s):
        """Timeout never waits for callback cancellation or replays its side effects."""
        callback_id=uuid.uuid4().hex
        with self._transaction() as state:
            row=state['sources'][source_id]['cleanup']
            if row['operation_id']!=operation_id:raise ValueError('source cleanup operation changed')
            row.update(callback_pending=True,callback_id=callback_id)
        task=asyncio.ensure_future(awaitable)
        self._active_callbacks[callback_id]=task
        def finished(done):
            self._active_callbacks.pop(callback_id,None)
            if not done.cancelled():done.exception()  # Retrieve late callback errors without claiming success.
            with self._transaction() as state:
                row=state['sources'][source_id]['cleanup']
                if row and row.get('operation_id')==operation_id and row.get('callback_id')==callback_id:
                    row.update(callback_pending=False,callback_finished_wall_s=time.time())
        task.add_done_callback(finished)
        return await asyncio.wait_for(asyncio.shield(task),timeout_s)

    async def cleanup(self, source_id, *, owner_work, inspector, deleter, timeout_s, max_age_s):
        if (type(timeout_s) not in (int,float) or type(max_age_s) not in (int,float)
                or not 0<timeout_s<float('inf') or not 0<max_age_s<float('inf')):
            raise ValueError('finite source cleanup deadlines required')
        with self._transaction() as state:
            row=state['sources'][source_id]
            if row['identity']['owner_work']!=owner_work:raise PermissionError('source belongs to another work')
            if row['identity']['borrowed']:return {'status':'retained','reason':'borrowed_source'}
            if any(c['status']!='released' for c in row['consumers'].values()):
                return {'status':'retained','reason':'consumer_reference_active'}
            old=row['cleanup']
            if old:
                if old['status']=='complete':return copy.deepcopy(old)
                return {'status':'unknown','reason':'source_delete_requires_reconciliation','operation_id':old['operation_id']}
            operation_id=digest([self.environment,source_id,owner_work,uuid.uuid4().hex])
            # Freeze admission before inspecting; another process cannot pin the source.
            row['cleanup']={'status':'inspecting','operation_id':operation_id,'started_wall_s':time.time()}
        started=time.monotonic()
        try:
            proof=self._proof(source_id,await self._bounded_callback(source_id,operation_id,inspector(source_id),timeout_s),max_age_s=max_age_s)
            if proof['consumer_refs']:
                with self._transaction() as state:state['sources'][source_id]['cleanup']=None
                return {'status':'retained','reason':'external_consumers_present','evidence_ref':proof['evidence_ref']}
            if not proof['exists']:
                result={'status':'complete','operation_id':operation_id,'evidence_ref':proof['evidence_ref'],'logical_release':'source_absent','physical_release':'unverified'}
            else:
                with self._transaction() as state:
                    state['sources'][source_id]['cleanup']={**state['sources'][source_id]['cleanup'],
                        'status':'submission_started','operation_id':operation_id,'submission_wall_s':time.time(),
                        'preflight_evidence_ref':proof['evidence_ref']}
                remaining=timeout_s-(time.monotonic()-started)
                if remaining<=0:raise TimeoutError()
                await self._bounded_callback(source_id,operation_id,deleter(source_id,remaining),remaining)
                delete_finished_wall_s=time.time()
                remaining=timeout_s-(time.monotonic()-started)
                if remaining<=0:raise TimeoutError()
                after=self._proof(source_id,await self._bounded_callback(source_id,operation_id,inspector(source_id),remaining),
                                  max_age_s=max_age_s,not_before_s=delete_finished_wall_s)
                if after['exists']:raise ValueError('source absence not independently observed')
                result={'status':'complete','operation_id':operation_id,'evidence_ref':after['evidence_ref'],'logical_release':'source_absent','physical_release':'unverified'}
        except BaseException as exc:
            with self._transaction() as state:
                row=state['sources'][source_id]
                row['cleanup']={**row['cleanup'],'status':'unknown','error_type':type(exc).__name__}
            if isinstance(exc,asyncio.CancelledError):raise
            if not isinstance(exc,Exception):raise
            return {'status':'unknown','operation_id':operation_id,'reason':'source_cleanup_outcome_unknown'}
        with self._transaction() as state:state['sources'][source_id]['cleanup']=result
        return result

    def reconcile_absent(self, source_id, *, owner_work, proof, max_age_s):
        proof=self._proof(source_id,proof,max_age_s=max_age_s)
        if proof['exists']:raise ValueError('unknown source deletion cannot be replayed from a presence observation')
        with self._transaction() as state:
            row=state['sources'][source_id]
            if row['identity']['owner_work']!=owner_work:raise PermissionError('foreign source')
            if row['cleanup'] is None:raise ValueError('no source cleanup intent')
            cleanup=row['cleanup']
            if cleanup['status']=='complete':return copy.deepcopy(cleanup)
            if cleanup['status']!='unknown':raise ValueError('source cleanup is still active')
            if cleanup.get('callback_id') in self._active_callbacks:
                raise ValueError('source cleanup callback is still active')
            if cleanup.get('callback_pending') and not (proof.get('operation_settled') is True
                    and proof.get('operation_id')==cleanup['operation_id']):
                raise ValueError('source callback settlement evidence required after interruption')
            self._proof(source_id,proof,max_age_s=max_age_s,
                        not_before_s=max(cleanup.get('started_wall_s',0),cleanup.get('submission_wall_s',0),
                                         cleanup.get('callback_finished_wall_s',0)))
            row['cleanup']={'status':'complete','operation_id':row['cleanup']['operation_id'],
                            'evidence_ref':proof['evidence_ref'],'logical_release':'source_absent','physical_release':'unverified'}
            return copy.deepcopy(row['cleanup'])
