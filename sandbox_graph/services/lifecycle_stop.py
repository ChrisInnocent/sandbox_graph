"""Trusted run-control requests; cancelling admission is not resource closure.

No model-supplied scope, no new repair budget and no external I/O. Stop requests
are durable even when the old owner or a provider is still active.
"""
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict


class LifecycleStopService:
    @classmethod
    def request_lifecycle_in(cls,tx,*,request_id,record,reason):
        from ..domain.lifecycle_stop import StopCommand
        from .lifecycle_disposition import DispositionVersions
        key='scope-stop:'+canonical_hash({'lifecycle_id':record.key.lifecycle_id,'request_id':request_id})
        old=tx.get('run_stop_requests',key)
        if old:
            if old['body']['reason']!=reason:raise Conflict('scoped stop request input changed')
            cls.expand_in(tx,key,old['body']);return key
        run=tx.get('runs',record.key.run_ref)['body']
        command=StopCommand(schema_version=1,request_id=request_id,run_ref=record.key.run_ref,
            plan_ref=record.plan_ref,graph_ref=record.graph_ref,reason=reason,policy='cancel',
            cutoff_generation=run.get('dispatch_generation',1),
            cutoff_disposition_revision=DispositionVersions.current_in(tx,record.key.run_ref),
            created_at=float(tx.now),producer='run_control.v1').model_dump(mode='json')
        tx.put('lifecycle_stop_scopes',key,{'lifecycle_id':record.key.lifecycle_id},immutable=True)
        tx.put('run_stop_requests',key,command,immutable=True)
        cls.expand_in(tx,key,command)
        return key

    @classmethod
    def request_in(cls, tx, *, request_id, run_ref, plan_ref, graph_ref, reason, policy):
        if policy not in {'cancel', 'drain'}:
            raise ValueError('explicit stop disposition required')
        command = {'request_id':request_id, 'run_ref':run_ref, 'plan_ref':plan_ref,
            'graph_ref':graph_ref, 'reason':reason, 'policy':policy}
        key = 'run-stop:' + canonical_hash({'run_ref':run_ref,'request_id':request_id})
        existing=tx.get('run_stop_requests',key)
        if existing:
            if any(existing['body'].get(k)!=v for k,v in command.items()):
                raise Conflict('stop request ID input differs')
            cls.expand_in(tx,key,existing['body'])
            return
        from ..domain.lifecycle_stop import StopCommand
        from .lifecycle_disposition import DispositionVersions
        run=tx.get('runs',run_ref)
        if not run:
            raise Conflict('stop original run missing')
        command=StopCommand(schema_version=1,**command,
            cutoff_generation=run['body'].get('dispatch_generation',1),
            cutoff_disposition_revision=DispositionVersions.current_in(tx,run_ref),
            created_at=float(tx.now),producer='run_control.v1').model_dump(mode='json')
        tx.put('run_stop_requests', key, command, immutable=True)
        cls.expand_in(tx, key, command)

    @staticmethod
    def index_command_in(tx, command_ref, command):
        row = tx.get('run_stop_index',command['run_ref'])
        refs = set(row['body']['refs'] if row else ()) | {command_ref}
        tx.put('run_stop_index',command['run_ref'],{'refs':sorted(refs)})

    @classmethod
    def expand_in(cls, tx, command_ref, command):
        cls.index_command_in(tx,command_ref,command)
        for row in tx.scan('agent_lifecycles'):
            cls.attach_target_in(tx,command_ref,command,row['key'],row['body'])

    @staticmethod
    def attach_target_in(tx, command_ref, command, lifecycle_id, body):
        scoped=tx.get('lifecycle_stop_scopes',command_ref)
        if scoped and scoped['body']['lifecycle_id']!=lifecycle_id:return
        if (body.get('key',{}).get('run_ref'),body.get('plan_ref'),body.get('graph_ref')) != (
                command['run_ref'],command['plan_ref'],command['graph_ref']):
            return
        key = 'lifecycle-stop:' + canonical_hash({'command_ref':command_ref,'lifecycle_id':lifecycle_id})
        if tx.get('lifecycle_stop_requests',key):
            return
        request = command | {'command_ref':command_ref,'lifecycle_id':lifecycle_id,
            'scope_kind':body['key']['scope_kind'],'scope_ref':body['key']['scope_ref'],
            'observed_revision':body['revision'],'observed_phase':body['phase']}
        from ..domain.lifecycle_stop import StopTarget
        if command.get('schema_version')==1:
            request=StopTarget(**request).model_dump(mode='json')
        tx.put('lifecycle_stop_requests',key,request,immutable=True)
        tx.put('lifecycle_stop_status',key,{'status':'pending'})
        row = tx.get('run_stop_targets',command['run_ref'])
        refs = set(row['body']['refs'] if row else ()) | {key}
        tx.put('run_stop_targets',command['run_ref'],{'refs':sorted(refs)})
        tx.put('outbox',key,{'kind':'lifecycle_stop_requested','request_ref':key},immutable=True)

    @classmethod
    def attach_lifecycle_in(cls, tx, lifecycle_id, body):
        index = tx.get('run_stop_index',body['key']['run_ref'])
        for ref in index['body']['refs'] if index else ():
            command = tx.get('run_stop_requests',ref)
            if command:
                cls.attach_target_in(tx,ref,command['body'],lifecycle_id,body)

    @classmethod
    def migrate_in(cls, tx, *, cursor='', limit=100):
        if not 1 <= limit <= 1000:
            raise ValueError('bounded stop migration batch required')
        rows = [r for r in tx.scan('run_stop_requests') if r['key'] > cursor]
        selected = rows[:limit]
        for row in selected:
            cls.expand_in(tx,row['key'],row['body'])
        state = {'cursor':selected[-1]['key'] if selected else cursor,'complete':len(rows)<=limit}
        tx.put('stop_migration','v1',state)
        return state

    @staticmethod
    def commands_in(tx, record):
        index=tx.get('run_stop_index',record.key.run_ref)
        rows=[tx.get('run_stop_requests',ref) for ref in index['body']['refs']] if index else tx.scan('run_stop_requests')
        return [r['body'] for r in rows if r and
            (not tx.get('lifecycle_stop_scopes',r['key']) or
             tx.get('lifecycle_stop_scopes',r['key'])['body']['lifecycle_id']==record.key.lifecycle_id) and
            (r['body']['run_ref'],r['body']['plan_ref'],r['body']['graph_ref']) == (
                record.key.run_ref,record.plan_ref,record.graph_ref)]

    @classmethod
    def policy_in(cls, tx, record):
        commands = cls.commands_in(tx,record)
        return ('cancel' if any(c['policy']=='cancel' for c in commands) else 'drain') if commands else None

    @classmethod
    def assert_admission_in(cls, tx, record):
        if cls.commands_in(tx,record):
            raise Conflict('lifecycle stop requested; only original safety continuation allowed')

    @staticmethod
    def original_deadline_in(tx, record, *, operation_kind=None, target=None):
        # The original submitted request can expire before its supervisor.
        # Never extend either deadline when a version is drained.
        deadlines = [record.deadline]
        kind = operation_kind or record.operation_kind
        refs = (target,) if target is not None else record.operation_refs
        for ref in refs:
            row = tx.get('agent_provider_requests' if kind=='provider' else 'operations',ref)
            if row:
                deadline = row['body'].get('envelope' if kind=='provider' else 'request',{}).get('deadline')
                if deadline is not None:
                    deadlines.append(deadline)
        return min(deadlines)

    @classmethod
    def stop_allowed_in(cls, tx, record, *, operation_kind, target):
        if cls.policy_in(tx,record) != 'drain' or record.stop_reason is not None:
            return True
        if tx.now >= cls.original_deadline_in(tx,record,operation_kind=operation_kind,target=target):
            return True
        # Stopping a sampler after its business operation completes is mandatory
        # disposition, not cancellation of the in-flight business operation.
        if operation_kind == 'site':
            from .lifecycle_safety import LifecycleSafetyService
            row = tx.get('operations',target)
            return bool(row and LifecycleSafetyService._operation_closed(row['body']))
        return False

    @staticmethod
    def successor_in(tx, request):
        if not request['request_id'].startswith('revision:'):
            return None
        activation = tx.get('revision_activations',request['request_id'].removeprefix('revision:'))
        expected = {k:request[k] for k in ('run_ref','plan_ref','graph_ref')}
        if not activation or activation['body'].get('predecessor') != expected:
            return None
        receipt = activation['body']
        result = receipt['result']
        current = tx.get('runs',result['run_id'])
        if receipt['policy'] != request['policy'] or not current or current['body']['graph_ref'] != result['graph_ref']:
            return None
        if result['run_id'] == request['run_ref']:
            return result['run_id'] if (result['graph_ref']==request['graph_ref']
                and result['active_plan_ref']!=request['plan_ref']) else None
        old = tx.get('runs',request['run_ref'])
        if (old and old['body'].get('successor_run_id')==result['run_id']
                and result.get('supersedes_run_id')==request['run_ref']):
            return result['run_id']
        return None

    @staticmethod
    def inventory_in(tx, record):
        from .lifecycle_inventory import LifecycleInventory
        snapshot = LifecycleInventory.collect_in(tx, run_ref=record.key.run_ref,
            scope_kind=record.key.scope_kind, scope_ref=record.key.scope_ref)
        names = {'operations':'operations', 'jobs':'jobs', 'providers':'agent_provider_requests',
            'resources':'resources', 'observations':'observations', 'leases':'resource_leases',
            'work_leases':'work_execution_leases', 'isolations':'repair_isolations',
            'candidate_isolations':'candidate_isolations'}
        material = {name:snapshot.rows_in(tx,kind,open_only=name!='operations') for name,kind in names.items()}
        material['ambiguous'] = [{'key':i.kind + ':' + i.key, 'body':{'reasons':list(i.reasons)}}
            for i in snapshot.items if i.ownership=='ambiguous']
        return material

    @classmethod
    def consume_in(cls, tx, intent_ref, body):
        from ..domain.agent_lifecycle import LifecycleEvent, TransitionGuards, transition_lifecycle
        from ..persistence.repository import LeaseToken, StaleOwner
        from .agent_lifecycle import LifecycleService
        from .lifecycle_safety import LifecycleSafetyService
        request = tx.get('lifecycle_stop_requests',body['request_ref'])
        route = tx.get('outbox',intent_ref)
        if not request or (body['request_ref']!=intent_ref and (not route or route['body']!=body)):
            raise Conflict('stop intent has no immutable command')
        intent_ref = body['request_ref']
        request = request['body']
        command = tx.get('run_stop_requests',request['command_ref'])
        if not command:
            raise Conflict('stop run command missing')
        record = LifecycleService.load_in(tx,request['lifecycle_id'])
        if (record.key.run_ref,record.plan_ref,record.graph_ref,record.key.scope_ref) != (
                request['run_ref'],request['plan_ref'],request['graph_ref'],request['scope_ref']):
            raise Conflict('stop original lifecycle identity changed')
        def waiting(reason, inventory=None):
            status = {'status':'waiting_dependency','reason':reason,
                'lifecycle_id':record.key.lifecycle_id,'request_ref':intent_ref,
                'recovery_entry':'replay original stop outbox after owner/safety evidence closes'}
            if inventory is not None:
                status['remaining'] = {k:[r['key'] for r in rows] for k, rows in inventory.items()}
            tx.put('lifecycle_stop_status',intent_ref,status)
            tx.put('outbox_errors',intent_ref,status)
            return False
        owner = tx.get('lifecycle_owners',record.key.lifecycle_id)
        if owner:
            saved=owner['body']
            try:
                tx.assert_fence(LeaseToken(saved['thread_id'],saved['owner_epoch'],saved['lease_id'],0))
            except StaleOwner:
                pass
            else:
                return waiting('original lifecycle owner still active')
        material = cls.inventory_in(tx,record)
        if material['ambiguous']:
            return waiting('ambiguous effect ownership requires original identity evidence',material)
        if any(r['body']['envelope']['lifecycle_id'] != record.key.lifecycle_id
               for r in material['providers']):
            return waiting('child provider requires its original lifecycle reconciliation',material)
        source = tx.get('lifecycle_sources',record.key.lifecycle_id)
        if not source:
            return waiting('original lifecycle source missing')
        try:
            token = tx.acquire_thread(source['body']['identity']['thread_id'],'run-stop',30)
        except Conflict as exc:
            return waiting(str(exc))
        try:
            LifecycleService._assert_scope_token(tx,source['body']['identity'],token)
            tx.put('lifecycle_owners',record.key.lifecycle_id,LifecycleService._owner_body(token))
            return cls._advance_in(tx,intent_ref,request,record,token,waiting)
        finally:
            tx.release_thread(token)

    @classmethod
    def _advance_in(cls,tx,intent_ref,request,record,token,waiting):
        from ..domain.agent_lifecycle import LifecycleEvent, TransitionGuards, transition_lifecycle
        from .agent_lifecycle import LifecycleService
        from .lifecycle_safety import LifecycleSafetyService
        tx.assert_fence(token)
        material = cls.inventory_in(tx,record)
        if material['ambiguous']:
            return waiting('ambiguous effect ownership requires original identity evidence',material)
        if any(r['body']['envelope']['lifecycle_id'] != record.key.lifecycle_id
               for r in material['providers']):
            # Aggregation is not authority to adopt a child's original request,
            # lifecycle budget or reconciliation grant.
            return waiting('child provider requires its original lifecycle reconciliation',material)
        known_ops={r['key'] for r in material['operations']}
        if (record.operation_kind=='site' and not set(record.operation_refs)<=known_ops
                or record.operation_kind=='provider' and any(
                    not tx.get('agent_provider_requests',ref) for ref in record.operation_refs)):
            return waiting('original operation/provider inventory missing',material)
        if (record.node_result.error is not None and record.node_result.error.effect_certainty=='unknown'
                and not material['operations'] and not record.operation_refs
                and not material['providers']):
            return waiting('unknown source effect lacks original operation identity',material)
        uncertain = [r for r in material['operations'] if not LifecycleSafetyService._operation_closed(r['body'])]
        has_effects = bool(uncertain or material['providers'] or material['observations'] or material['resources'])
        policy = cls.policy_in(tx,record)
        deadline = min([cls.original_deadline_in(tx,record), *(
            cls.original_deadline_in(tx,record,operation_kind=kind,target=row['key'])
            for kind,rows in (('provider',material['providers']),('site',uncertain)) for row in rows)])
        stop_kind = 'cancel' if policy=='cancel' else (
            'deadline' if tx.now>=deadline else None)
        # Persist the actual stopping cause before safety continuation finishes.
        # This control-plane transition never executes effects or takes over a live owner.
        if (not record.is_terminal and stop_kind is not None and record.stop_reason!='explicit_cancel'
                and not (stop_kind=='deadline' and record.stop_reason=='deadline')):
            kind, refs = record.operation_kind, record.operation_refs
            if not refs:
                if material['providers']:
                    kind, refs = 'provider', tuple(r['key'] for r in material['providers'])
                elif material['operations']:
                    kind, refs = 'site', tuple(r['key'] for r in material['operations'])
            if has_effects and not refs:
                return waiting('unknown effects lack original operation identity',material)
            isolation_refs = record.isolation_refs
            if has_effects and not isolation_refs:
                isolation_ref = 'stop-isolation:' + canonical_hash({'request_ref':intent_ref})
                tx.put('lifecycle_stop_isolations',isolation_ref,{'request_ref':intent_ref,
                    'lifecycle_id':record.key.lifecycle_id,'inventory_hash':canonical_hash(material)},immutable=True)
                isolation_refs = (isolation_ref,)
            event = LifecycleEvent(schema_version=1,event_id=intent_ref + ':' + stop_kind,kind=stop_kind,
                source='trusted_run_control',expected_revision=record.revision,
                evidence_refs=(intent_ref,),observed_at=tx.now,operation_kind=kind,
                operation_refs=refs,isolation_refs=isolation_refs)
            from .lifecycle_budgets import LifecycleBudgetService
            try:
                bounds = LifecycleBudgetService.snapshot_in(tx,record.key.lifecycle_id).values()
                safety_deadline = min(b['safety_deadline'] for b in bounds)
                safety_available = all(any(b['safety_limits'].get(k,0) >
                    b['safety_spent'].get(k,0)+b['safety_reserved'].get(k,0)
                    for k in ('query_calls','stop_calls','cleanup_calls')) for b in bounds)
            except (Conflict,KeyError):
                safety_deadline, safety_available = None, False
            guards = TransitionGuards(schema_version=1,effect_known=not has_effects,
                effects_in_flight=has_effects,disposition_closed=not has_effects,
                safety_budget_available=safety_available,safety_deadline=safety_deadline)
            # No safety call is authorized or spent here. Actual reconciliation
            # still uses its original policy/grant/deadline/both budgets.
            updated = transition_lifecycle(record,event,guards)
            record = LifecycleService.persist_transition_in(tx,record,event,updated)
        if (has_effects or material['jobs'] or material['isolations'] or material['leases']
                or material['work_leases'] or material['candidate_isolations']):
            return waiting('original work/provider/cleanup or isolation remains open',material)
        if record.key.scope_kind=='work':
            snapshot=LifecycleSafetyService._snapshot_in(tx,record.key.scope_ref)
            if not LifecycleSafetyService._closure_valid(snapshot):
                return waiting('work attempt/sampler closure proof missing',material)
        from .lifecycle_disposition import DispositionVersions
        proof = {'request_ref':intent_ref,'lifecycle_id':record.key.lifecycle_id,
            'attempt_revision':DispositionVersions.current_in(tx,record.key.run_ref),
            'inventory':material,'inventory_hash':canonical_hash(material),'observed_at':tx.now,
            'owner_epoch':token.owner_epoch,'lease_id':token.lease_id,'thread_id':token.thread_id}
        successor = cls.successor_in(tx,request) if policy=='drain' else None
        if policy=='drain' and not successor:
            return waiting('verified original version successor missing',material)
        proof['successor_ref'] = successor
        proof_ref = 'stop-proof:' + canonical_hash(proof)
        if not record.is_terminal:
            if policy!='drain' and record.phase!='cancelling':
                return waiting('original lifecycle requires explicit safety disposition',material)
            owns_work = False
            try:
                if record.key.scope_kind=='work':
                    LifecycleSafetyService.acquire_work_in(tx,record.key.scope_ref,token,purpose='cleanup')
                    owns_work = True
                    LifecycleSafetyService.assert_work_in(tx,record.key.scope_ref,token)
                tx.put('lifecycle_owners',record.key.lifecycle_id,LifecycleService._owner_body(token))
                event = LifecycleEvent(schema_version=1,event_id=intent_ref + ':closed',
                    kind='version_drained' if policy=='drain' else 'disposition_closed',
                    source='trusted_run_control',expected_revision=record.revision,
                    evidence_refs=(proof_ref,),observed_at=tx.now)
                guards = TransitionGuards(schema_version=1,work_lease_valid=True,disposition_closed=True,
                    effect_known=True,effects_in_flight=False,successor_bound=bool(successor))
                updated = transition_lifecycle(record,event,guards).model_copy(update={'owner_epoch':token.owner_epoch})
                LifecycleService.persist_transition_in(tx,record,event,updated)
            except Conflict as exc:
                return waiting(str(exc),material)
            finally:
                if owns_work:
                    LifecycleSafetyService.release_work_in(tx,record.key.scope_ref,token)
        from .lifecycle_inventory import LifecycleInventory
        snapshot = LifecycleInventory.collect_in(tx,run_ref=record.key.run_ref,
            scope_kind=record.key.scope_kind,scope_ref=record.key.scope_ref)
        proof.update(disposition_revision=DispositionVersions.current_in(tx,record.key.run_ref),
            inventory_digest=snapshot.digest,command_hash=canonical_hash(request),
            producer='lifecycle_stop.v1')
        from ..domain.lifecycle_stop import StopProof
        from .agent_lifecycle import decode
        proof=decode(StopProof,proof|{'schema_version':1,'proof_ref':proof_ref}).model_dump(mode='json')
        tx.assert_fence(token)
        tx.put('lifecycle_stop_proofs',proof_ref,proof,immutable=True)
        tx.put('lifecycle_stop_status',intent_ref,{'status':'closed','proof_ref':proof_ref,
            'lifecycle_id':record.key.lifecycle_id})
        return True
