"""Versioned opt-in extension profiles and durable, scope-bound action execution.

Callbacks are explicitly registered adapters, never arbitrary shell strings.
Unknown side effects require evidence reconciliation before any replay/cleanup.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


@dataclass(frozen=True)
class ExtensionStep:
    action: str
    stage: str
    parameters: dict = field(default_factory=dict)
    compensates: tuple = ()


@dataclass(frozen=True)
class ExtensionProfile:
    kind: str
    version: str
    profile_id: str
    seed: int
    max_steps: int
    max_duration_s: float
    scope: dict
    steps: tuple
    workload: dict | None = None
    reset_mode: str | None = None
    initial_state: str = 'absent'
    clock_domain: str = 'local-monotonic'
    compensation_timeout_s: float = 30
    audit_every_steps: int = 10

    def __post_init__(self):
        if self.kind not in {'E01','E02','E03','E04','E05','E06'} or not self.version or not self.profile_id:
            raise ValueError('known kind, profile ID and version required')
        if type(self.seed) is not int or type(self.max_steps) is not int or self.max_steps<=0:
            raise ValueError('integer seed and positive finite step budget required')
        if type(self.audit_every_steps) is not int or self.audit_every_steps<=0:
            raise ValueError('positive audit interval required')
        for value in (self.max_duration_s,self.compensation_timeout_s):
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
                raise ValueError('finite positive time budget required')
        if (not self.scope.get('environment') or not self.scope.get('resource_ids')
                or not self.clock_domain or len(self.steps)>self.max_steps):
            raise ValueError('explicit owned scope, clock domain and bounded steps required')


class KnownActionFailure(Exception):
    """Adapter has definitive outcome evidence, unlike an ambiguous connection loss."""
    def __init__(self,reason='action_failed',*,applied=False,result=None):
        super().__init__(reason);self.reason=reason;self.applied=applied;self.result=result or {}


class ActionRegistry:
    def __init__(self):
        self.actions={}

    def register(self,name,callback,*,side_effect=True,compensate=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or name in self.actions:
            raise ValueError('unique registered action name required')
        if callback is not None and not callable(callback):
            raise ValueError('action must be an adapter callback')
        if compensate is not None and not callable(compensate):
            raise ValueError('compensation must be a callback')
        self.actions[name]={'callback':callback,'side_effect':side_effect,'compensate':compensate}


TRANSITIONS={('absent','create'):'running',('running','pause'):'paused',
             ('paused','resume'):'running',('running','reset'):'running',
             ('running','delete'):'deleted',('paused','delete'):'deleted',
             ('running','probe'):'running',('running','audit'):'running',('paused','audit'):'paused'}
STAGES={'E01':['workload'],'E02':['baseline','mutate','reset','verify'],
        'E04':['pressure','work','release','recovery'],
        'E05':['baseline','interference','measure','remove','recovery'],
        'E06':['inject','observe','recover','verify']}


class ExtensionRunner:
    def __init__(self,profile,registry,state_path,*,clock=time.monotonic):
        self.profile=profile;self.registry=registry;self.path=Path(state_path);self.clock=clock
        self.fingerprint=hashlib.sha256(_json(asdict(profile)).encode()).hexdigest()
        self.state=None

    def _preflight(self):
        p=self.profile
        if hashlib.sha256(_json(asdict(p)).encode()).hexdigest()!=self.fingerprint:
            raise ValueError('profile changed after runner construction')
        for index,step in enumerate(p.steps):
            if step.action not in self.registry.actions:
                raise ValueError('unregistered extension action')
            action=self.registry.actions[step.action]
            if action['callback'] is None:
                return 'missing_adapter'
            if set(step.parameters.get('targets',p.scope['resource_ids']))-set(p.scope['resource_ids']):
                raise ValueError('action target outside owned scope')
            if step.parameters.get('environment',p.scope['environment'])!=p.scope['environment']:
                raise ValueError('action environment outside scope')
            if p.kind in {'E04','E05','E06'} and action['side_effect'] and action['compensate'] is None:
                raise ValueError('side-effect action requires compensation')
            for prior in step.compensates:
                if type(prior) is not int or prior<0 or prior>=index:
                    raise ValueError('compensation must reference a prior step')
        stages=[s.stage for s in p.steps]
        if p.kind in STAGES:
            # Repeated stages permit bounded pressure stairs and measurements.
            collapsed=[s for i,s in enumerate(stages) if i==0 or stages[i-1]!=s]
            if collapsed!=STAGES[p.kind]:
                raise ValueError('profile stage sequence does not match its versioned kind')
        if p.kind=='E01':
            required={'work_units','input_sha256','output_sha256'}
            if not p.workload or not required<=p.workload.keys():
                return 'missing_workload'
            if type(p.workload['work_units']) is not int or p.workload['work_units']<=0:
                raise ValueError('fixed positive workload required')
            if any(not re.fullmatch('[0-9a-f]{64}',p.workload[k]) for k in ('input_sha256','output_sha256')):
                raise ValueError('workload SHA256 must be defined')
        if p.kind=='E02' and p.reset_mode not in {'reset','recreate'}:
            return 'missing_reset_contract'
        if p.kind=='E03':
            state=p.initial_state
            since_audit=0
            for step in p.steps:
                if (state,step.stage) not in TRANSITIONS:
                    raise ValueError('illegal lifecycle transition')
                since_audit=0 if step.stage=='audit' else since_audit+1
                if since_audit>p.audit_every_steps:
                    raise ValueError('periodic lifecycle audit missing')
                state=TRANSITIONS[state,step.stage]
        return None

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_suffix(self.path.suffix+'.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if self.path.exists():
                self.state=json.loads(self.path.read_text())
                if self.state['fingerprint']!=self.fingerprint:
                    raise ValueError('persisted profile/scope/version differs')
            else:
                self.state={'schema_version':'extension_state.v1','fingerprint':self.fingerprint,
                            'profile_id':self.profile.profile_id,'kind':self.profile.kind,'status':'ready',
                            'steps':{},'compensations':[],'segments':[],'budget_used':0,
                            'lifecycle':self.profile.initial_state,'deadline_s':self.clock()+self.profile.max_duration_s}
            yield

    def _save(self):
        fd,name=tempfile.mkstemp(prefix='.extension-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w') as handle:
                handle.write(_json(self.state));handle.flush();os.fsync(handle.fileno())
            os.replace(name,self.path)
            directory=os.open(self.path.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)

    def _result(self):
        return json.loads(_json(self.state))

    def _context(self,index,*,compensation=False):
        deadline=self.clock()+self.profile.compensation_timeout_s if compensation else self.state['deadline_s']
        return {'profile_id':self.profile.profile_id,'version':self.profile.version,'seed':self.profile.seed,
                'scope':json.loads(_json(self.profile.scope)),'clock_domain':self.profile.clock_domain,
                'step_id':f'{self.profile.profile_id}:{index}',
                'action_id':f'{self.profile.profile_id}:{"comp" if compensation else "action"}:{index}',
                'action':self.profile.steps[index].action,'deadline_s':deadline,
                'remaining_s':max(0,deadline-self.clock()),'compensation':compensation}

    def _invoke(self, callback, parameters, context):
        """Bound caller waiting; an uncooperative remote action remains unknown.

        A daemon thread cannot cancel an external effect. Its private late result
        is discarded and the durable action stays isolated until reconciled.
        """
        from queue import Queue, Empty
        from threading import Thread
        replies = Queue(maxsize=1)
        def call():
            try: replies.put((True,callback(parameters,context)))
            except BaseException as exc: replies.put((False,exc))
        if context['remaining_s'] <= 0:
            raise TimeoutError('extension action deadline exhausted')
        Thread(target=call,daemon=True,name='bounded-extension-action').start()
        try: success,value = replies.get(timeout=context['remaining_s'])
        except Empty as exc: raise TimeoutError('extension action outcome unknown') from exc
        if context['compensation'] and self.clock() > context['deadline_s']:
            raise TimeoutError('extension action completed after deadline')
        if not success: raise value
        return value

    def run(self,*,selected=False):
        if not selected:
            return {'status':'not_selected','kind':self.profile.kind,'executed_steps':0}
        blocked=self._preflight()
        if blocked:
            return {'status':'blocked','reason':blocked,'kind':self.profile.kind,'executed_steps':0}
        with self._locked():
            if self.state['status'] in {'complete','compensated','failed','isolated'}:
                return self._result()
            # A crash between intent and receipt is an unknown side effect.
            if any(row['status']=='started' for row in self.state['steps'].values()):
                self.state.update(status='isolated',reason='interrupted_action_unknown');self._save()
                return self._result()
            self.state['segments'].append({'start_s':self.clock(),'clock_domain':self.profile.clock_domain})
            self.state['status']='running';self._save()
            for index,step in enumerate(self.profile.steps):
                key=str(index)
                if self.state['steps'].get(key,{}).get('status')=='complete':
                    continue
                if self.clock()>=self.state['deadline_s'] or self.state['budget_used']>=self.profile.max_steps:
                    self.state.update(status='failed',reason='budget_exhausted');self._save()
                    self._compensate();return self._result()
                action=self.registry.actions[step.action]
                self.state['steps'][key]={'status':'started','stage':step.stage,'action':step.action}
                self.state['budget_used']+=1
                if action['side_effect'] and action['compensate'] is not None:
                    self.state['compensations'].append({'index':index,'status':'pending','outcome':'unknown'})
                self._save()
                try:
                    result=self._invoke(action['callback'],json.loads(_json(step.parameters)),self._context(index))
                    if not isinstance(result,dict):
                        raise ValueError('action must return structured evidence')
                    _json(result)
                except KnownActionFailure as exc:
                    self.state['steps'][key].update(status='failed',result=exc.result,applied=exc.applied)
                    self._set_outcome(index,'applied' if exc.applied else 'not_applied')
                    self.state.update(status='failed',reason=exc.reason);self._save()
                    self._compensate();return self._result()
                except Exception as exc:
                    self.state['steps'][key].update(status='unknown',error_type=type(exc).__name__)
                    self.state.update(status='isolated',reason='action_outcome_unknown');self._save()
                    return self._result()
                self.state['steps'][key].update(status='complete',result=result)
                self._set_outcome(index,'applied')
                if self.profile.kind=='E03':
                    self.state['lifecycle']=TRANSITIONS[self.state['lifecycle'],step.stage]
                for prior in step.compensates:
                    for item in self.state['compensations']:
                        if item['index']==prior:item['status']='complete'
                self._save()
                if self.clock()>self.state['deadline_s']:
                    self.state.update(status='failed',reason='deadline_exceeded');self._save()
                    self._compensate();return self._result()
            reason=self._assertions()
            self.state.update(status='failed' if reason else 'complete')
            if reason:self.state['reason']=reason
            if self.profile.kind=='E02':self.state['group']=self.profile.reset_mode
            self.state['segments'][-1]['end_s']=self.clock();self._save()
            self._compensate()
            return self._result()

    def _set_outcome(self,index,outcome):
        for item in self.state['compensations']:
            if item['index']==index:item['outcome']=outcome

    def _assertions(self):
        rows=[self.state['steps'][str(i)]['result'] for i in range(len(self.profile.steps))]
        if self.profile.kind=='E01':
            if any(any(row.get(k)!=v for k,v in self.profile.workload.items()) for row in rows):
                return 'workload_evidence_mismatch'
        if self.profile.kind=='E02':
            baseline,verified=rows[0],rows[-1]
            if not baseline.get('content_sha256') or baseline.get('content_sha256')!=verified.get('content_sha256'):
                return 'reset_content_mismatch'
            before,after=baseline.get('sandbox_id'),verified.get('sandbox_id')
            if not before or not after or ((before==after)!=(self.profile.reset_mode=='reset')):
                return 'reset_identity_mismatch'
        if self.profile.kind=='E03':
            for step,row in zip(self.profile.steps,rows):
                if step.stage=='audit' and row.get('healthy') is not True:
                    return 'lifecycle_audit_failed'
        return None

    def _compensate(self):
        stack=self.state['compensations']
        if any(x['status'] in {'started','unknown'} or (x['status']=='pending' and x['outcome']=='unknown') for x in stack):
            self.state.update(status='isolated',reason='compensation_requires_reconciliation');self._save();return
        for item in reversed(stack):
            if item['status']=='complete':continue
            if item['outcome']=='not_applied':
                item['status']='complete';self._save();continue
            index=item['index'];step=self.profile.steps[index]
            item['status']='started';self._save()
            try:
                result=self._invoke(self.registry.actions[step.action]['compensate'],
                                    json.loads(_json(step.parameters)),self._context(index,compensation=True))
                _json(result)
            except Exception as exc:
                item.update(status='unknown',error_type=type(exc).__name__)
                self.state.update(status='isolated',reason='compensation_outcome_unknown');self._save();return
            item.update(status='complete',result=result);self._save()

    def recover(self):
        blocked=self._preflight()
        if blocked:return {'status':'blocked','reason':blocked}
        with self._locked():
            if any(r['status'] in {'started','unknown'} for r in self.state['steps'].values()):
                self.state.update(status='isolated',reason='action_requires_reconciliation');self._save();return self._result()
            self._compensate()
            if all(x['status']=='complete' for x in self.state['compensations']):
                self.state['status']='compensated';self._save()
            return self._result()

    def reconcile_step(self,index,*,outcome,evidence,result=None):
        if outcome not in {'applied','not_applied'} or not evidence:
            raise ValueError('definitive outcome and external evidence required')
        with self._locked():
            row=self.state['steps'][str(index)]
            if row['status'] not in {'started','unknown'}:
                raise ValueError('only unknown actions can be reconciled')
            row.update(status='complete' if outcome=='applied' else 'not_applied',result=result or {},reconciliation=evidence)
            self._set_outcome(index,outcome)
            if outcome=='applied' and self.profile.kind=='E03':
                self.state['lifecycle']=TRANSITIONS[self.state['lifecycle'],self.profile.steps[index].stage]
            self.state.update(status='ready');self.state.pop('reason',None);self._save()

    def reconcile_compensation(self,index,*,applied,evidence):
        if type(applied) is not bool or not evidence:
            raise ValueError('definitive compensation evidence required')
        with self._locked():
            item=next(x for x in self.state['compensations'] if x['index']==index)
            if item['status'] not in {'started','unknown'}:
                raise ValueError('only unknown compensation may be reconciled')
            item.update(status='complete' if applied else 'pending',reconciliation=evidence)
            self.state['status']='ready';self._save()
