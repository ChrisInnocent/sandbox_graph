"""Private data-plane credentials bound to scope, instance and generation.

No probing, command execution or FIRST-marker consumption occurs here. Public
records carry references only. Tombstones prohibit reusing a deleted identity.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile

from .secrets import SecretStore
from ..domain.models import canonical_hash

TOKEN_FIELDS=frozenset({'envdAccessToken','trafficAccessToken','routeKey','route_key'})


class InstanceTokenVault:
    def __init__(self,root,*,scope,generation):
        if not isinstance(scope,str) or not scope or type(generation) is not int or generation<1:
            raise ValueError('explicit scope and positive instance generation required')
        self.store=SecretStore(root);self.scope=scope;self.generation=generation
        self.private_scope='instance-'+canonical_hash(scope)
        self.index=self.store.root/('.instances-'+canonical_hash(scope)+'.json')
        self.lock=self.index.with_suffix('.lock')
        with self._locked() as state:
            if state is None:self._save({'scope':scope,'generation':generation,'instances':{}})
            elif state['generation']>generation:raise PermissionError('instance generation is obsolete')
            elif state['generation']<generation:self._save({'scope':scope,'generation':generation,'instances':{}})

    @contextmanager
    def _locked(self):
        fd=os.open(self.lock,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            if self.index.is_symlink():raise ValueError('instance index symlink refused')
            state=json.loads(self.store._read(self.index)) if self.index.exists() else None
            if state and state['scope']!=self.scope:raise PermissionError('instance scope changed')
            yield state
        finally:os.close(fd)

    def _save(self,state):
        fd,name=tempfile.mkstemp(prefix='.instance-stage-',dir=self.store.root)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(state,stream,sort_keys=True,separators=(',',':'),allow_nan=False)
                stream.flush();os.fsync(stream.fileno())
            os.replace(name,self.index)
            directory=os.open(self.store.root,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)

    def _current(self,state):
        if not state or state['generation']!=self.generation:raise PermissionError('instance generation changed')

    def _load(self,state,instance,*,observation=False):
        self._current(state)
        row=state['instances'].get(instance)
        if not row or (row['status']!='active' and not observation):raise KeyError(instance)
        body=json.loads(self.store.resolve(row['secret_ref'],scope=self.private_scope))
        if (body['scope'],body['generation'],body['instance'])!=(self.scope,self.generation,instance):
            raise PermissionError('instance credential binding changed')
        return ({key:value for key,value in body['credentials'].items() if key in {'routeKey','route_key'}}
            if observation else body['credentials'])

    def save(self,instance,credentials):
        if not isinstance(instance,str) or not instance or len(instance)>512:raise ValueError('bounded instance identity required')
        if (not isinstance(credentials,dict) or not credentials or set(credentials)-TOKEN_FIELDS
                or any(not isinstance(value,str) or not value or len(value)>16384 for value in credentials.values())):
            raise ValueError('only explicit bounded data-plane credentials may be stored')
        with self._locked() as state:
            self._current(state);old=state['instances'].get(instance)
            if old and old['status']!='active':raise PermissionError('deleted instance requires a new generation')
            merged=(self._load(state,instance) if old else {})|credentials
            body={'scope':self.scope,'generation':self.generation,'instance':instance,'credentials':merged}
            raw=json.dumps(body,sort_keys=True,separators=(',',':'))
            item=self.store.create_if_absent(self.private_scope,canonical_hash(instance),lambda:raw,
                version=canonical_hash(body))
            if self.store.resolve(item.ref,scope=self.private_scope)!=raw:raise ValueError('private credential digest collision')
            state['instances'][instance]={'status':'active','secret_ref':item.ref}
            self._save(state)
            return item.ref

    def load(self,instance):
        with self._locked() as state:return self._load(state,instance)

    def observe_route(self,instance):
        """Read-only control queries may need a route after asynchronous delete ACK."""
        with self._locked() as state:return self._load(state,instance,observation=True)

    def is_closed(self,instance):
        with self._locked() as state:
            self._current(state)
            return state['instances'].get(instance,{}).get('status')=='closed'

    def close(self,instance):
        with self._locked() as state:
            self._current(state)
            if instance in state['instances']:
                state['instances'][instance]['status']='closed';self._save(state)

    def owned(self):
        with self._locked() as state:
            self._current(state)
            return sorted(key for key,value in state['instances'].items() if value['status']=='active')
