"""Explicit single-host source ownership, consumer pins and collector cleanup.

Only an evidenced template endpoint is registered. Snapshot/image deletion is
deliberately unavailable. A delete acknowledgement never releases a consumer.
"""
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import time

from .runner import atomic_json, digest
from .source_lifecycle import SourceLedger


class ConfiguredSourceLifecycle:
    def __init__(self,session,binding):
        self.session=session;self.case=session.case;self.binding=dict(binding)
        self.folder=session.folder/'source_lifecycle';self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'bindings.json'
        self.ledger=SourceLedger(binding['ledger_path'],self.case.environment_ref)
        identity={'case_hash':self.case.digest(),'binding_hash':digest(binding)}
        with self._transaction() as state:
            if state and state['identity']!=identity:raise ValueError('source lifecycle binding changed')
            if not state:state.update(identity=identity,sources={},consumers={})

    @contextmanager
    def _transaction(self):
        with self.path.with_suffix('.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state=json.loads(self.path.read_text()) if self.path.exists() else {}
            before=digest(state)
            yield state
            if digest(state)!=before:atomic_json(self.path,state)

    async def own_source(self,source_id,source_kind,index):
        self.restore_owned(source_id,source_kind,index)

    def restore_owned(self,source_id,source_kind,index):
        if source_kind not in ('template','snapshot','image'):raise ValueError('explicit source kind required')
        evidence='work:'+self.case.digest()+':source:'+digest([source_id,index])
        self.ledger.register(source_id,owner_work=self.case.work_id,creation_evidence_ref=evidence)
        with self._transaction() as state:
            row={'kind':source_kind,'borrowed':False,'request_index':index,'evidence_ref':evidence}
            if source_id in state['sources'] and state['sources'][source_id]!=row:
                raise ValueError('source kind or operation identity changed')
            state['sources'][source_id]=row

    async def pin(self,source,consumer_key):
        sid=source['source_id'];kind=source['kind']
        with self._transaction() as state:
            if sid not in state['sources']:
                if sid!=self.case.source.id or kind!=self.case.source.kind:
                    raise ValueError('unregistered owned source cannot be borrowed implicitly')
                evidence='borrowed:'+self.case.source.digest
                if sid not in self.ledger.snapshot()['sources']:
                    self.ledger.register(sid,owner_work='borrowed:'+digest([self.case.environment_ref,sid]),
                        creation_evidence_ref=evidence,borrowed=True)
                state['sources'][sid]={'kind':kind,'borrowed':True,'evidence_ref':evidence}
            if state['sources'][sid]['kind']!=kind:raise ValueError('source kind mismatch')
            ref=self.case.work_id+':consumer:'+digest([sid,consumer_key])
            previous=state['consumers'].get(ref)
            if previous:
                if previous['source_id']!=sid or previous['key']!=consumer_key:raise ValueError('consumer changed')
                raise ValueError('consumer already reserved; reconcile without submitting again')
            self.ledger.pin(sid,ref)
            state['consumers'][ref]={'source_id':sid,'key':consumer_key,'sandbox_id':None,
                                    'pinned_wall_s':time.time()}
        return ref

    async def bind(self,source,sandbox_id,consumer_ref):
        if not isinstance(sandbox_id,str) or not sandbox_id:raise ValueError('known child identity required')
        with self._transaction() as state:
            row=state['consumers'][consumer_ref]
            if row['source_id']!=source['source_id']:raise ValueError('consumer source changed')
            if row['sandbox_id'] not in (None,sandbox_id):raise ValueError('consumer child changed')
            row['sandbox_id']=sandbox_id

    def _document(self):
        path=Path(self.binding['collector_path'])
        with path.open('rb') as stream:data=stream.read(self.binding['max_bytes']+1)
        if len(data)>self.binding['max_bytes']:raise ValueError('source collector exceeds bound')
        value=json.loads(data)
        expected={'environment_ref':self.case.environment_ref,'product':self.case.product,
                  'product_version':self.case.product_version,'authority_ref':self.binding['authority_ref']}
        if any(value.get(key)!=item for key,item in expected.items()):
            raise ValueError('source collector scope or authority mismatch')
        if not isinstance(value.get('sources'),dict) or not isinstance(value.get('consumers'),dict):
            raise ValueError('complete source and consumer observations required')
        checksum=hashlib.sha256(data).hexdigest()
        destination=self.folder/('collector-'+checksum+'.json')
        if not destination.exists():destination.write_bytes(data)
        return value

    def _valid_age(self,proof,not_before):
        observed=proof.get('observed_wall_s')
        return (type(observed) in (int,float) and observed>=not_before
                and 0<=time.time()-observed<=self.binding['max_age_s'])

    def _restore_settled(self,row):
        if not row['key'].startswith('restore:'):return True
        verifier=getattr(self.session,'fixture_restore',None)
        if verifier is None:return False
        operation=verifier.journal.state()['operations'].get(row['key'][len('restore:'):])
        return operation is not None and operation['status'] in ('success','failed','unsent')

    def _release_proven(self,source_id,document):
        source=document['sources'].get(source_id,{})
        with self._transaction() as state:
            rows=list(state['consumers'].items())
        ledger=self.ledger.snapshot()['sources'][source_id]
        for ref,row in rows:
            if row['source_id']!=source_id or not row['sandbox_id'] or not self._restore_settled(row):continue
            if ledger['consumers'][ref]['status']=='released':continue
            proof=document['consumers'].get(ref,{})
            if (proof.get('source_id')!=source_id or proof.get('sandbox_id')!=row['sandbox_id']
                    or proof.get('operation_settled') is not True or not proof.get('evidence_ref')
                    or not self._valid_age(proof,row['pinned_wall_s'])):continue
            absent=proof.get('sandbox_absent') is True
            zero=(source.get('complete') is True and source.get('consumer_refs')==[]
                  and source.get('source_id')==source_id
                  and source.get('environment_ref')==self.case.environment_ref
                  and source.get('evidence_ref') and self._valid_age(source,row['pinned_wall_s']))
            if absent or zero:self.ledger.release(source_id,ref,evidence_ref=proof['evidence_ref'])

    async def reconcile_consumers(self,timeout_s):
        if timeout_s<=0:return {'status':'incomplete','reason':'consumer_observation_deadline'}
        try:
            document=await asyncio.wait_for(asyncio.to_thread(self._document),min(timeout_s,self.binding['timeout_s']))
            with self._transaction() as state:sources=list(state['sources'])
            for sid in sources:self._release_proven(sid,document)
        except (OSError,ValueError,KeyError,TypeError):
            return {'status':'incomplete','reason':'consumer_observation_unavailable'}
        return {'status':'incomplete' if self.residuals() else 'completed'}

    async def cleanup(self,source_id,timeout_s):
        if type(timeout_s) not in (int,float) or not 0<timeout_s<float('inf'):
            raise ValueError('finite source cleanup budget required')
        deadline=time.monotonic()+min(timeout_s,self.binding['timeout_s'])
        async def document_before_deadline():
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            return await asyncio.wait_for(asyncio.to_thread(self._document),remaining)
        with self._transaction() as state:local=state['sources'].get(source_id)
        if local is None:return {'status':'blocked','reason':'source_ownership_unregistered'}
        if local['borrowed']:
            await self.reconcile_consumers(deadline-time.monotonic())
            return {'status':'retained','reason':'borrowed_source'}
        expected={'environment_ref':self.case.environment_ref,'product':self.case.product,
            'product_version':self.case.product_version,'source_kind':local['kind'],
            'endpoint':'/templates/{source_id}'}
        capability=next((item for item in self.binding['delete_capabilities']
            if all(item.get(k)==v for k,v in expected.items()) and item.get('evidence_ref')),None)
        if local['kind']!='template' or capability is None:
            return {'status':'blocked','reason':'source_delete_capability_unverified'}
        try:
            document=await document_before_deadline()
            self._release_proven(source_id,document)
        except (OSError,ValueError,KeyError,TypeError):
            return {'status':'blocked','reason':'source_usage_evidence_unavailable'}
        async def inspector(sid):
            document=await document_before_deadline()
            proof=document['sources'][sid]
            if proof.get('kind')!=local['kind']:raise ValueError('source proof kind mismatch')
            return proof
        async def deleter(sid,remaining):
            return await asyncio.to_thread(self.session.adapter.delete_source,sid,remaining)
        remaining=deadline-time.monotonic()
        if remaining<=0:return {'status':'blocked','reason':'source_cleanup_deadline'}
        result=await self.ledger.cleanup(source_id,owner_work=self.case.work_id,inspector=inspector,
            deleter=deleter,timeout_s=remaining,max_age_s=self.binding['max_age_s'])
        if result['status']=='unknown':
            try:
                proof=await inspector(source_id)
                result=self.ledger.reconcile_absent(source_id,owner_work=self.case.work_id,
                    proof=proof,max_age_s=self.binding['max_age_s'])
            except (OSError,ValueError,KeyError,TypeError):pass
        return result

    def export(self):
        # A new shared-ledger fact changes this byte snapshot and invalidates an old seal.
        atomic_json(self.folder/'shared-ledger.json',self.ledger.snapshot())

    def residuals(self):
        ledger=self.ledger.snapshot()
        with self._transaction() as state:refs=set(state['consumers'])
        return [{'source_id':sid,'consumer_ref':ref,'status':row['status']}
            for sid,source in ledger['sources'].items() for ref,row in source['consumers'].items()
            if ref in refs and row['status']!='released']
