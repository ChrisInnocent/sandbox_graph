"""Separate observation operation identity and discontinuous sampling segments.

Stores compact observation-event references; raw measurements belong in artifacts.
"""
from ..persistence.repository import Conflict

class ObservationService:
    def __init__(self,repository): self.repository=repository
    def start(self,session_id,scope,*,operation_id,clock_epoch,deadline):
        with self.repository.transaction() as tx:
            if deadline<=tx.now: raise Conflict('observation deadline already expired')
            existing=tx.get('observations',session_id)
            if existing:
                b=existing['body']
                if (b['scope'],b['operation_id'],b['deadline'])!=(scope,operation_id,deadline): raise Conflict('observation identity changed')
                return b
            record={'session_id':session_id,'scope':scope,'operation_id':operation_id,'clock_epoch':clock_epoch,'deadline':deadline,'segments':[clock_epoch],'interrupted':False,'status':'running'}
            tx.put('observations',session_id,record,immutable=True);return record
    def append(self,session_id,*,seq,clock_epoch,value):
        if seq<1: raise ValueError('positive sequence required')
        with self.repository.transaction() as tx:
            row=tx.get('observations',session_id)
            if not row: raise KeyError(session_id)
            body=row['body']
            if body['status']!='running' or body['deadline']<=tx.now: raise Conflict('observation is no longer authorized')
            if body['clock_epoch']!=clock_epoch: raise Conflict('cross-restart monotonic sample')
            tx.put('observation_events',f'{session_id}:{seq:020}',{'session_id':session_id,'seq':seq,'clock_epoch':clock_epoch,'value':value},immutable=True)
    def restart(self,session_id,*,clock_epoch):
        with self.repository.transaction() as tx:
            row=tx.get('observations',session_id)
            if not row: raise KeyError(session_id)
            body=row['body']
            if body['status']!='running' or body['deadline']<=tx.now: raise Conflict('observation cannot restart')
            if clock_epoch in body['segments']: raise Conflict('restart requires new process clock epoch')
            tx.put('observations',session_id,body|{'clock_epoch':clock_epoch,'segments':body['segments']+[clock_epoch],'interrupted':True})
    def stop(self,session_id,*,last_seq):
        with self.repository.transaction() as tx:
            row=tx.get('observations',session_id)
            if not row: raise KeyError(session_id)
            body=row['body']
            if body['status']=='stopped' and 'last_seq' in body:
                if body['last_seq']!=last_seq: raise Conflict('observation final sequence changed')
                return body
            sequences={r['body']['seq'] for r in tx.scan('observation_events') if r['body']['session_id']==session_id}
            if any(seq>last_seq for seq in sequences): raise Conflict('final sequence truncates recorded observation')
            missing=sorted(set(range(1,last_seq+1))-sequences)
            result=body|{'status':'stopped','last_seq':last_seq,'missing_sequences':missing,'data_status':'insufficient' if missing or body['interrupted'] or body.get('data_status')=='insufficient' else 'unverified'}
            tx.put('observations',session_id,result);return result

    @staticmethod
    def consume_sampler_in(tx, receipt, *, collection_ref, operation_id, token, validate_only=False):
        """Only OperationService calls this after its configured adapter verifies I/O.

        The immutable collection and original operation are the authority chain;
        stop() above deliberately never creates this chain.
        """
        from ..domain.models import canonical_hash
        binding=receipt['binding']; evidence=receipt['evidence']
        original=tx.get('operations',binding['operation_id'])
        current=tx.get('operations',operation_id)
        work=tx.get('work_instances',binding['scope'])
        expected=(original or {}).get('body',{}).get('execution_binding')
        if (not current or not work or not expected
                or binding!={**expected,'operation_id':binding['operation_id']}
                or (binding.get('sampler_session_id') is not None and evidence['session_id']!=binding['sampler_session_id'])
                or receipt['collection_operation_id']!=operation_id
                or current['body']['scope']!=binding['scope']
                or current['body']['run_id']!=binding['run_id']
                or work['body'].get('key',{}).get('measurement_generation',1)!=binding['measurement_generation']
                or any(work['body'].get(k)!=binding[k] for k in
                       ('run_id','group_id','plan_ref','graph_ref','contract_ref'))):
            raise Conflict('sampler receipt original operation/scope/generation differs')
        from ..services.lifecycle_safety import LifecycleSafetyService
        LifecycleSafetyService.assert_work_in(tx,binding['scope'],token)
        if validate_only: return
        ref='sampler-receipt:'+canonical_hash(receipt)
        proof={'schema_version':1,'producer':'operation_service','producer_version':'1',
               'receipt':receipt,'collection_ref':collection_ref,'operation_id':operation_id}
        if not tx.get('sampler_receipts',ref):
            tx.put('sampler_receipts',ref,proof,immutable=True)
        session_id=evidence['session_id']
        old=tx.get('observations',session_id)
        if old and (old['body'].get('operation_id')!=binding['operation_id']
                    or old['body'].get('scope')!=binding['scope']):
            raise Conflict('sampler session reused by another operation')
        # A later collection can re-read the original running receipt. It cannot
        # reopen a proven stopped session or overwrite missing/interrupted data.
        if old and old['body'].get('physical_stop_ref'):
            return
        stopped=evidence['state'] in {'stopped','not_started'}
        body=(old or {}).get('body',{}) | {
            'session_id':session_id,'scope':binding['scope'],'run_id':binding['run_id'],
            'operation_id':binding['operation_id'],'measurement_generation':binding['measurement_generation'],
            'clock_epoch':evidence['clock_epoch'],
            'segments':old['body'].get('segments',[evidence['session_id']]) if old else [evidence['session_id']],
            'interrupted':evidence['interrupted'] or bool(old and old['body'].get('interrupted')),'status':'stopped' if stopped else 'running',
            'data_status':'insufficient' if evidence['has_gaps'] or (old and (old['body'].get('interrupted')
                or old['body'].get('missing_sequences') or old['body'].get('data_status')=='insufficient')) else 'unverified',
            'sampler_start_ref':old['body'].get('sampler_start_ref',ref) if old else ref}
        if stopped:
            body['physical_stop_ref']=ref
            body['physical_state']=evidence['state']
        tx.put('observations',session_id,body)

    @staticmethod
    def closed_in(tx, row):
        from ..domain.models import canonical_hash
        body=row['body']
        ref=body.get('physical_stop_ref')
        proof=tx.get('sampler_receipts',ref) if ref else None
        if not proof:
            return False
        proof=proof['body']; receipt=proof.get('receipt',{})
        evidence=receipt.get('evidence',{}); binding=receipt.get('binding',{})
        collection=tx.get('collections',proof.get('collection_ref',''))
        original=tx.get('operations',binding.get('operation_id',''))
        return bool(proof.get('producer')=='operation_service' and proof.get('producer_version')=='1'
            and ref=='sampler-receipt:'+canonical_hash(receipt)
            and collection and receipt in collection['body'].get('sampler_receipts',[])
            and original and binding=={**original['body'].get('execution_binding',{}),
                                      'operation_id':body.get('operation_id')}
            and binding.get('scope')==body.get('scope') and binding.get('run_id')==body.get('run_id')
            and evidence.get('session_id')==row['key'] and body.get('status')=='stopped'
            and evidence.get('state') in {'stopped','not_started'}
            and evidence.get('executor_exit')==('joined' if evidence.get('state')=='stopped' else 'not_started')
            and evidence.get('capture_submitted')==evidence.get('capture_completed')
            and evidence.get('loop_exit') in {'completed','cancelled','not_started'}
            and evidence.get('close_exit')==('completed' if evidence.get('state')=='stopped' else 'not_started'))

    @classmethod
    def sampler_status_in(cls,tx,rows,*,empty='not_started'):
        if not rows:
            return empty
        if all(cls.closed_in(tx,row) for row in rows):
            return 'not_started' if all(r['body'].get('physical_state')=='not_started' for r in rows) else 'stopped'
        return 'running' if any(r['body'].get('status')=='running' for r in rows) else 'unknown'
