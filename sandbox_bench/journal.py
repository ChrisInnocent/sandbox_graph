"""单宿主离线runner持久账本；正式多worker通过编排仓储注入同协议。

文件锁＋原子替换保护预算和提交意图。它不宣称提供远端API幂等性或
替代PostgreSQL fencing；submission_started后不能无条件重复提交。
"""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager


class FileJournal:
    backend = 'local-file-single-host'

    def __init__(self, folder, work_id, *, child_budget, parent_budget):
        if not work_id or any(type(x) is not int or x <= 0 for x in (child_budget,parent_budget)):
            raise ValueError('work identity and positive finite budgets required')
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'journal.json';self.lock_path=self.folder/'journal.lock'
        self.work_id=work_id
        with self._transaction() as state:
            if not state:
                state.update(work_id=work_id,child_budget=child_budget,parent_budget=parent_budget,
                             child_reserved=0,parent_reserved=0,operations={},resources={},receipts={},events=[])
            elif (state['work_id'],state['child_budget'],state['parent_budget']) != (work_id,child_budget,parent_budget):
                raise ValueError('journal identity/budget mismatch')

    @contextmanager
    def _transaction(self):
        with self.lock_path.open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:
                state=json.loads(self.path.read_text()) if self.path.exists() else {}
                before=copy.deepcopy(state)
                yield state
                if state!=before:
                    raw=json.dumps(state,sort_keys=True,separators=(',',':'),allow_nan=False)
                    fd,name=tempfile.mkstemp(prefix='.journal-',dir=self.folder)
                    try:
                        with os.fdopen(fd,'w') as f:
                            f.write(raw);f.flush();os.fsync(f.fileno())
                        os.replace(name,self.path)
                        directory=os.open(self.folder,os.O_RDONLY)
                        try:os.fsync(directory)
                        finally:os.close(directory)
                    finally:
                        if os.path.exists(name):os.unlink(name)
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    def _event(self,s,kind,**data):
        seq=len(s['events'])+1
        s['events'].append({'event_id':f'{self.work_id}:{seq}','seq':seq,'kind':kind,**data})

    def reserve_sync(self,request_index,child_weight=1):
        if type(request_index) is not int or request_index<0 or type(child_weight) is not int or child_weight<=0:
            raise ValueError('invalid request slot')
        token=hashlib.sha256(f'{self.work_id}:request:{request_index}'.encode()).hexdigest()
        with self._transaction() as s:
            if token in s['operations']:
                if s['operations'][token]['weight']!=child_weight:raise ValueError('slot input conflict')
                return token
            if s['child_reserved']+child_weight>s['child_budget'] or s['parent_reserved']+1>s['parent_budget']:
                raise ValueError('request budget exhausted')
            s['child_reserved']+=child_weight;s['parent_reserved']+=1
            s['operations'][token]={'operation_id':token,'index':request_index,'weight':child_weight,'status':'reserved'}
            self._event(s,'reserved',operation_id=token)
        return token

    def submission_started_sync(self,token):
        with self._transaction() as s:
            if s.get('cancelled'):raise ValueError('session cancelled; no new submission')
            op=s['operations'][token]
            if op['status']!='reserved':raise ValueError('already submitted: reconcile before any replay')
            op['status']='submission_started';self._event(s,'submission_started',operation_id=token)

    def terminal_sync(self,token,result):
        allowed={'success','failed','timeout','unknown','cancelled','unsent'}
        if result.get('status') not in allowed:raise ValueError('invalid terminal status')
        with self._transaction() as s:
            op=s['operations'][token]
            if 'result' in op:
                if op['result']!=result:raise ValueError('conflicting terminal evidence')
                return
            op['result']=copy.deepcopy(result);op['status']=result['status']
            self._event(s,'terminal',operation_id=token,result=result)

    async def reserve(self,request_index,child_weight=1):
        return self.reserve_sync(request_index,child_weight)

    async def submission_started(self,token):self.submission_started_sync(token)

    async def record_terminal(self,token,result):
        from dataclasses import asdict,is_dataclass
        value=asdict(result) if is_dataclass(result) else dict(result)
        self.terminal_sync(token,value)

    async def record_child_terminal(self, token, child_index, result, completed_at=None):
        from dataclasses import asdict,is_dataclass
        import math
        value=asdict(result) if is_dataclass(result) else dict(result)
        if value.get('status') not in ('success','failed','timeout','unknown','unsent'):
            raise ValueError('invalid child terminal status')
        if type(child_index) is not int or child_index<0:raise ValueError('invalid child index')
        if completed_at is not None and (type(completed_at) not in (int,float) or not math.isfinite(completed_at)):
            raise ValueError('invalid child observation time')
        record={'result':value,'completed_at':completed_at}
        with self._transaction() as state:
            op=state['operations'][token]
            if child_index>=op['weight']:raise ValueError('child outside reserved parent weight')
            children=op.setdefault('children',{});key=str(child_index)
            if key in children:
                if children[key]!=record:raise ValueError('conflicting child terminal evidence')
                return
            if op['status'] not in ('submission_started',):raise ValueError('child terminal without active parent')
            children[key]=record
            self._event(state,'child_terminal',operation_id=token,child_index=child_index,**record)

    def state(self):
        with self._transaction() as s:return copy.deepcopy(s)

    def operation(self,token):return self.state()['operations'][token]

    def collect(self,last_seq=0):return [x for x in self.state()['events'] if x['seq']>last_seq]

    def own(self,kind,resource_id,attributes):
        key=f'{kind}:{resource_id}'
        row={'kind':kind,'resource_id':resource_id,'owner':self.work_id,**attributes}
        if row['owner']!=self.work_id or not row.get('environment'):raise ValueError('ownership identity missing')
        with self._transaction() as s:
            if key in s['resources'] and s['resources'][key]!=row:raise ValueError('resource identity conflict')
            if key not in s['resources']:
                s['resources'][key]=row;self._event(s,'resource_owned',resource_ref=key)

    def owned(self,environment):
        rows=list(self.state()['resources'].values())
        if any(x['environment']!=environment for x in rows):raise ValueError('environment mismatch')
        return rows

    def receipt(self,step_id,input_hash,result=None):
        with self._transaction() as s:
            existing=s['receipts'].get(step_id)
            if existing:
                if existing['input_hash']!=input_hash:raise ValueError('step input conflict')
                if result is not None and existing['result']!=result:raise ValueError('step result conflict')
                return copy.deepcopy(existing['result'])
            if result is not None:
                s['receipts'][step_id]={'input_hash':input_hash,'result':copy.deepcopy(result)}
                self._event(s,'receipt',step_id=step_id)
            return copy.deepcopy(result)

    def begin_step(self, step_id, input_hash):
        with self._transaction() as s:
            steps=s.setdefault('steps',{})
            if step_id in steps:
                if steps[step_id]['input_hash']!=input_hash:raise ValueError('step input conflict')
                return False
            steps[step_id]={'input_hash':input_hash,'status':'started'}
            self._event(s,'step_started',step_id=step_id)
            return True

    def audit(self, kind, **data):
        with self._transaction() as s:self._event(s,kind,**data)

    def cancel(self, reason):
        with self._transaction() as s:
            if not s.get('cancelled'):
                s['cancelled']=True
                self._event(s,'cancel_requested',reason=reason)

    def stage_event(self, event, *, max_submissions):
        identity={k:event.get(k) for k in ('request_index','stage','child_index')}
        if type(identity['request_index']) is not int or not identity['stage']:
            raise ValueError('stage request identity missing')
        key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        phase=event.get('phase')
        with self._transaction() as state:
            stages=state.setdefault('stages',{})
            if phase=='intent':
                if state.get('cancelled') and not (event.get('compensation') is True and identity['stage']=='source_resume'):
                    raise ValueError('session cancelled')
                if key in stages:raise ValueError('stage already submitted; reconcile required')
                if len(stages)>=max_submissions:raise ValueError('stage HTTP budget exhausted')
                stages[key]={'identity':identity,'phase':'intent'}
            elif phase=='receipt':
                if key not in stages:raise ValueError('stage receipt lacks submission intent')
                if stages[key]['phase']=='receipt' and stages[key].get('receipt')!=event:
                    raise ValueError('stage result conflict')
                stages[key].update(phase='receipt',receipt=event)
            elif phase!='observed':raise ValueError('unknown stage event')
            self._event(state,'stage_'+phase,stage_ref=key,evidence=event)
