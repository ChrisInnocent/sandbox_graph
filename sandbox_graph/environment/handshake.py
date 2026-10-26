"""One remote step, one host lock, one short lived challenge and original journal.

The executor is installed locally with a pinned recipe catalog. The wire selects
a recipe hash and step identity, never an executable or shell fragment.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import math
import os
from pathlib import Path
import secrets
import time
from .contracts import ExecutionGrant
from .probes.inventory import parse_proc_stat
from ..domain.models import canonical_hash

ACTION='product.recipe.step.v1'

@dataclass
class PreparedStep:
    session: object
    data: dict
    step: object
    lock: int
    challenge: dict|None
    observation: dict|None
    deadline: float
    used: bool=False

    def execute(self,grant_data):
        session=self.session
        if self.used or self.challenge is None:raise PermissionError('challenge already used or original step already exists')
        self.used=True
        grant=ExecutionGrant.model_validate(grant_data)
        if (session.monotonic()>=self.deadline or grant.nonce!=self.challenge['nonce']
                or grant.operation_id!=self.data['operation_id'] or grant.step_id!=self.data['step_id']
                or grant.machine_id!=session.machine_id
                or abs((session.wall()-self.challenge['wall_time'])-(session.monotonic()-self.challenge['monotonic_time']))>session.max_skew
                or not session.verifier.authorize(ACTION,grant_data,self.data)):
            raise PermissionError('execution grant does not authorize current locked challenge')
        journal=session.executor.journal
        row=journal.accept(operation_id=self.data['operation_id'],action=ACTION,input_data=self.data)
        if row['state']!='accepted':return row
        identity=parse_proc_stat(Path('/proc/self/stat').read_text())
        journal.transition(self.data['operation_id'],state='started',process_identity={'pid':os.getpid(),'start_ticks':identity['start_ticks']})
        try:
            extra={}
            if self.step.kind in {'kuasar-tenant','kuasar-group-config'}:
                # The nonce is consumed once above. Subsequent internal calls
                # retain the lock and must remain inside that original grant.
                extra['still_authorized']=lambda:session.wall()<grant.not_after and session.monotonic()<self.deadline
            result=session.executor._step(self.step,self.data['expected_before'],self.lock,
                operation_id=self.data['operation_id'],**extra)
        except (ValueError,PermissionError) as exc:result={'status':'failed','reason':type(exc).__name__}
        except Exception as exc:result={'status':'unknown','reason':type(exc).__name__}
        # A completed primitive is execution evidence; group verification is separate.
        return journal.transition(self.data['operation_id'],state=result['status'],result=result)


class RecipeHandshake:
    def __init__(self,executor,verifier,*,machine_id,boot_id,wall=time.time,monotonic=time.monotonic,
                 challenge_seconds=15,max_skew=2):
        if not boot_id or not machine_id or not 0<challenge_seconds<=30 or not 0<=max_skew<=2:
            raise ValueError('bounded handshake and host boot identity required')
        self.executor=executor;self.verifier=verifier;self.machine_id=machine_id;self.boot_id=boot_id
        self.wall=wall;self.monotonic=monotonic;self.challenge_seconds=challenge_seconds;self.max_skew=max_skew

    @contextmanager
    def prepare(self,data):
        if (not isinstance(data,dict) or set(data)!={'operation_id','recipe','recipe_hash','step_id','expected_before'}
                or not isinstance(data['expected_before'],dict) or not isinstance(data['operation_id'],str)
                or not 0<len(data['operation_id'])<=128):raise ValueError('invalid remote step identity')
        recipe=data['recipe']
        if recipe not in self.executor.recipes or data['recipe_hash']!=self.executor.recipe_hash(recipe):
            raise ValueError('recipe differs from pinned host catalog')
        steps={step.step_id:step for step in self.executor.recipes[recipe]}
        if data['step_id'] not in steps:raise ValueError('unregistered recipe step')
        lock=os.open(self.executor.lock_dir/'host.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            row=self.executor.journal.get(data['operation_id'])
            if row:
                if row['action']!=ACTION or row['input_hash']!=canonical_hash({'action':ACTION,'input':data}):
                    raise ValueError('original remote operation input changed')
                if row['state']!='accepted':
                    yield PreparedStep(self,data,steps[data['step_id']],lock,None,row,0)
                    return
            wall=self.wall();mono=self.monotonic()
            if not all(math.isfinite(v) for v in (wall,mono)):raise ValueError('clock unavailable')
            challenge={'protocol':'environment-grant.v1','nonce':secrets.token_urlsafe(32),
                'machine_id':self.machine_id,'boot_id':self.boot_id,'input_hash':canonical_hash({'action':ACTION,'input':data}),
                'wall_time':wall,'monotonic_time':mono,'challenge_seconds':self.challenge_seconds}
            yield PreparedStep(self,data,steps[data['step_id']],lock,challenge,None,mono+self.challenge_seconds)
        finally:os.close(lock)
