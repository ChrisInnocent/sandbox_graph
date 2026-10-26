"""Operator controls over the same fenced lifecycle services and fixed gate suites."""
import argparse
import json
import math
import os
from pathlib import Path
from ..domain.models import canonical_hash
from ..domain.evolution import ChangeProposal,TargetBinding,CandidateRevision
from ..environment.cli import read_json
from ..persistence.repository import Conflict
from ..services.agent_lifecycle import LifecycleService,decode
from ..services.lifecycle_stop import LifecycleStopService

ROOT=Path(__file__).resolve().parents[2]
PROFILES=('development','persistent','isolation','release')


def rollback_command(repository,data):
    """One operator request, checked by the same publication transaction."""
    from contextlib import suppress
    from ..domain.evolution import RollbackRecord
    from ..services.rollback import RollbackService
    from ..persistence.repository import StaleOwner
    request=decode(RollbackRecord,data)
    with repository.transaction() as tx:
        token=tx.acquire_thread('rollback-control:'+canonical_hash(request.request_id),'rollback-cli',60)
    try:return RollbackService(repository).rollback(request,token=token)
    finally:
        with repository.transaction() as tx:
            with suppress(StaleOwner):tx.release_thread(token)


def run_profile(name,output):
    from ..testing.local_gate import run_gate
    if name not in PROFILES:raise ValueError('unregistered lifecycle gate profile')
    profile=read_json(ROOT/'config/gates'/f'lifecycle-{name}.json')
    return run_gate(ROOT,profile,output)


