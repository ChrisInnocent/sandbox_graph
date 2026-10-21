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
            if body['status']=='stopped':
                if body['last_seq']!=last_seq: raise Conflict('observation final sequence changed')
                return body
            sequences={r['body']['seq'] for r in tx.scan('observation_events') if r['body']['session_id']==session_id}
            if any(seq>last_seq for seq in sequences): raise Conflict('final sequence truncates recorded observation')
            missing=sorted(set(range(1,last_seq+1))-sequences)
            result=body|{'status':'stopped','last_seq':last_seq,'missing_sequences':missing,'data_status':'insufficient' if missing or body['interrupted'] else 'unverified'}
            tx.put('observations',session_id,result);return result
