"""Durable candidate revisions over an authorized, immutable input snapshot.

Filesystem work happens between two fenced transactions. A lost worker leaves
the original operation available for reconciliation; it never replays edits.
This service accepts structured edits only, not shell commands or model gates.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

from ..domain.evolution import ChangeProposal, CandidateRevision, TargetBinding, TARGET_FIELDS
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from .agent_lifecycle import LifecycleService, decode
from .lifecycle_budgets import LifecycleBudgetService


class EvolutionService:
    def __init__(self, repository, workspace):
        self.repository, self.workspace = repository, workspace

    @staticmethod
    def verify_targets_in(tx, target):
        for name, kind in [('plan','plans'),('contract','contracts'),('auth','execution_authorizations')]:
            row=tx.get(kind,getattr(target,'target_'+name+'_ref'))
            if not row or canonical_hash(row['body'])!=getattr(target,'target_'+name+'_hash'):
                raise ValueError('frozen target '+name+' is missing or changed')
            if name=='auth' and (row['revision']!=target.target_auth_revision
                    or row['body'].get('revoked') is not False or row['body'].get('expires_at',0)<=tx.now):
                raise PermissionError('successor execution authorization invalid')
        auth=tx.get('execution_authorizations',target.target_auth_ref)['body']
        plan=tx.get('plans',target.target_plan_ref)['body']
        expected={'plan_ref':target.target_plan_ref,'plan_hash':target.target_plan_hash,
            'contract_ref':target.target_contract_ref,'contract_hash':target.target_contract_hash}
        if isinstance(target,CandidateRevision) and plan.get('graph_ref')!=target.graph_ref:
            raise PermissionError('candidate graph differs from frozen target plan')
        groups={case.get('group_id') for case in plan.get('cases',())}
        if (not auth.get('principal') or 'run.execute' not in auth.get('permissions',())
                or any(auth.get(k)!=v for k,v in expected.items())
                or not groups or None in groups or not groups<=set(auth.get('authorized_groups',()))
                or plan.get('stage') not in auth.get('stages',())):
            raise PermissionError('successor execution permission does not cover frozen plan/contract/group/stage scope')

    @staticmethod
    def authorize_in(tx, proposal, token, *, new_work=True):
        LifecycleService.assert_owner_in(tx,proposal.source_key.lifecycle_id,token)
        life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
        if life.key!=proposal.source_key or life.graph_ref!=proposal.base_graph_ref:
            raise PermissionError('proposal source differs from lifecycle')
        if not new_work:return life
        from .lifecycle_rollout import LifecycleRolloutService
        LifecycleRolloutService.assert_enabled_in(tx,life,'candidate')
        feature=tx.get('evolution_features',life.key.run_ref)
        if not feature or feature['body'].get('candidate_enabled') is not True:
            raise PermissionError('candidate evolution is disabled')
        if tx.now>=life.deadline or life.is_terminal or life.terminal_intent is not None or life.phase=='blocked':
            raise PermissionError('lifecycle does not admit candidate work')
        gate=tx.get('dispatch_gates',life.key.run_ref)
        if gate and not gate['body']['open']:raise PermissionError('run dispatch is closed')
        row=tx.get('evolution_authorizations',proposal.scope_auth)
        auth=row['body'] if row else {}
        expected={'lifecycle_id':life.key.lifecycle_id,'run_ref':life.key.run_ref,'scope_ref':life.key.scope_ref}
        if (auth.get('revoked') is not False or auth.get('expires_at',0)<=tx.now
                or any(auth.get(k)!=v for k,v in expected.items())):
            raise PermissionError('candidate scope authorization invalid')
        for path in proposal.allowed_paths:
            if not any(path==prefix or path.startswith(prefix+'/') for prefix in auth.get('allowed_paths',[])):
                raise PermissionError('candidate path scope is not authorized')
        for field in ('node_scope','contract_scope'):
            if not set(getattr(proposal,field))<=set(auth.get(field,[])):
                raise PermissionError('candidate '+field+' is not authorized')
        if life.key.node_id not in proposal.node_scope:raise PermissionError('failure node outside proposal scope')
        LifecycleService._evidence_in(tx,life,proposal.failure_evidence_refs)
        return life

    def open(self, proposal, *, target, token):
        with self.repository.transaction() as tx:
            inputs=self.register_in(tx,proposal,target=target,token=token)
        self.workspace.create(proposal.proposal_id,inputs['base_commit'],allowed_paths=proposal.allowed_paths)
        return self.status(proposal.proposal_id)

    @classmethod
    def register_in(cls,tx,proposal,*,target,token):
        if not isinstance(proposal,ChangeProposal) or not isinstance(target,TargetBinding):
            raise ValueError('typed proposal and target required')
        life=cls.authorize_in(tx,proposal,token)
        cls.verify_targets_in(tx,target)
        if target.target_contract_ref!=life.contract_ref and target.target_contract_ref not in proposal.contract_scope:
            raise PermissionError('successor contract outside authorized proposal scope')
        base=tx.get('code_bundles',proposal.base_code_ref)
        if not base or not base['body'].get('base_commit'):raise ValueError('registered base commit missing')
        inputs={'proposal':proposal.model_dump(mode='json'),'target':target.model_dump(mode='json'),
            'base_commit':base['body']['base_commit']}
        old=tx.get('change_proposals',proposal.proposal_id)
        if old and old['body']!=inputs:raise Conflict('proposal input snapshot changed')
        if not old:
            cls._begin_in(tx,proposal,token)
        tx.put('change_proposals',proposal.proposal_id,inputs,immutable=True)
        if not tx.get('candidate_progress',proposal.proposal_id):
            tx.put('candidate_progress',proposal.proposal_id,{'status':'building','candidate_refs':[],
                'active_operation':None,'lifecycle_id':proposal.source_key.lifecycle_id},immutable=True)
        return inputs

    @classmethod
    def _begin_in(cls,tx,proposal,token):
        from .lifecycle_safety import LifecycleSafetyService
        from ..domain.agent_lifecycle import TransitionGuards
        life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
        if life.phase not in {'draining','diagnosing','candidate_building'}:
            raise Conflict('lifecycle cannot begin candidate work in current phase')
        lease=tx.get('work_execution_leases',LifecycleSafetyService.scope_in(tx,life.key.scope_ref,token))
        LifecycleSafetyService.acquire_work_in(tx,life.key.scope_ref,token,purpose='diagnosis')
        proof=LifecycleSafetyService.produce_disposition_in(tx,life.key.scope_ref,token,ttl=min(60.,life.deadline-tx.now))
        LifecycleSafetyService.verify_disposition_in(tx,proof,life.key.scope_ref,token)
        tx.put('lifecycle_dispositions',life.key.lifecycle_id,{'receipt_ref':proof})
        LifecycleService.bind_evidence_in(tx,life,proof,kind='resource_disposition')
        guards=LifecycleService.guards_in(tx,life,None)
        if life.phase=='draining':life=cls._event_in(tx,proposal,'disposition_confirmed',proof,token,guards)
        if life.phase=='diagnosing':
            snapshot=LifecycleBudgetService.snapshot_in(tx,life.key.lifecycle_id)
            budget=all(row['spent']['candidate_revisions']+row['reserved']['candidate_revisions']<row['limits']['candidate_revisions']
                for row in snapshot.values())
            cls._event_in(tx,proposal,'needs_code',proof,token,guards.model_copy(update={
                'authority_valid':True,'budget_available':budget,'new_evidence':True}))
        if lease is None:LifecycleSafetyService.release_work_in(tx,life.key.scope_ref,token)

    @staticmethod
    def _event_in(tx,proposal,kind,evidence,token,guards,**event_fields):
        from ..domain.agent_lifecycle import LifecycleEvent,transition_lifecycle
        from .lifecycle_safety import LifecycleSafetyService
        LifecycleService.assert_owner_in(tx,proposal.source_key.lifecycle_id,token)
        life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
        previous=tx.get('work_execution_leases',LifecycleSafetyService.scope_in(tx,life.key.scope_ref,token))
        LifecycleSafetyService.acquire_work_in(tx,life.key.scope_ref,token,purpose='diagnosis')
        LifecycleSafetyService.assert_work_in(tx,life.key.scope_ref,token)
        guards=guards.model_copy(update={'work_lease_valid':True})
        if not tx.get('lifecycle_evidence_bindings',LifecycleService.evidence_binding_key(life.key.lifecycle_id,evidence)):
            LifecycleService.bind_evidence_in(tx,life,evidence,kind='candidate_evolution')
        event=LifecycleEvent(schema_version=1,event_id='evolution-event:'+canonical_hash({
            'proposal':proposal.proposal_id,'kind':kind,'evidence':evidence,'revision':life.revision}),kind=kind,
            source='candidate_evolution.v1',expected_revision=life.revision,evidence_refs=(evidence,),observed_at=float(tx.now),**event_fields)
        updated=LifecycleService.persist_transition_in(tx,life,event,transition_lifecycle(life,event,guards))
        if previous is None:LifecycleSafetyService.release_work_in(tx,life.key.scope_ref,token)
        return updated

    @classmethod
    def gate_observed_in(cls,tx,candidate,manifest_ref,token):
        from .gates import GateService
        from ..domain.agent_lifecycle import TransitionGuards
        _,proposal,_=cls._inputs_in(tx,candidate.proposal_id)
        life=cls.authorize_in(tx,proposal,token)
        checked=GateService.evaluate_in(tx,manifest_ref,candidate)
        if life.phase!='candidate_verifying':return life
        budget=all(row['spent']['candidate_revisions']+row['reserved']['candidate_revisions']<row['limits']['candidate_revisions']
            for row in LifecycleBudgetService.snapshot_in(tx,life.key.lifecycle_id).values())
        kind='tests_pass' if checked['status']=='pass' else 'tests_fail' if checked['status']=='fail' and budget else 'environment_missing' if checked['status']=='blocked' else 'budget_exhausted'
        return cls._event_in(tx,proposal,kind,manifest_ref,token,TransitionGuards(schema_version=1,
            proof_valid=True,budget_available=budget,effects_in_flight=False))

    @staticmethod
    def _inputs_in(tx, proposal_id):
        row=tx.get('change_proposals',proposal_id)
        if not row:raise KeyError(proposal_id)
        return row['body'],decode(ChangeProposal,row['body']['proposal']),decode(TargetBinding,row['body']['target'])

    @classmethod
    def revision_target_in(cls,tx,proposal_id,revision):
        inputs,proposal,initial=cls._inputs_in(tx,proposal_id)
        row=tx.get('candidate_revision_targets',proposal_id+':'+str(revision))
        if row:target=decode(TargetBinding,row['body']['target'])
        elif revision==1:target=initial
        else:raise PermissionError('candidate revision budget requires a frozen target binding')
        cls.verify_targets_in(tx,target)
        plan=tx.get('plans',target.target_plan_ref)['body']
        graph=plan.get('graph_ref')
        if not isinstance(graph,str) or not graph or graph==proposal.base_graph_ref:
            raise PermissionError('candidate target plan requires a new graph version')
        if target.target_contract_ref not in {LifecycleService.load_in(tx,proposal.source_key.lifecycle_id).contract_ref,*proposal.contract_scope}:
            raise PermissionError('revision target contract outside proposal scope')
        return target,graph

    def bind_revision_target(self,proposal_id,revision,target,*,token):
        with self.repository.transaction() as tx:
            return self.bind_revision_target_in(tx,proposal_id,revision,target,token=token)

    @classmethod
    def bind_revision_target_in(cls,tx,proposal_id,revision,target,*,token):
        if type(revision) is not int or revision<1 or not isinstance(target,TargetBinding):
            raise ValueError('positive revision and typed frozen target required')
        _,proposal,initial=cls._inputs_in(tx,proposal_id)
        cls.authorize_in(tx,proposal,token);cls.verify_targets_in(tx,target)
        if revision==1 and target!=initial:raise Conflict('initial target is already frozen')
        key=proposal_id+':'+str(revision);body={'target':target.model_dump(mode='json')}
        old=tx.get('candidate_revision_targets',key)
        if old:
            if old['body']!=body:raise Conflict('candidate revision target is already frozen')
            return
        progress=tx.get('candidate_progress',proposal_id)['body']
        if progress['active_operation'] or revision<=len(progress['candidate_refs']):
            raise Conflict('cannot change target of a started candidate revision')
        tx.put('candidate_revision_targets',key,body,immutable=True)
        cls.revision_target_in(tx,proposal_id,revision)

    def edit_revision(self, proposal_id, *, request_id, edits, graph_ref, token):
        command={'proposal_id':proposal_id,'edits':edits,'graph_ref':graph_ref}
        with self.repository.transaction() as tx:
            inputs,proposal,target=self._inputs_in(tx,proposal_id)
            self.authorize_in(tx,proposal,token)
            self.verify_targets_in(tx,target)
            old=tx.get('candidate_operations',request_id)
            if old:
                if old['body']['command']!=command:raise Conflict('candidate request ID input conflict')
                if old['body']['status']=='completed':
                    return decode(CandidateRevision,tx.get('candidate_revisions',old['body']['candidate_ref'])['body'])
                raise Conflict('original candidate operation requires reconciliation')
            progress=tx.get('candidate_progress',proposal_id)
            if progress['body']['status']=='cancelled':raise PermissionError('candidate cancelled')
            if progress['body']['active_operation']:raise Conflict('candidate operation already in progress')
            self.workspace.validate_edits(proposal_id,edits)
            revision=len(progress['body']['candidate_refs'])+1
            target,expected_graph=self.revision_target_in(tx,proposal_id,revision)
            if graph_ref!=expected_graph:raise PermissionError('candidate graph differs from frozen target plan')
            reservation='candidate-revision:'+request_id
            LifecycleBudgetService.reserve_in(tx,request_id=reservation,lifecycle_id=proposal.source_key.lifecycle_id,
                amounts={'candidate_revisions':1},input_ref=canonical_hash(command))
            tx.put('candidate_operations',request_id,{'command':command,'revision':revision,
                'reservation':reservation,'status':'started'},immutable=True)
            tx.put('candidate_progress',proposal_id,progress['body']|{'active_operation':request_id},
                expected_revision=progress['revision'])
        self.workspace.edit(proposal_id,edits)
        return self.reconcile_revision(request_id,token=token)

    def reconcile_revision(self, request_id, *, token):
        with self.repository.transaction() as tx:
            row=tx.get('candidate_operations',request_id)
            if not row:raise KeyError(request_id)
            op=row['body'];command=op['command'];proposal_id=command['proposal_id']
            inputs,proposal,_=self._inputs_in(tx,proposal_id)
            target,expected_graph=self.revision_target_in(tx,proposal_id,op['revision'])
            if command['graph_ref']!=expected_graph:raise PermissionError('original candidate target graph differs')
            self.authorize_in(tx,proposal,token)
            self.verify_targets_in(tx,target)
            if op['status']=='completed':return decode(CandidateRevision,tx.get('candidate_revisions',op['candidate_ref'])['body'])
            progress=tx.get('candidate_progress',proposal_id)
            if progress['body']['status']=='cancelled':raise PermissionError('candidate cancelled')
            if progress['body']['active_operation']!=request_id:raise Conflict('candidate operation changed')
        # Continue only the original registered intent under its exclusive writer lock.
        # Every path must be exactly before/after; foreign bytes remain unknown.
        self.workspace.resume_edits(proposal_id,command['edits'])
        folder=self.workspace._folder(proposal_id)
        for edit in command['edits']:
            path=folder/'work'/edit['path']
            content=edit['content']
            if content is None:
                if path.exists():raise Conflict('candidate deletion effect unknown; reconciliation required')
            elif (path.is_symlink() or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest()!=hashlib.sha256(content.encode()).hexdigest()):
                raise Conflict('candidate edit effect unknown; reconciliation required')
        meta=json.loads((folder/'metadata.json').read_text())
        revision=op['revision']
        if len(meta['revisions'])>=revision:
            frozen=self.workspace.verify(meta['revisions'][revision-1]['record'])
        else:frozen=self.workspace.freeze(proposal_id,revision=revision,graph_ref=command['graph_ref'])
        candidate=decode(CandidateRevision,{'schema_version':1,**frozen,
            'candidate_ref':proposal_id+'/revision/'+str(revision),'proposal_id':proposal_id,
            'budget_ref':proposal.source_key.lineage_budget_ref,**{key:getattr(target,key) for key in TARGET_FIELDS}})
        with self.repository.transaction() as tx:
            self.authorize_in(tx,proposal,token)
            self.verify_targets_in(tx,target)
            auth=tx.get('evolution_authorizations',proposal.scope_auth)['body']
            if int(candidate.risk[1:])>int(auth.get('max_risk','R0')[1:]):raise PermissionError('actual candidate risk exceeds authorization')
            row=tx.get('candidate_operations',request_id)
            if row['body']['status']=='completed':return decode(CandidateRevision,tx.get('candidate_revisions',row['body']['candidate_ref'])['body'])
            progress=tx.get('candidate_progress',proposal_id)
            if progress['body']['status']=='cancelled' or progress['body']['active_operation']!=request_id:
                raise Conflict('candidate operation no longer active')
            tx.put('candidate_revisions',candidate.candidate_ref,candidate.model_dump(mode='json'),immutable=True)
            tx.put('candidate_operations',request_id,row['body']|{'status':'completed','candidate_ref':candidate.candidate_ref},expected_revision=row['revision'])
            tx.put('candidate_progress',proposal_id,progress['body']|{'status':'verifying','active_operation':None,
                'candidate_refs':[*progress['body']['candidate_refs'],candidate.candidate_ref]},expected_revision=progress['revision'])
            LifecycleBudgetService.settle_in(tx,request_id=op['reservation'])
            LifecycleService.bind_evidence_in(tx,LifecycleService.load_in(tx,proposal.source_key.lifecycle_id),candidate.candidate_ref,kind='candidate_revision')
            if LifecycleService.load_in(tx,proposal.source_key.lifecycle_id).phase=='candidate_building':
                from ..domain.agent_lifecycle import TransitionGuards
                self._event_in(tx,proposal,'revision_frozen',candidate.candidate_ref,token,
                    TransitionGuards(schema_version=1,proof_valid=True))
            tx.put('outbox','candidate-frozen:'+candidate.candidate_ref,{'kind':'candidate_frozen',
                'candidate_ref':candidate.candidate_ref,'lifecycle_id':proposal.source_key.lifecycle_id},immutable=True)
        return candidate

    def cancel(self, proposal_id, *, token):
        with self.repository.transaction() as tx:
            _,proposal,_=self._inputs_in(tx,proposal_id)
            self.authorize_in(tx,proposal,token,new_work=False)
            row=tx.get('candidate_progress',proposal_id)
            from .lifecycle_stop import LifecycleStopService
            life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
            LifecycleStopService.request_lifecycle_in(tx,request_id='candidate-cancel:'+proposal_id,
                record=life,reason='candidate cancelled by authorized owner')
            tx.put('candidate_progress',proposal_id,row['body']|{'status':'cancelled'},expected_revision=row['revision'])
            tx.put('outbox','candidate-cancel:'+proposal_id,{'kind':'candidate_cancel','proposal_id':proposal_id},immutable=True)

    def status(self, proposal_id):
        with self.repository.transaction() as tx:
            row=tx.get('candidate_progress',proposal_id)
            if not row:raise KeyError(proposal_id)
            return row['body']