class LifecycleCommands:
    def __init__(self,repository,*,evolution=None):self.repository,self.evolution=repository,evolution

    def status(self,lifecycle_id):
        with self.repository.transaction() as tx:
            record=LifecycleService.load_in(tx,lifecycle_id)
            return {'lifecycle':record.model_dump(mode='json'),
                'decisions':[row['body'] for row in tx.scan('lifecycle_decisions') if row['body']['lifecycle_id']==lifecycle_id],
                'stops':[row['body'] for row in tx.scan('lifecycle_stop_status') if row['body'].get('lifecycle_id')==lifecycle_id],
                'candidates':[row['body'] for row in tx.scan('candidate_progress') if row['body']['lifecycle_id']==lifecycle_id]}

    def execute(self,action,command):
        if action not in {'request-repair','request-change','decision','cancel'}:raise ValueError('unsupported lifecycle command')
        required={'request_id','lifecycle_id','expected_revision','principal','authorization_ref','input'}
        if (not isinstance(command,dict) or set(command)!=required or not isinstance(command['input'],dict)
                or type(command['expected_revision']) is not int or command['expected_revision']<0
                or any(not isinstance(command[k],str) or not command[k] for k in required-{'input','expected_revision'})):
            raise ValueError('versioned identity, expected revision and explicit authority required')
        identity=canonical_hash({'action':action,'command':command});request=command['request_id'];token=None
        try:
            with self.repository.transaction() as tx:
                old=tx.get('lifecycle_control_commands',request)
                if old:
                    if old['body']['input_hash']!=identity:raise Conflict('lifecycle command request input changed')
                    return old['body']['result']
                life=LifecycleService.load_in(tx,command['lifecycle_id'])
                if life.revision!=command['expected_revision']:raise Conflict('lifecycle command revision changed')
                authority_hash=self._authorize_in(tx,action,command,life)
                if action=='cancel':
                    if set(command['input'])!={'reason'} or not isinstance(command['input']['reason'],str) or not command['input']['reason']:
                        raise ValueError('explicit cancellation reason required')
                    ref=LifecycleStopService.request_lifecycle_in(tx,request_id=request,record=life,reason=command['input']['reason'])
                    for proposal in tx.scan('change_proposals'):
                        if proposal['body']['proposal']['source_key']['lifecycle_id']!=life.key.lifecycle_id:continue
                        progress=tx.get('candidate_progress',proposal['key'])
                        tx.put('candidate_progress',proposal['key'],progress['body']|{'status':'cancelled'})
                        tx.put('outbox','candidate-cancel:'+proposal['key'],{'kind':'candidate_cancel','proposal_id':proposal['key']},immutable=True)
                    result={'status':'cancel_requested','stop_ref':ref,'cleanup':'pending_original_evidence'}
                    self._audit(tx,request,identity,command,result);return result
                token=tx.acquire_thread('lifecycle-control:'+identity,'operator:'+command['principal'],60)
            LifecycleService(self.repository).claim(command['lifecycle_id'],token=token)
            with self.repository.transaction() as tx:
                life=LifecycleService.load_in(tx,command['lifecycle_id'])
                if life.revision!=command['expected_revision']:raise Conflict('lifecycle command revision changed during claim')
                self._authorize_in(tx,action,command,life,expected_hash=authority_hash)
            data=command['input']
            if action=='request-change':
                if self.evolution is None:raise ValueError('registered candidate workspace is required')
                if not {'proposal','target'}<=set(data) or set(data)-{'proposal','target','revision_targets'}:
                    raise ValueError('frozen proposal and target required')
                proposal=decode(ChangeProposal,data['proposal']);target=decode(TargetBinding,data['target'])
                targets={}
                for revision,binding in data.get('revision_targets',{}).items():
                    if not isinstance(revision,str) or not revision.isdecimal() or not 1<=int(revision)<=1000:
                        raise ValueError('bounded explicit revision target required')
                    targets[int(revision)]=decode(TargetBinding,binding)
                if proposal.source_key.lifecycle_id!=command['lifecycle_id']:raise PermissionError('proposal belongs to another lifecycle')
                self.evolution.open(proposal,target=target,token=token)
                from .evolution_dispatch import EvolutionCoordinator
                with self.repository.transaction() as tx:
                    self._authorize_in(tx,action,command,life,expected_hash=authority_hash)
                    self.evolution.authorize_in(tx,proposal,token)
                    for revision,binding in targets.items():
                        self.evolution.bind_revision_target_in(tx,proposal.proposal_id,revision,binding,token=token)
                    job=EvolutionCoordinator._enqueue_in(tx,proposal,sequence=1)
                    result={'status':'candidate_queued','proposal_id':proposal.proposal_id,'job_ref':job}
                    self._audit(tx,request,identity,command,result);return result
            permitted={'route','evidence_refs','action_ref','proposal_ref'} if action=='decision' else {'evidence_refs','action_ref'}
            if set(data)-permitted or 'evidence_refs' not in data:raise ValueError('explicit decision contract required')
            route=data.get('route') if action=='decision' else 'known_action'
            with self.repository.transaction() as tx:
                self._authorize_in(tx,action,command,life,expected_hash=authority_hash)
                result=LifecycleService.record_decision_in(tx,command['lifecycle_id'],request_id=request,
                    expected_revision=command['expected_revision'],route=route,evidence_refs=tuple(data['evidence_refs']),
                    action_ref=data.get('action_ref'),proposal_ref=data.get('proposal_ref'),token=token)
                self._audit(tx,request,identity,command,result);return result
        finally:
            if token is not None:
                from contextlib import suppress
                from ..persistence.repository import StaleOwner
                with self.repository.transaction() as tx:
                    with suppress(StaleOwner):tx.release_thread(token)

    @staticmethod
    def _authorize_in(tx,action,command,life,expected_hash=None):
        row=tx.get('lifecycle_control_authorizations',command['authorization_ref']);auth=row['body'] if row else {}
        expected={'principal':command['principal'],'lifecycle_id':life.key.lifecycle_id,
            'run_ref':life.key.run_ref,'scope_ref':life.key.scope_ref,'graph_ref':life.graph_ref,'plan_ref':life.plan_ref}
        expiry=auth.get('expires_at')
        if (auth.get('revoked') is not False or action not in auth.get('actions',())
                or type(expiry) not in (float,int) or not math.isfinite(expiry) or expiry<=tx.now
                or any(auth.get(k)!=v for k,v in expected.items())
                or expected_hash is not None and canonical_hash(auth)!=expected_hash):
            raise PermissionError('current exact-scope lifecycle control authorization required')
        return canonical_hash(auth)

    @staticmethod
    def _audit(tx,request,identity,command,result):
        tx.put('lifecycle_control_commands',request,{'input_hash':identity,'principal':command['principal'],
            'authorization_ref':command['authorization_ref'],'lifecycle_id':command['lifecycle_id'],
            'result':result,'observed_at':tx.now},immutable=True)


