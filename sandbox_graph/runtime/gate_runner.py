"""Run fixed controller-side cases and attest their observations.

Case callables are installed by the trusted controller, outside the candidate
interpreter and mounts. Candidate stdout/JUnit are observations only: they are
never accepted as a verdict. Unknown case execution is not silently rerun.
"""
from __future__ import annotations
import asyncio
import copy
from dataclasses import dataclass,field
import hashlib
import inspect
import json
from pathlib import Path

from ..domain.evolution import CandidateRevision,GateManifest,TARGET_FIELDS
from ..domain.models import canonical_hash
from ..services.agent_lifecycle import decode
from ..services.gate_policy import gate_plan
from ..services.gates import GateService


@dataclass(frozen=True)
class GateCase:
    version: str
    observe: object
    configuration: dict = field(default_factory=dict)

    def identity(self):
        source=inspect.getsourcefile(self.observe)
        if not source or not Path(source).is_file():raise ValueError('trusted case source is not inspectable')
        return {'version':self.version,'callable':self.observe.__module__+':'+self.observe.__qualname__,
            'module_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),'configuration_hash':canonical_hash(self.configuration)}


@dataclass(frozen=True)
class GateContext:
    case_id: str
    request_id: str
    candidate: CandidateRevision
    environment_ref: str
    environment_hash: str
    configuration: dict = field(default_factory=dict)
    isolation: object = None


class TrustedGateRunner:
    def __init__(self, repository, *, cases, producer_identity, producer_version,
                 runner_ref, runner_version, runner_bundle_hash, case_timeout_s=30., ttl_s=300.,context_factory=None):
        if not 0<case_timeout_s<=3600 or not 0<ttl_s<=3600:raise ValueError('finite gate time limits required')
        self.repository=repository;self.cases=dict(cases)
        self.producer_identity=producer_identity;self.producer_version=producer_version
        self.runner_ref=runner_ref;self.runner_version=runner_version;self.runner_bundle_hash=runner_bundle_hash
        self.case_timeout_s=case_timeout_s;self.ttl_s=ttl_s
        self.context_factory=context_factory

    @property
    def suite_hash(self):return canonical_hash({key:value.identity() for key,value in sorted(self.cases.items())})

    def _execution(self, tx, job_id, token):
        tx.assert_fence(token)
        row=tx.get('gate_executions',job_id)
        if not row:raise KeyError(job_id)
        job=row['body']
        if job['thread_id']!=token.thread_id or job['producer_identity']!=self.producer_identity:
            raise PermissionError('trusted runner does not own gate execution')
        producer=tx.get('trusted_gate_producers',self.producer_identity)
        if not producer:raise PermissionError('trusted runner is not registered')
        expected={'version':self.producer_version,'runner_ref':self.runner_ref,
            'runner_version':self.runner_version,'bundle_hash':self.runner_bundle_hash}
        if (producer['body'].get('revoked') is not False or producer['body'].get('expires_at',0)<=tx.now
                or any(producer['body'].get(key)!=value for key,value in expected.items())):
            raise PermissionError('trusted runner registration changed or expired')
        candidate=decode(CandidateRevision,tx.get('candidate_revisions',job['candidate_ref'])['body'])
        profile=tx.get('gate_profiles',job['profile_ref'])['body'];plan=gate_plan(candidate,profile)
        if plan['trusted_suite_hash']!=self.suite_hash:raise ValueError('trusted suite source digest changed')
        if set(plan['expected_case_ids'])-set(self.cases):raise ValueError('trusted collection missing required case IDs')
        return job,candidate,plan,producer['body']

    async def run(self, job_id, *, token):
        with self.repository.transaction() as tx:
            job,candidate,plan,producer=self._execution(tx,job_id,token)
            if job.get('manifest_ref'):return GateService.evaluate_in(tx,job['manifest_ref'],candidate)
            if job['status']!='running':raise PermissionError('gate execution is inactive')
        results={};logs={}
        for case_id in plan['expected_case_ids']:
            request_id='gate-case:'+canonical_hash({'job_id':job_id,'case_id':case_id,'plan':plan['plan_hash']})
            with self.repository.transaction() as tx:
                self._execution(tx,job_id,token)
                old=tx.get('gate_case_observations',request_id)
                if old:
                    result=old['body']['result']
                    results[case_id]=result;logs[case_id]=canonical_hash(result)
                    continue
                active=tx.get('gate_case_executions',request_id)
                if not active:
                    tx.put('gate_case_executions',request_id,{'job_id':job_id,'case_id':case_id,
                        'plan_hash':plan['plan_hash'],'status':'started','observed_at':tx.now},immutable=True)
            if active:
                result={'status':'blocked','observations':{'reason':'original_case_execution_unknown','request_id':request_id}}
            else:
                context=GateContext(case_id,request_id,candidate,job['environment_ref'],job['environment_hash'],
                    copy.deepcopy(self.cases[case_id].configuration))
                if self.context_factory is not None:context=self.context_factory(context,token)
                try:
                    result=await asyncio.wait_for(self.cases[case_id].observe(context),self.case_timeout_s)
                    if (not isinstance(result,dict) or set(result)!={'status','observations'}
                            or result['status'] not in {'pass','fail','blocked'}):
                        raise ValueError('trusted case returned malformed observation')
                    if len(json.dumps(result,allow_nan=False).encode())>1024*1024:
                        raise ValueError('case observation exceeds byte budget')
                except TimeoutError:
                    result={'status':'blocked','observations':{'reason':'case_timeout'}}
                except Exception as exc:
                    result={'status':'fail','observations':{'reason':'trusted_case_'+type(exc).__name__}}
            with self.repository.transaction() as tx:
                self._execution(tx,job_id,token)
                tx.put('gate_case_observations',request_id,{'job_id':job_id,'case_id':case_id,
                    'candidate_hash':candidate.candidate_hash,'result':result,'observed_at':tx.now},immutable=True)
                execution=tx.get('gate_case_executions',request_id)
                tx.put('gate_case_executions',request_id,execution['body']|{'status':'completed'},expected_revision=execution['revision'])
            results[case_id]=result;logs[case_id]=canonical_hash(result)
        with self.repository.transaction() as tx:
            job,current,current_plan,producer=self._execution(tx,job_id,token)
            if current!=candidate or current_plan!=plan:raise ValueError('gate inputs changed during execution')
            grouped=[]
            for gate,ids in plan['case_assignments'].items():
                statuses={results[case]['status'] for case in ids}
                status='fail' if 'fail' in statuses else 'blocked' if 'blocked' in statuses else 'pass'
                grouped.append({'schema_version':1,'gate_id':gate,'status':status,'case_ids':ids,
                    'log_hash':canonical_hash({case:logs[case] for case in ids})})
            manifest=decode(GateManifest,{'schema_version':1,'manifest_ref':'gate-manifest:'+job_id,
                'candidate_ref':candidate.candidate_ref,'candidate_hash':candidate.candidate_hash,
                **{key:getattr(candidate,key) for key in TARGET_FIELDS},
                'trusted_suite_hash':plan['trusted_suite_hash'],'policy_hash':plan['policy_hash'],
                'environment_ref':job['environment_ref'],'environment_hash':job['environment_hash'],
                'runner_ref':self.runner_ref,'runner_version':self.runner_version,'runner_bundle_hash':self.runner_bundle_hash,
                'required_gates':plan['required_gates'],'expected_case_ids':plan['expected_case_ids'],
                'actual_case_ids':sorted(results),'results':grouped,
                'case_results':[{'schema_version':1,'case_id':case,'status':result['status'],'log_hash':logs[case]} for case,result in results.items()],
                'log_hash':canonical_hash(logs),'producer_identity':self.producer_identity,'producer_version':self.producer_version,
                'observed_at':float(tx.now),'expires_at':float(min(tx.now+self.ttl_s,producer['expires_at'])),'clock':'wall'})
            GateService.record_result_in(tx,job_id,manifest,producer_identity=self.producer_identity,token=token)
            return GateService.evaluate_in(tx,manifest.manifest_ref,candidate)
