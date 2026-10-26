"""Optional, deterministic work lifecycle scheduling; no provider calls in a UoW.

Bindings come from the operator, never from job payloads or model proposals.
Outbox consumers only commit bounded jobs; their handlers own actual job,
thread, lifecycle and shared-work leases before diagnosing or dispatching.
"""
from contextlib import suppress
from .tasks import business_job_kind
from copy import deepcopy
import math

from ..catalog.repair_actions import standard_repair_catalog
from ..domain.agent_lifecycle import LifecycleEvent, RepairPolicy, RepairRequest, TransitionGuards, transition_lifecycle
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict, StaleOwner, positive_duration
from ..services.agent_lifecycle import LifecycleService, decode
from ..services.lifecycle_budgets import LifecycleBudgetService
from ..services.lifecycle_safety import LifecycleSafetyService
from ..services.recovery import RecoveryService
from ..services.remediation import NodeRemediationService
from ..services.repair_strategy import select_strategy_in
from .tasks import JobSpec, TaskBackend


LIFECYCLE_INTENTS = frozenset({'lifecycle_observed', 'lifecycle_advanced', 'decision_ready', 'lifecycle_attach_requested', 'delivery_rework_requested',
    'repair_recovery_required', 'repair_retry_required', 'repair_reconciliation_required',
    'repair_blocked', 'lifecycle_work_completed'})
_BUDGET_FIELDS = ('limits', 'lineage_limits', 'deadline', 'lineage_deadline',
    'safety_limits', 'lineage_safety_limits', 'safety_deadline', 'lineage_safety_deadline', 'cost_unit')