def parser():
    root=argparse.ArgumentParser(prog='python -m sandbox_graph lifecycle')
    commands=root.add_subparsers(dest='command',required=True)
    status=commands.add_parser('status');status.add_argument('--lifecycle-id',required=True)
    for name in ('request-repair','request-change','decision','cancel'):
        command=commands.add_parser(name);command.add_argument('--input',type=Path,required=True)
        if name=='request-change':command.add_argument('--runtime',type=Path,required=True)
    rollback=commands.add_parser('rollback');rollback.add_argument('--input',type=Path,required=True)
    rollout=commands.add_parser('rollout');rollout.add_argument('--input',type=Path,required=True)
    gates=commands.add_parser('gates').add_subparsers(dest='gate_command',required=True)
    run=gates.add_parser('run');run.add_argument('--profile',choices=PROFILES,required=True);run.add_argument('--output',type=Path,required=True)
    show=gates.add_parser('show');show.add_argument('--manifest-ref',required=True)
    return root


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='gates' and args.gate_command=='run':result=run_profile(args.profile,args.output)
        else:
            from ..persistence.repository import PostgresRepository
            dsn=os.environ.get('SANDBOX_GRAPH_POSTGRES_DSN')
            if not dsn:raise ValueError('SANDBOX_GRAPH_POSTGRES_DSN is required; no fallback database')
            repository=PostgresRepository(dsn);evolution=None
            if args.command=='request-change':
                from .candidate_workspace import CandidateWorkspace
                from ..services.evolution import EvolutionService
                value=read_json(args.runtime)
                if set(value)!={'repository','candidate_root'}:raise ValueError('explicit candidate filesystem roots required')
                evolution=EvolutionService(repository,CandidateWorkspace(value['repository'],value['candidate_root']))
            service=LifecycleCommands(repository,evolution=evolution)
            if args.command=='status':result=service.status(args.lifecycle_id)
            elif args.command=='rollback':result=rollback_command(repository,read_json(args.input))
            elif args.command=='rollout':
                from ..services.lifecycle_rollout import LifecycleRolloutService
                result=LifecycleRolloutService(repository).configure(read_json(args.input))
            elif args.command=='gates':
                from ..services.gates import GateService
                with repository.transaction() as tx:
                    manifest=tx.get('gate_manifests',args.manifest_ref)
                    if not manifest:raise ValueError('registered gate manifest missing')
                    candidate=decode(CandidateRevision,tx.get('candidate_revisions',manifest['body']['candidate_ref'])['body'])
                    result=GateService.evaluate_in(tx,args.manifest_ref,candidate)
            else:result=service.execute(args.command,read_json(args.input))
        code={'pass':0,'fail':1,'blocked':2}.get(result.get('status'),0)
    except Exception as exc:
        from pydantic import ValidationError
        from ..environment.evidence import SecretRedactor
        reason='invalid_command_contract' if isinstance(exc,ValidationError) else (
            SecretRedactor().redact(str(exc))[:512] if isinstance(exc,(ValueError,PermissionError,Conflict))
            else 'backend_or_filesystem_unavailable')
        result={'status':'blocked','error':type(exc).__name__,'reason':reason};code=2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,default=str));return code
