"""Production candidate verifier assembled from installed controller observers.

Observers are explicitly pinned operator code outside both candidate and source
trees. They own verdicts; candidate programs can only supply observations through
the isolated execution handle. Loading a model-supplied module is not supported.
"""
import copy
from dataclasses import replace
import hashlib
import inspect
from pathlib import Path
import os
import sys
import types

from ..domain.models import canonical_hash
from ..services.agent_lifecycle import LifecycleService
from ..services.evolution import EvolutionService
from .gate_runner import GateCase,TrustedGateRunner


def load_cases(service,rows):
    if not isinstance(rows,dict) or not 1<=len(rows)<=10000:raise ValueError('bounded fixed trusted case catalog required')
    cases={}
    for case_id,value in rows.items():
        if isinstance(value,dict) and set(value)=={'observer','version','configuration'}:
            if value['observer']!='controller-pytest.v1':raise ValueError('unregistered built-in trusted observer')
            suite=Path(value['configuration']['suite_root']).resolve()
            if any(suite==root or root in suite.parents or suite in root.parents
                    for root in (service.workspace.root,service.workspace.repository)):
                raise PermissionError('controller suite must be outside source and candidate roots')
            from ..testing.controller_pytest import observe
            cases[case_id]=GateCase(value['version'],observe,copy.deepcopy(value['configuration']))
            continue
        if not isinstance(value,dict) or set(value)!={'path','sha256','callable','version','configuration'}:
            raise ValueError('digest-pinned observer registration required')
        path=Path(value['path'])
        if not path.is_absolute() or path.resolve()!=path or not path.is_file():raise ValueError('canonical observer module required')
        if any(path==root or root in path.parents for root in (service.workspace.root,service.workspace.repository)):
            raise PermissionError('trusted observer must be outside source and candidate roots')
        info=path.stat()
        if info.st_uid not in {0,os.geteuid()} or info.st_mode&0o022:raise PermissionError('observer code is writable by another identity')
        with path.open('rb') as stream:raw=stream.read(1024*1024+1)
        if len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=value['sha256']:raise ValueError('observer module digest differs')
        name='sandbox_graph_controller_'+canonical_hash({'path':str(path),'hash':value['sha256']})
        module=sys.modules.get(name)
        if module is None:
            module=types.ModuleType(name);module.__file__=str(path)
            exec(compile(raw,str(path),'exec'),module.__dict__)
            sys.modules[name]=module
        observe=getattr(module,value['callable'],None)
        if not inspect.iscoroutinefunction(observe) or observe.__module__!=name:
            raise ValueError('observer must be an async function defined in the pinned module')
        cases[case_id]=GateCase(value['version'],observe,copy.deepcopy(value['configuration']))
    return cases


class BoundIsolation:
    def __init__(self,service,backend,backend_id,context,token):
        self.service,self.repository,self.backend=service,service.repository,backend
        self.backend_id,self.context,self.token=backend_id,context,token

    async def execute(self,command_ref,stdin=b'',*,timeout_s=30):
        candidate=self.context.candidate
        request_id='gate-isolation:'+canonical_hash({'gate_request':self.context.request_id,'command_ref':command_ref})
        identity=self.backend.identity(request_id,candidate.workspace_ref,command_ref,stdin,
            timeout_s=timeout_s,readonly=True)
        body={'proposal_id':candidate.proposal_id,'candidate_ref':candidate.candidate_ref,
            'candidate_hash':candidate.candidate_hash,'backend_id':self.backend_id,
            'request_id':request_id,'gate_request_ref':self.context.request_id,'identity':identity}
        def guard():
            with self.repository.transaction() as tx:
                _,proposal,_=self.service._inputs_in(tx,candidate.proposal_id)
                self.service.authorize_in(tx,proposal,self.token)
                if tx.get('candidate_progress',candidate.proposal_id)['body']['status']=='cancelled':
                    raise PermissionError('candidate execution cancelled')
        guard()
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            tx.put('candidate_isolations',request_id,body,immutable=True)
        guard()
        result=await self.backend.execute(request_id,candidate.workspace_ref,command_ref,stdin,
            timeout_s=timeout_s,readonly=True,before_start=guard)
        # Only independent original-process observation closes this identity.
        observed=await self.backend.reconcile(request_id)
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            if observed.get('cleanup')=='complete':
                tx.put('candidate_disposition_proofs',request_id,{
                    'isolation_ref':request_id,'isolation_hash':canonical_hash(body),
                    'children_stopped':True,'observation':observed,'producer':'candidate_gate.v1'},immutable=True)
        if observed.get('cleanup')!='complete':
            return dict(result,status='unknown',cleanup='unknown',reason='independent_cleanup_unconfirmed')
        return result


class CandidateGateRuntime:
    def __init__(self,service,config,*,backend=None,backend_id=None):
        required={'profile_ref','environment_ref','environment_hash','producer_identity','producer_version',
            'runner_ref','runner_version','runner_bundle_hash','cases'}
        if not required<=set(config) or set(config)-required-{'case_timeout_s','ttl_s'}:
            raise ValueError('explicit candidate gate runtime required')
        self.service,self.repository=service,service.repository;self.config=copy.deepcopy(config)
        self.backend,self.backend_id=backend,backend_id
        def context_factory(context,token):
            return replace(context,isolation=BoundIsolation(service,backend,backend_id,context,token)) if backend else context
        self.runner=TrustedGateRunner(self.repository,cases=load_cases(service,config['cases']),context_factory=context_factory,
            **{key:value for key,value in config.items() if key not in {'profile_ref','environment_ref','environment_hash','cases'}})

    async def __call__(self,candidate,token):
        self.service.workspace.verify_revision(candidate)
        ref='candidate-gate:'+canonical_hash({'candidate_hash':candidate.candidate_hash,'profile':self.config['profile_ref'],
            'environment_ref':self.config['environment_ref'],'environment_hash':self.config['environment_hash']})
        with self.repository.transaction() as tx:
            _,proposal,_=self.service._inputs_in(tx,candidate.proposal_id)
            self.service.authorize_in(tx,proposal,token);EvolutionService.verify_targets_in(tx,candidate)
            old=tx.get('gate_executions',ref)
            body={'thread_id':token.thread_id,'candidate_ref':candidate.candidate_ref,'profile_ref':self.config['profile_ref'],
                'producer_identity':self.config['producer_identity'],'environment_ref':self.config['environment_ref'],
                'environment_hash':self.config['environment_hash'],'status':'running'}
            if old:
                # Ownership has already transferred through LifecycleService.
                # Started case IDs remain unresolved, never replayed as new work.
                if any(old['body'].get(k)!=v for k,v in body.items() if k not in {'thread_id','status'}):
                    raise PermissionError('original gate binding differs')
                tx.put('gate_executions',ref,old['body']|{'thread_id':token.thread_id},expected_revision=old['revision'])
            else:tx.put('gate_executions',ref,body,immutable=True)
        result=await self.runner.run(ref,token=token)
        self.service.workspace.verify_revision(candidate)
        return result