class LifecycleSupervisor:
    def __init__(self, repository, *, bindings, lease_seconds, owner='lifecycle', model_team=None, repair_catalog=None,evolution=None):
        self.repository = repository
        self.lease_seconds = positive_duration(lease_seconds)
        self.owner = owner
        self.model_team = model_team
        self.evolution = evolution
        self.repair_catalog = repair_catalog or standard_repair_catalog()
        self.bindings = {}
        for value in bindings:
            binding = deepcopy(value)
            if 'scope_ref' in binding:
                if binding.get('work_ref',binding['scope_ref'])!=binding['scope_ref']:raise ValueError('conflicting lifecycle scope aliases')
                binding['work_ref']=binding['scope_ref']
            for field in ('binding_ref', 'run_ref', 'work_ref', 'node_id', 'policy_ref',
                          'authorization_ref', 'principal', *_BUDGET_FIELDS):
                if field not in binding or binding[field] in (None, ''):
                    raise ValueError(f'explicit lifecycle binding {field} required')
            key = tuple(binding[k] for k in ('run_ref', 'work_ref', 'node_id'))
            if key in self.bindings:
                raise ValueError('duplicate run/work/node lifecycle binding')
            self.bindings[key] = binding

    def _binding(self, run, work, node):
        return self.bindings.get((run, work, node))

    @staticmethod
    def _diagnosis_identity(lifecycle_id, attempt_refs=()):
        # Draining -> diagnosing changes revision, but not the logical round.
        # Observation, its advanced event and replay all share this identity;
        # only a committed repair order creates a new diagnosis round.
        return lifecycle_id + ':diagnosis-after:' + (attempt_refs[-1] if attempt_refs else 'source')

    def _registered_action(self, policy, node_id, result):
        for rule in self.repair_catalog.rules:
            if (rule.node_id == node_id and rule.action_ref in policy.actions
                    and rule.predicate == 'source_error'
                    and (result.get('error') or {}).get('code') in rule.error_codes):
                action_id, _, version = rule.action_ref.rpartition('@')
                return self.repair_catalog.resolve(action_id, version, node_id).require_available()
        return None

    @staticmethod
    def _attachment_owner_in(tx, command):
        """Resolve actual recovery ownership, independent of caller payload flags.

        A plan's immutable activation assignment and Driver startup record bind
        the current child to its original order/lifecycle. No current-owner
        lease or still-valid proof is required to READ that historical identity.
        """
        row = tx.get('jobs',command['original_job_id'])
        receipt = tx.get('receipts',command['source_receipt'])
        if not row or not receipt:
            raise Conflict('attachment original job or source receipt missing')
        job, identity = row['body'],receipt['body']['identity']
        if ((job['run_id'],job['scope_ref'])!=(command['run_ref'],command['work_ref'])
                or any(identity.get(k)!=v for k,v in {
                    'activation_id':job['activation_id'],'thread_id':job['thread_id'],
                    'scope':job['scope_ref'],'run_ref':job['run_id'],'node_id':command['node_id']}.items())):
            raise Conflict('attachment owner source identity mismatch')
        activation = tx.get('activations',job['activation_id'])
        record = activation['body']['record'] if activation else {}
        started = tx.get('recovery_starts',job['activation_id'])
        refs = {ref for ref in (job['payload'].get('recovery_plan_ref'),record.get('recovery_plan_ref'),
            started['body']['plan_ref'] if started else None) if ref}
        refs.update(row['key'] for row in tx.scan('recovery_plan_activations')
                    if row['body'].get('activation_id')==job['activation_id'])
        if not refs:
            if job['payload'].get('repair_order_ref'):
                raise Conflict('lifecycle repair child requires its original lifecycle, not root attachment')
            return None
        metadata = tx.get('activation_jobs',job['activation_id'])
        expected = {'activation_id':job['activation_id'],'thread_id':job['thread_id'],
            'run_id':job['run_id'],'scope':job['scope_ref'],
            'plan_ref':identity['plan_ref'],'graph_ref':identity['graph_ref'],
            'input_refs':identity['input_refs']}
        if (not metadata or metadata['body'].get('job_id')!=job['job_id']
                or not activation or any(record.get(k)!=v for k,v in expected.items())):
            raise Conflict('lifecycle recovery child activation identity invalid')
        owners = []
        for ref in sorted(refs):
            plan, context = RecoveryService._load_in(tx,ref)
            assigned = tx.get('recovery_plan_activations',ref)
            order = tx.get('repair_orders',context['order_ref'])
            lifecycle = tx.get('agent_lifecycles',context['lifecycle_id'])
            if (not assigned or assigned['body']!={'activation_id':job['activation_id'],'thread_id':job['thread_id']}
                    or not started or started['body'].get('plan_ref')!=ref
                    or started['body'].get('owner',{}).get('thread_id')!=job['thread_id']
                    or (context['scope'],context['run_ref'],context['plan_ref'],plan.graph_ref,context['input_refs'])
                    !=(job['scope_ref'],job['run_id'],identity['plan_ref'],identity['graph_ref'],identity['input_refs'])
                    or not order or not lifecycle):
                raise Conflict('lifecycle recovery child plan ownership invalid')
            original = LifecycleService.load_in(tx,context['lifecycle_id'])
            if (order['body']['lifecycle_id']!=original.key.lifecycle_id
                    or context['order_ref'] not in original.attempt_refs
                    or (order['body']['work_ref'],order['body']['run_ref'],order['body']['source_receipt'])
                    !=(original.key.scope_ref,original.key.run_ref,original.source_receipt)
                    or (original.key.scope_ref,original.key.run_ref,original.plan_ref,original.graph_ref,original.contract_ref)
                    !=(context['scope'],context['run_ref'],context['plan_ref'],plan.graph_ref,plan.contract_ref)
                    or context['source_receipt']!=original.source_receipt):
                raise Conflict('lifecycle recovery child original order identity invalid')
            owners.append({'status':'rejected','reason':'lifecycle_owned_recovery_attempt',
                'lifecycle_id':original.key.lifecycle_id,'source_receipt':original.source_receipt,
                'attempted_source_receipt':command['source_receipt'],'recovery_plan_ref':ref,
                'recovery_entry':'read agent_lifecycles/' + original.key.lifecycle_id + ' and its lifecycle_blockers entry',
                'resume_entry':'existing lifecycle only: explicit phase, authority and remaining/adjusted budget required; attach_failure does not continue it'})
        if len(owners)!=1:
            raise Conflict('lifecycle recovery child has conflicting plan assignments')
        return owners[0]

    @staticmethod
    def _reject_attachment_in(tx, request_id, command, result, *, intent_ref=None):
        tx.put('lifecycle_attachment_rejections',request_id,{'command':command,'result':result},immutable=True)
        if intent_ref:
            body = result | {'intent_ref':intent_ref}
            for kind in ('lifecycle_intent_results','outbox_errors'):
                old = tx.get(kind,intent_ref)
                if not old or old['body']!=body:
                    tx.put(kind,intent_ref,body)
        return result

    def _validate_attachment_in(self, tx, command):
        """Check the current completed attempt; attaching never issues authority."""
        binding = self._binding(command['run_ref'],command['work_ref'],command['node_id'])
        if not binding or binding['binding_ref']!=command['binding_ref']:
            raise PermissionError('attachment scope has no exact configured binding')
        work = tx.get('work_instances',command['work_ref'])
        expected_revision = command['expected_work_revision']
        if (type(expected_revision) is not int or expected_revision<0 or not work
                or work['revision']!=expected_revision):
            raise Conflict('attachment work revision changed')
        jobrow = tx.get('jobs',command['original_job_id'])
        receipt = tx.get('receipts',command['source_receipt'])
        if not jobrow or not receipt:
            raise Conflict('attachment original job or source receipt missing')
        job, identity = jobrow['body'], receipt['body']['identity']
        versions = {k:work['body'].get(k) for k in ('plan_ref','graph_ref','contract_ref')}
        expected = {'run_ref':command['run_ref'],'scope':command['work_ref'],
            'node_id':command['node_id'],'activation_id':job['activation_id'],
            'thread_id':job['thread_id'],'plan_ref':versions['plan_ref'],'graph_ref':versions['graph_ref']}
        metadata = tx.get('activation_jobs',job['activation_id'])
        activation = tx.get('activations',job['activation_id'])
        facts = tx.get('work_safety_facts',command['work_ref'])
        refs = job.get('result',{}).get('graph_result',{}).get('result_refs',{})
        from ..catalog.registry import NODE_GROUPS
        failures = [refs[node] for node in NODE_GROUPS['work']
                    if node in refs and tx.get('agent_supervision',refs[node])]
        if (business_job_kind(job['job_kind'])!='work' or job['run_id']!=command['run_ref']
                or job['scope_ref']!=command['work_ref']
                or job['status'] not in {'complete','failed','blocked'}
                or not tx.get('event_ids','completion:' + job['job_id'])
                or any(identity.get(k)!=v for k,v in expected.items())
                or any(job['payload'].get(k)!=versions[k] for k in ('plan_ref','graph_ref'))
                or work['body'].get('run_id')!=command['run_ref']
                or work['body'].get('result_ref')!='job:' + job['job_id']
                or not metadata or metadata['body'].get('job_id')!=job['job_id']
                or not activation or activation['body'].get('status')!='complete'
                or activation['body']['record'].get('input_refs')!=identity.get('input_refs')
                or activation['body']['result'].get('result_refs',{}).get(command['node_id'])!=command['source_receipt']
                or not failures or failures[0]!=command['source_receipt']
                or not facts or facts['body'].get('activation_id')!=job['activation_id']
                or not LifecycleSafetyService._closure_valid(LifecycleSafetyService._snapshot_in(tx,command['work_ref']))):
            raise Conflict('attachment source is not the current completed failed attempt')
        for row in tx.scan('jobs'):
            other = row['body']
            if (business_job_kind(other['job_kind'])=='work' and other['scope_ref']==command['work_ref']
                    and other['status'] in {'pending','running','draining'}):
                raise Conflict('another work attempt already pending or running')
        run = tx.get('runs',command['run_ref'])
        gate = tx.get('dispatch_gates',command['run_ref'])
        if (not run or not gate or gate['body'].get('open') is not True
                or gate['body'].get('generation')!=run['body'].get('dispatch_generation')
                or run['body'].get('graph_ref')!=versions['graph_ref']
                or run['body'].get('active_plan_ref')!=versions['plan_ref']
                or tx.now>=min(binding['deadline'],binding['lineage_deadline'])):
            raise Conflict('attachment dispatch gate, versions or deadline changed')
        authorization = tx.get('repair_authorizations',command['authorization_ref'])
        auth = authorization['body'] if authorization else {}
        expiry = auth.get('expires_at')
        auth_scope = {'principal':binding['principal'],'run_ref':command['run_ref'],
            'scope_ref':command['work_ref'],'work_revision':expected_revision,**versions}
        if (command['principal']!=binding['principal']
                or command['authorization_ref']!=binding['authorization_ref']
                or any(auth.get(k)!=v for k,v in auth_scope.items()) or auth.get('revoked') is not False
                or type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=tx.now):
            raise PermissionError('attachment requires current exact-scope operator authorization')
        policyrow = tx.get('repair_policies',binding['policy_ref'])
        if not policyrow:
            raise Conflict('attachment registered policy missing')
        action = self._registered_action(decode(RepairPolicy,policyrow['body']),command['node_id'],receipt['body']['result'])
        if action and (action.ref not in auth.get('actions',())
                       or not set(action.permissions)<=set(auth.get('permissions',()))):
            raise PermissionError('attachment registered action permission missing')
        return binding,authorization,jobrow,receipt

    def attach_failure(self, *, request_id, binding_ref, run_ref, work_ref, node_id,
                       original_job_id, source_receipt, expected_work_revision,
                       principal, authorization_ref):
        """Persist an explicit observation intent after legacy supervision delivery.

        The caller supplies an existing exact operator authorization. Source
        receipt/review/delivery are read-only, and request replay is historical
        acknowledgment, never a new grant for the current work revision.
        """
        if not isinstance(request_id,str) or not request_id:
            raise ValueError('explicit attachment request ID required')
        command = {'schema_version':1,'binding_ref':binding_ref,'run_ref':run_ref,
            'work_ref':work_ref,'node_id':node_id,'original_job_id':original_job_id,
            'source_receipt':source_receipt,'expected_work_revision':expected_work_revision,
            'principal':principal,'authorization_ref':authorization_ref}
        with self.repository.transaction() as tx:
            return self._attach_failure_in(tx, request_id, command)

    def _attach_failure_in(self, tx, request_id, command):
        rejected = tx.get('lifecycle_attachment_rejections',request_id)
        if rejected:
            if rejected['body']['command']!=command:
                raise Conflict('attachment request ID input conflict')
            return rejected['body']['result']
        previous = tx.get('lifecycle_attachments',request_id)
        if previous:
            if previous['body']['command']!=command:
                raise Conflict('attachment request ID input conflict')
            owned = self._attachment_owner_in(tx,command)
            if owned:
                return self._reject_attachment_in(tx,request_id,command,owned)
            return previous['body']['result']
        binding,auth,job,receipt = self._validate_attachment_in(tx,command)
        owned = self._attachment_owner_in(tx,command)
        if owned:
            return self._reject_attachment_in(tx,request_id,command,owned)
        lifecycle_id = 'lifecycle:' + canonical_hash({'source_receipt':command['source_receipt']})
        intent_ref = 'lifecycle-attach:' + canonical_hash({'request_id':request_id})
        result = {'status':'accepted','lifecycle_id':lifecycle_id,'intent_ref':intent_ref}
        tx.put('lifecycle_attachments',request_id,{'command':command,'result':result,
            'binding_hash':canonical_hash(binding),'authorization_revision':auth['revision'],
            'source_job_hash':canonical_hash(job),'source_receipt_hash':canonical_hash(receipt)},immutable=True)
        tx.put('outbox',intent_ref,{'kind':'lifecycle_attach_requested',
            'lifecycle_id':lifecycle_id,'request_ref':request_id},immutable=True)
        return result

    @staticmethod
    def _block_in(tx, identity, *, reason, intent_ref, operation_refs=(), status='blocked', lifecycle_id=None, job_ref=None):
        body = {'status':status, 'reason':reason, 'intent_ref':intent_ref,
            'lifecycle_id':lifecycle_id, 'operation_refs':list(operation_refs),
            'resume_entry': 'restore the named dependency/authorization, then replay this retained intent',
            'recovery_entry':'LifecycleSupervisor.consume_in', 'job_ref':job_ref}
        for kind, key in (('lifecycle_blockers',identity),('lifecycle_intent_results',intent_ref),
                          ('outbox_errors',intent_ref)):
            previous = tx.get(kind,key)
            if not previous or previous['body']!=body:
                tx.put(kind,key,body)
        return False

    def _initialize_in(self, tx, binding, lifecycle_id):
        row = tx.get('repair_policies', binding['policy_ref'])
        if not row:
            raise Conflict('registered repair policy missing')
        policy = decode(RepairPolicy, row['body'])
        caps = {'repair_attempts':policy.max_repair_attempts,
            'diagnosis_calls':policy.max_diagnosis_calls, 'candidate_revisions':policy.max_candidate_revisions,
            'tokens':policy.token_limit, 'child_jobs':policy.child_job_limit, 'observations':policy.max_observations}
        if (any(binding['limits'].get(k, 0) > maximum for k, maximum in caps.items())
                or binding['deadline'] > policy.deadline
                or binding['safety_deadline'] > policy.safety_budget.deadline
                or binding['cost_unit'] != policy.safety_budget.cost_unit
                or any(binding['safety_limits'].get(k, 0) > maximum for k, maximum in {
                    'stop_calls':policy.safety_budget.max_stop_calls,
                    'query_calls':policy.safety_budget.max_query_calls,
                    'cleanup_calls':policy.safety_budget.max_cleanup_calls,
                    'cost':policy.safety_budget.cost_limit}.items())):
            raise PermissionError('binding exceeds registered lifecycle policy')
        run = tx.get('runs', binding['run_ref'])
        if not run:
            raise Conflict('binding run missing')
        LifecycleBudgetService.initialize_in(tx, lifecycle_id=lifecycle_id,
            lineage_budget_ref=run['body']['lineage_id'], **{k:binding[k] for k in _BUDGET_FIELDS})
        tx.put('lifecycle_bindings', binding['binding_ref'], binding, immutable=True)
        tx.put('lifecycle_binding_refs', lifecycle_id, {'binding_ref':binding['binding_ref']}, immutable=True)

    def _enqueue_in(self, tx, binding, lifecycle_id, stage, identity, payload, *, due_at=None, intent_ref=None):
        from ..services.lifecycle_stop import LifecycleStopService
        if tx.get('agent_lifecycles',lifecycle_id):
            LifecycleStopService.assert_admission_in(tx,LifecycleService.load_in(tx,lifecycle_id))
        key = 'lifecycle-job:' + canonical_hash({'lifecycle':lifecycle_id, 'stage':stage, 'identity':identity})
        if tx.get('jobs', key):
            return True
        deadline = min(binding['deadline'], binding['lineage_deadline'])
        if tx.now >= deadline:
            return self._block_in(tx, lifecycle_id, reason='lifecycle_dispatch_deadline_elapsed',
                intent_ref=identity, lifecycle_id=lifecycle_id)
        LifecycleBudgetService.reserve_in(tx, request_id=key, lifecycle_id=lifecycle_id,
            amounts={'child_jobs':1}, input_ref=identity, metadata={'stage':stage})
        TaskBackend.enqueue_in(tx, JobSpec(job_id=key, job_kind='lifecycle',
            scope_ref=binding['work_ref'], thread_id=key, activation_id=key,
            dedupe_key=key, due_at=tx.now if due_at is None else due_at, deadline=deadline,
            run_id=binding['run_ref'],
            dispatch_generation=tx.get('runs', binding['run_ref'])['body']['dispatch_generation'],
            payload={'lifecycle_id':lifecycle_id, 'binding_ref':binding['binding_ref'],
                     'stage':stage, 'intent_ref':intent_ref or identity, **payload}))
        return True

    def _delivery_rework_in(self, tx, intent_ref, body):
        from ..services.delivery_rework import DeliveryReworkService
        gap_ref = body['gap_ref']
        gaprow = tx.get('delivery_rework_gaps', gap_ref)
        if not gaprow or intent_ref != gap_ref:
            raise Conflict('rework intent has no immutable T6 gap')
        gap = gaprow['body']
        status = tx.get('delivery_rework_status', gap_ref)
        if status and status['body'].get('status') == 'resolved':
            resolution = tx.get('delivery_rework_resolutions', status['body']['resolution_ref'])
            if not resolution or resolution['body']['gap_ref'] != gap_ref:
                raise Conflict('resolved rework has no immutable resolution')
            return True
        try:
            delivery = tx.get('work_deliveries', gap['delivery_ref'])
            review = tx.get('reviews', gap['review_ref'])
            if (not delivery or not review or canonical_hash(delivery['body']) != gap['delivery_hash']
                    or canonical_hash(review['body']) != gap['review_hash']
                    or review['body']['delivery_hash'] != gap['delivery_hash']):
                raise Conflict('rework frozen delivery/review changed')
            source = DeliveryReworkService.source_in(tx, gap['delivery_ref'], delivery['body'],
                run_ref=gap['run_ref'], group_ref=gap['group_ref'])
            # Current work revision may advance after recovery; historical source may not.
            if any(source[k] != v for k, v in gap['source'].items() if k != 'work_revision'):
                raise Conflict('rework source identity changed')
            command = {'schema_version':1, 'run_ref':gap['run_ref'],
                'work_ref':source['work_ref'], 'node_id':source['node_id'],
                'original_job_id':source['job_id'], 'source_receipt':source['receipt_ref'],
                'expected_work_revision':gap['source']['work_revision']}
            owner = self._attachment_owner_in(tx, command)
            generation = tx.get('generation_failure_links', source['receipt_ref'])
            lifecycle_id = (owner['lifecycle_id'] if owner else
                generation['body']['source_lifecycle_ref'] if generation else
                'lifecycle:' + canonical_hash({'source_receipt':source['receipt_ref']}))
            lifecycle = tx.get('agent_lifecycles', lifecycle_id)
            if generation and lifecycle:
                from ..services.measurement_generation import MeasurementGenerationService
                MeasurementGenerationService._failure_source_in(tx,
                    LifecycleService.load_in(tx, lifecycle_id), source['receipt_ref'])
            if lifecycle:
                # Merely link: repeated review cannot replenish or resume an exhausted lifecycle.
                result = {'status':'linked', 'lifecycle_id':lifecycle_id}
            elif owner or generation:
                raise Conflict('original lifecycle owner missing')
            else:
                binding = self._binding(gap['run_ref'], source['work_ref'], source['node_id'])
                if not binding:
                    raise PermissionError('exact work/node operator authorization binding missing')
                command.update({k:binding[k] for k in ('binding_ref','principal','authorization_ref')})
                result = self._attach_failure_in(tx, gap_ref, command)
            tx.put('delivery_rework_status', gap_ref, result)
            return True
        except (Conflict, PermissionError, ValueError) as exc:
            tx.put('delivery_rework_status', gap_ref, {'status':'needs_input', 'reason':str(exc)})
            return False

    def consume_in(self, tx, intent_ref, body):
        """None means legacy/unbound supervision; False retains the durable intent."""
        kind = body.get('kind')
        if kind=='delivery_rework_requested':
            return self._delivery_rework_in(tx, intent_ref, body)
        if kind=='lifecycle_attach_requested':
            attachment = tx.get('lifecycle_attachments',body['request_ref'])
            if not attachment or attachment['body']['result']['intent_ref']!=intent_ref:
                raise Conflict('attachment intent has no original command')
            saved = attachment['body']
            lifecycle_id = saved['result']['lifecycle_id']
            identity = self._diagnosis_identity(lifecycle_id)
            key = 'lifecycle-job:' + canonical_hash({'lifecycle':lifecycle_id,'stage':'diagnose','identity':identity})
            try:
                owned = self._attachment_owner_in(tx,saved['command'])
                if owned:
                    self._reject_attachment_in(tx,body['request_ref'],saved['command'],owned,intent_ref=intent_ref)
                    return False
                if tx.get('jobs',key):
                    return True
                binding,auth,job,receipt = self._validate_attachment_in(tx,saved['command'])
                if (canonical_hash(binding)!=saved['binding_hash']
                        or auth['revision']!=saved['authorization_revision']
                        or canonical_hash(job)!=saved['source_job_hash']
                        or canonical_hash(receipt)!=saved['source_receipt_hash']):
                    raise Conflict('attachment frozen authority or source changed')
                self._initialize_in(tx,binding,lifecycle_id)
                return self._enqueue_in(tx,binding,lifecycle_id,'diagnose',identity,
                    {'receipt_ref':saved['command']['source_receipt']},intent_ref=intent_ref)
            except (Conflict,PermissionError,ValueError) as exc:
                return self._block_in(tx,lifecycle_id,reason=str(exc),intent_ref=intent_ref,lifecycle_id=lifecycle_id)
        if kind == 'agent_supervision':
            binding = self._binding(body['run_ref'], body['scope'], body['node_id'])
            receipt = tx.get('receipts', body['receipt_ref'])
            identity = receipt['body']['identity'] if receipt else {}
            metadata = tx.get('activation_jobs', identity.get('activation_id', ''))
            source = tx.get('jobs', metadata['body'].get('job_id', '')) if metadata else None
            # Repair visits and recovery failures belong to their existing lifecycle.
            if source and (source['body']['payload'].get('repair_order_ref') or source['body']['payload'].get('recovery_plan_ref')):
                return True
            if binding is None:
                return None
            if not source or source['body']['status'] in {'pending','running','draining'}:
                return False
            job = source['body']
            # _publish_completion carries actual closure facts to the final metadata revision.
            if business_job_kind(job['job_kind'])=='work' and not tx.get('event_ids', 'completion:' + job['job_id']):
                return False
            result = job.get('result', {}).get('graph_result', {})
            refs = result.get('result_refs', {})
            from ..catalog.registry import NODE_GROUPS
            nodes=next((nodes for nodes in NODE_GROUPS.values() if identity['node_id'] in nodes),())
            failures = [refs[node] for node in nodes
                        if node in refs and tx.get('agent_supervision',refs[node])]
            if not failures or failures[0] != body['receipt_ref']:
                return True
            lifecycle_id = 'lifecycle:' + canonical_hash({'source_receipt':body['receipt_ref']})
            try:
                # A failure can reach this outbox after cutoff. Recording its
                # original lifecycle is observation, not admission to diagnosis.
                from ..services.lifecycle_stop import LifecycleStopService
                stopped=any((r['body']['run_ref'],r['body']['plan_ref'],r['body']['graph_ref']) ==
                    (identity['run_ref'],identity['plan_ref'],identity['graph_ref'])
                    for r in tx.scan('run_stop_requests'))
                if stopped:
                    if not tx.get('agent_lifecycles',lifecycle_id):
                        token=tx.acquire_thread(identity['thread_id'],'stop-failure-observer',self.lease_seconds)
                        try:
                            LifecycleService.observe_failure_in(tx,body['receipt_ref'],
                                policy_ref=binding['policy_ref'],token=token)
                            self._initialize_in(tx,binding,lifecycle_id)
                        finally:
                            tx.release_thread(token)
                    return True
                self._initialize_in(tx, binding, lifecycle_id)
                return self._enqueue_in(tx, binding, lifecycle_id, 'diagnose',
                    self._diagnosis_identity(lifecycle_id), {'receipt_ref':body['receipt_ref']},
                    intent_ref=intent_ref)
            except (Conflict, PermissionError, ValueError) as exc:
                return self._block_in(tx, lifecycle_id, reason=str(exc), intent_ref=intent_ref, lifecycle_id=lifecycle_id)
        if kind not in LIFECYCLE_INTENTS:
            return None
        lifecycle_id = body['lifecycle_id']
        record = LifecycleService.load_in(tx, lifecycle_id)
        binding = self._binding(record.key.run_ref, record.key.scope_ref, record.key.node_id)
        if not binding:
            return self._block_in(tx, lifecycle_id, reason='lifecycle_binding_missing',
                intent_ref=intent_ref, lifecycle_id=lifecycle_id)
        if kind == 'repair_reconciliation_required':
            return self._block_in(tx, lifecycle_id, reason='original_operation_reconciler_not_connected: ' + str(body.get('reason')),
                intent_ref=intent_ref, operation_refs=body.get('operation_refs', ()),
                status='waiting_dependency', lifecycle_id=lifecycle_id)
        if kind == 'repair_blocked':
            return self._block_in(tx, lifecycle_id, reason=body.get('reason') or 'repair_exhausted',
                intent_ref=intent_ref, lifecycle_id=lifecycle_id)
        if kind == 'decision_ready':
            stage, identity, payload = 'decision', body['decision_ref'], {'decision_ref':body['decision_ref']}
            if tx.get('lifecycle_decision_consumptions', body['decision_ref']):
                return True
        elif kind == 'repair_recovery_required':
            stage, identity, payload = 'recover', intent_ref, {}
        elif kind == 'lifecycle_work_completed':
            stage, identity, payload = 'complete', intent_ref, {'source_job_ref':body['job_ref']}
        elif kind == 'repair_retry_required' or record.phase == 'backoff':
            if record.phase not in {'diagnosing','backoff'}:
                return True
            stage, identity, payload = 'diagnose', self._diagnosis_identity(lifecycle_id,record.attempt_refs), {}
        elif record.phase in {'draining','diagnosing'}:
            if tx.get('lifecycle_blockers', lifecycle_id):
                return False
            if any(r['body']['lifecycle_id']==lifecycle_id and r['body']['expected_revision']==record.revision
                   for r in tx.scan('lifecycle_decisions')):
                return True
            stage, identity, payload = 'diagnose', self._diagnosis_identity(lifecycle_id,record.attempt_refs), {}
        else:
            return True
        try:
            return self._enqueue_in(tx, binding, lifecycle_id, stage, identity, payload,
                due_at=record.next_due_at if record.phase=='backoff' else None, intent_ref=intent_ref)
        except (Conflict, PermissionError, ValueError) as exc:
            return self._block_in(tx, lifecycle_id, reason=str(exc), intent_ref=intent_ref, lifecycle_id=lifecycle_id)

    @staticmethod
    def job_ended_in(tx, job):
        """Finite task watchdog publishes failed/expired scheduler ownership."""
        payload = job.get('payload',{})
        if job['job_kind']=='lifecycle':
            if tx.get('budget_reservations',job['job_id']):
                LifecycleBudgetService.settle_in(tx,request_id=job['job_id'])
            if not tx.get('lifecycle_dispatch_results',job['job_id']):
                LifecycleSupervisor._block_in(tx,payload['lifecycle_id'],
                    reason='lifecycle_job_deadline_or_attempt_budget' if job.get('reason')=='deadline_or_attempt_budget'
                    else 'lifecycle_job_stopped: ' + str(job.get('result',{}).get('error')),
                    intent_ref=payload['intent_ref'], lifecycle_id=payload['lifecycle_id'],job_ref=job['job_id'])
        if payload.get('recovery_plan_ref') and tx.get('budget_reservations',job['job_id']):
            LifecycleBudgetService.settle_in(tx,request_id=job['job_id'])

    @staticmethod
    def completed_in(tx, job):
        ref = job['payload'].get('recovery_plan_ref')
        if ref:
            context = tx.get('recovery_contexts', ref)
            if not context:
                raise Conflict('completed recovery context missing')
            tx.put('outbox', 'lifecycle-complete:' + job['job_id'], {
                'kind':'lifecycle_work_completed', 'lifecycle_id':context['body']['lifecycle_id'],
                'job_ref':job['job_id']}, immutable=True)

    def resume_intent(self, intent_ref, *, request_id, principal, authorization_ref):
        """Operator entry for a denied decision after an exact authorization update.

        This queues a new bounded consumer of the SAME decision. It cannot
        clear effect isolation, change lifecycle phase, or replace an order.
        """
        if not request_id:
            raise ValueError('explicit resume request ID required')
        command = {'intent_ref':intent_ref,'principal':principal,'authorization_ref':authorization_ref}
        with self.repository.transaction() as tx:
            previous = tx.get('lifecycle_resumptions',request_id)
            if previous:
                if previous['body']['command']!=command:
                    raise Conflict('resume request ID input conflict')
                return previous['body']['job_ref']
            blocked = tx.get('lifecycle_intent_results',intent_ref)
            source = tx.get('jobs',blocked['body'].get('job_ref','')) if blocked else None
            if not source or source['body']['payload']['stage']!='decision':
                raise Conflict('only a retained denied decision can be resumed; effect reconciliation requires its registered dependency')
            payload = source['body']['payload']
            binding = tx.get('lifecycle_bindings',payload['binding_ref'])['body']
            record = LifecycleService.load_in(tx,payload['lifecycle_id'])
            work = tx.get('work_instances',record.key.scope_ref)
            authrow = tx.get('repair_authorizations',authorization_ref)
            auth = authrow['body'] if authrow else {}
            expiry = auth.get('expires_at')
            expected = {'principal':binding['principal'],'run_ref':record.key.run_ref,
                'scope_ref':record.key.scope_ref,'plan_ref':record.plan_ref,
                'graph_ref':record.graph_ref,'contract_ref':record.contract_ref,
                'work_revision':work['revision']}
            if (principal!=binding['principal'] or authorization_ref!=binding['authorization_ref']
                    or any(auth.get(k)!=v for k,v in expected.items()) or auth.get('revoked') is not False
                    or type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=tx.now):
                raise PermissionError('resume requires current exact-scope operator authorization')
            if record.phase!='diagnosing' or LifecycleSafetyService._repair_isolations_in(tx,record.key.scope_ref):
                raise Conflict('resume cannot bypass lifecycle phase or effect isolation')
            self._enqueue_in(tx,binding,record.key.lifecycle_id,'decision','resume:' + request_id,
                {'decision_ref':payload['decision_ref']},intent_ref=intent_ref)
            identity = 'lifecycle-job:' + canonical_hash({'lifecycle':record.key.lifecycle_id,
                'stage':'decision','identity':'resume:' + request_id})
            if not tx.get('jobs',identity):
                raise Conflict('resume deadline does not permit another consumer')
            tx.put('lifecycle_resumptions',request_id,{'command':command,'job_ref':identity,
                'previous_blocker':blocked['body'],'authorization_revision':authrow['revision']},immutable=True)
            tx.delete('lifecycle_blockers',record.key.lifecycle_id)
            tx.delete('outbox_errors',intent_ref)
            return identity

    @staticmethod
    def _proof_in(tx, record, token, ttl):
        ref = LifecycleSafetyService.produce_disposition_in(tx, record.key.scope_ref, token, ttl=ttl)
        LifecycleSafetyService.verify_disposition_in(tx, ref, record.key.scope_ref, token)
        tx.put('lifecycle_dispositions', record.key.lifecycle_id, {'receipt_ref':ref})
        LifecycleService.bind_evidence_in(tx,record,ref,kind='resource_disposition')
        return ref

    @staticmethod
    def _event(tx, record, kind, evidence, identity):
        return LifecycleEvent(schema_version=1, event_id=identity, kind=kind,
            source='lifecycle_supervisor', expected_revision=record.revision,
            evidence_refs=tuple(evidence), observed_at=tx.now)

    async def __call__(self, job):
        payload = job['payload']
        lifecycle_id = payload['lifecycle_id']
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx, job)
            old = tx.get('lifecycle_dispatch_results', job['job_id'])
            if old:
                return old['body']
            binding_row = tx.get('lifecycle_bindings', payload['binding_ref'])
            binding = binding_row['body'] if binding_row else None
            if not binding or binding != self._binding(job['run_id'], job['scope_ref'], binding['node_id']):
                raise Conflict('frozen lifecycle binding unavailable or changed')
            token = tx.acquire_thread(job['thread_id'], self.owner,
                min(self.lease_seconds, job['deadline']-tx.now))
        owns_work = False
        try:
            service = LifecycleService(self.repository)
            if payload.get('receipt_ref'):
                service.observe_failure(payload['receipt_ref'], policy_ref=binding['policy_ref'], token=token)
            service.claim(lifecycle_id, token=token)
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx, job)
                LifecycleSafetyService.acquire_work_in(tx, binding['work_ref'], token, purpose='diagnosis')
                owns_work = True
            stage = payload['stage']
            if stage == 'diagnose':
                result = self._diagnose(job, token)
            elif stage == 'decision':
                result = self._decide(job, token, binding)
            elif stage == 'recover':
                result = self._recover(job, token)
            else:
                result = self._complete(job, token)
        except (Conflict, PermissionError, ValueError) as exc:
            if isinstance(exc, StaleOwner):
                raise
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx, job)
                self._block_in(tx, lifecycle_id, reason=str(exc), intent_ref=payload['intent_ref'],
                    lifecycle_id=lifecycle_id,job_ref=job['job_id'])
            result = {'execution_status':'blocked','error':{'code':'LIFECYCLE_BLOCKED','reason':str(exc)}}
        finally:
            with self.repository.transaction() as tx:
                if owns_work:
                    with suppress(StaleOwner):
                        LifecycleSafetyService.release_work_in(tx, job['scope_ref'], token)
                with suppress(StaleOwner):
                    tx.release_thread(token)
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx, job)
            LifecycleBudgetService.settle_in(tx, request_id=job['job_id'])
            tx.put('lifecycle_dispatch_results', job['job_id'], result, immutable=True)
        return result

    def _diagnose(self, job, token):
        lifecycle_id = job['payload']['lifecycle_id']
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx, job)
            record = LifecycleService.load_in(tx, lifecycle_id)
            proof = self._proof_in(tx, record, token, min(self.lease_seconds, record.deadline-tx.now))
            if record.phase in {'draining','backoff'}:
                kind = 'timer_due' if record.phase=='backoff' else 'disposition_confirmed'
                event = self._event(tx, record, kind, (proof,), f'diagnose:{lifecycle_id}:{record.revision}')
                record = LifecycleService.advance_in(tx, lifecycle_id, event, expected_revision=record.revision, token=token)
            if record.phase != 'diagnosing':
                return {'execution_status':'succeeded','phase':record.phase}
            policy = decode(RepairPolicy, tx.get('repair_policies',record.policy_ref)['body'])
            selection, exhausted = select_strategy_in(tx,record,policy,self.repair_catalog)
            action_ref = selection['action_ref'] if selection else None
            evidence_refs = tuple(selection['evidence_refs']) if selection else (record.source_receipt,)
            if exhausted and selection is None:
                event=self._event(tx,record,'no_safe_route',evidence_refs,
                    f'no-progress:{lifecycle_id}:{record.revision}')
                # No action may bypass max_same_failure through a growing evidence list.
                record=LifecycleService.advance_in(tx,lifecycle_id,event,
                    expected_revision=record.revision,token=token)
                self._block_in(tx,lifecycle_id,reason='no_progress',
                    intent_ref=job['payload']['intent_ref'],lifecycle_id=lifecycle_id)
                return {'execution_status':'blocked','phase':record.phase}
        if action_ref is None and self.model_team is not None:
            task_ref = self.model_team.request_role(job, token=token, role='investigator')
            return {'execution_status':'succeeded','task_ref':task_ref}
        candidate_available=self.evolution is not None and self.evolution.supports(record)
        decision = LifecycleService(self.repository).record_decision(lifecycle_id,
            request_id=f'diagnosis:{lifecycle_id}:{record.revision}', expected_revision=record.revision,
            route='known_action' if action_ref else 'needs_code' if candidate_available else 'needs_input', action_ref=action_ref,
            evidence_refs=evidence_refs, token=token, strategy_selection=selection)
        return {'execution_status':'succeeded','decision_ref':decision['decision_ref']}

    def _decide(self, job, token, binding):
        lifecycle_id = job['payload']['lifecycle_id']
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx, job)
            record = LifecycleService.load_in(tx, lifecycle_id)
            self._proof_in(tx, record, token, min(self.lease_seconds, record.deadline-tx.now))
        def dispatch(tx, decision):
            TaskBackend.assert_claim_in(tx, job)
            if decision['route']=='needs_code' and self.evolution is not None:
                record=LifecycleService.load_in(tx,lifecycle_id)
                return self.evolution.request_in(tx,decision,record,token=token)
            if decision['route'] != 'known_action':
                record = LifecycleService.load_in(tx, lifecycle_id)
                event = self._event(tx, record, 'needs_input', (record.source_receipt,), 'wait:' + decision['decision_ref'])
                LifecycleService.advance_in(tx, lifecycle_id, event, expected_revision=record.revision, token=token)
                reason = ('candidate_tools_46_not_connected' if decision['route']=='needs_code'
                    else 'model_proposal_' + decision['route'] if self.model_team is not None
                    else 'no_applicable_rule_model_unavailable')
                self._block_in(tx, lifecycle_id, reason=reason,
                    intent_ref=job['payload']['intent_ref'], status='waiting_decision', lifecycle_id=lifecycle_id)
                return {'status':'waiting_decision'}
            record = LifecycleService.load_in(tx, lifecycle_id)
            source = tx.get('lifecycle_sources', lifecycle_id)['body']
            manifest = {'schema_version':1, 'source_receipt':record.source_receipt,
                'evidence_refs':decision['evidence_refs'], 'input_refs':source['identity']['input_refs']}
            ref = 'repair-input:' + canonical_hash({'lifecycle_id':lifecycle_id,'manifest':manifest})
            body = {'lifecycle_id':lifecycle_id,'manifest':manifest}
            tx.put('repair_inputs', ref, body | {'hash':canonical_hash(body)}, immutable=True)
            request = RepairRequest(schema_version=1, request_id='dispatch:' + decision['decision_ref'],
                lifecycle_id=lifecycle_id, source_receipt=record.source_receipt,
                expected_revision=record.revision, action_ref=decision['action_ref'], input_manifest_ref=ref,
                proposal_ref=decision['decision_ref'], authorization_ref=binding['authorization_ref'])
            return NodeRemediationService.request_remediation_in(tx, request,
                principal=binding['principal'], token=token, catalog=self.repair_catalog)
        result = LifecycleService(self.repository).consume_decision(job['payload']['decision_ref'],
            token=token, dispatch_in=dispatch)
        return {'execution_status':'succeeded','output':result}

    def _recover(self, job, token):
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx, job)
            lifecycle_id = job['payload']['lifecycle_id']
            record = LifecycleService.load_in(tx, lifecycle_id)
            identity = 'recovery-work:' + record.attempt_refs[-1]
            previous = tx.get('jobs',identity)
            if previous:
                return {'execution_status':'succeeded','recovery_job_ref':identity,
                    'recovery_plan_ref':previous['body']['payload']['recovery_plan_ref']}
            if record.phase != 'reentering':
                raise Conflict('accepted repair must be reentering')
            plan = RecoveryService.compile_recovery_in(tx, lifecycle_id, token=token, catalog=self.repair_catalog)
            context = tx.get('recovery_contexts',plan.ref)['body']
            order = tx.get('repair_orders',context['order_ref'])['body']
            identity = 'recovery-work:' + context['order_ref']
            previous = tx.get('jobs',identity)
            if not previous:
                LifecycleBudgetService.reserve_in(tx, request_id=identity, lifecycle_id=lifecycle_id,
                    amounts={'child_jobs':1}, input_ref=plan.ref, metadata={'stage':'recovery_work'})
                TaskBackend.enqueue_in(tx, JobSpec(job_id=identity,job_kind='work',
                    scope_ref=record.key.scope_ref,thread_id=identity,activation_id=identity,
                    dedupe_key=identity,due_at=tx.now,deadline=min(record.deadline,context['proof_expires_at']),
                    run_id=record.key.run_ref,dispatch_generation=order['dispatch_generation'],
                    payload=dict(context['input_refs']) | {'plan_ref':record.plan_ref,
                        'graph_ref':record.graph_ref,'recovery_plan_ref':plan.ref}))
            return {'execution_status':'succeeded','recovery_job_ref':identity,'recovery_plan_ref':plan.ref}

    def _complete(self, job, token):
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx, job)
            lifecycle_id = job['payload']['lifecycle_id']
            record = LifecycleService.load_in(tx, lifecycle_id)
            source = tx.get('jobs',job['payload']['source_job_ref'])['body']
            activation = tx.get('activations',source['activation_id'])
            ref = source['payload']['recovery_plan_ref']
            context = tx.get('recovery_contexts',ref)['body']
            start = tx.get('recovery_starts',source['activation_id'])
            evidence = 'recovery-completion:' + source['job_id']
            previous = tx.get('lifecycle_events',evidence)
            if previous and previous['body']['lifecycle_id']==lifecycle_id:
                accepted = previous['body']['event']['kind']=='node_accepted'
                return {'execution_status':'succeeded' if accepted else 'blocked','phase':record.phase}
            if (not activation or activation['body']['status']!='complete' or not start
                    or context['lifecycle_id']!=lifecycle_id or record.phase!='reentering'
                    or context['source_receipt']!=record.source_receipt):
                raise Conflict('recovery completion lacks authoritative activation/plan')
            result = activation['body']['result']
            acceptance = result.get('recovery_acceptance',{})
            proof = self._proof_in(tx, record, token, min(self.lease_seconds,record.deadline-tx.now))
            accepted = (acceptance.get('accepted') is True and not result.get('recovery_dependency_failures')
                and acceptance.get('source_receipt')==record.source_receipt
                and acceptance.get('source_node')==record.key.node_id)
            tx.put('lifecycle_evidence',evidence,{'lifecycle_id':lifecycle_id,'kind':'recovery_acceptance',
                'activation_id':source['activation_id'],'recovery_plan_ref':ref,'acceptance':acceptance},immutable=True)
            event = self._event(tx, record, 'node_accepted' if accepted else 'node_failed',
                (proof,evidence),evidence)
            guards = TransitionGuards(schema_version=1,work_lease_valid=True,
                disposition_closed=True,effects_in_flight=False,recovery_plan_valid=True,node_acceptance_valid=accepted)
            updated = transition_lifecycle(record,event,guards)
            LifecycleService.persist_transition_in(tx,record,event,updated)
            LifecycleBudgetService.settle_in(tx,request_id=source['job_id'])
            if not accepted:
                self._block_in(tx,lifecycle_id,reason=acceptance.get('reason','recovery_source_not_accepted'),
                    intent_ref=job['payload']['intent_ref'],lifecycle_id=lifecycle_id)
            return {'execution_status':'succeeded' if accepted else 'blocked','phase':updated.phase}
