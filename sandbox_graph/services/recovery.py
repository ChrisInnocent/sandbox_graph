"""Fenced recovery compilation and startup verification against durable facts.

Plans are immutable. Context snapshots retain the source attempt's provenance;
no plan grants a new measurement generation or changes a budget ledger.
"""
from __future__ import annotations

from ..catalog.registry import NODE_GROUPS
from ..domain.agent_lifecycle import RecoveryPlan
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from .agent_lifecycle import LifecycleService, decode
from .lifecycle_safety import LifecycleSafetyService

# The WorkGraph builder verifies this contract before executing a recovery.
WORK_RECOVERY_CONTRACT = {
    'version': 4, 'nodes': NODE_GROUPS['work'], 'entry': 'N00',
    'successors': {'N00':'N01','N01':'N02','N02':'N03','N03':'N04',
                   'N04':'N05','N05':'N06','N06':'N07','N07':'N08',
                   'N08':'N09','N09':'N10','N10':'__end__'},
    'failure': {'N00':'N07','N01':'N07','N02':'N07','N03':'N07',
                'N04':'N07','N05':'N07','N06':'N07','N07':'N09',
                'N08':'N09','N09':'N10','N10':'__end__'},
    'recovery': {'N00':('N00','N01','N02','N03'), 'N01':('N01','N02','N03'),
                 'N02':('N01','N02','N03'), 'N03':('N01','N02','N03'),
                 'N04':('N07',),'N05':('N07',),'N06':('N07',),'N07':('N07',),
                 'N08':('N07','N08'),'N09':('N07','N09'),'N10':('N07','N09','N10')},
    'measurement': ('N04','N05','N06'), 'tail': ('N07','N08','N09','N10'),
}


class RecoveryService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _input_evidence_in(tx, input_refs):
        # Missing fixture evidence never grants reuse: preparation is mandatory.
        # Existing referenced records are version-bound even when refs are stable.
        kinds = ('artifacts','resources','fixtures','target_bindings','inputs',
                 'environment_evidence','plans','contracts')
        return canonical_hash({ref:{kind:tx.get(kind,ref) for kind in kinds}
                               for ref in sorted(set(input_refs.values()))})

    @staticmethod
    def _versions_in(tx, work):
        graph = tx.get('graph_definitions', work['graph_ref'])
        graph_hash = canonical_hash({'graph_ref': work['graph_ref'],
            'definition': graph['body'] if graph else None, 'runtime': WORK_RECOVERY_CONTRACT})
        contract = tx.get('contracts', work['contract_ref'])
        binding = tx.get('measurement_bindings', work['work_id'])
        if contract:
            contract_hash = canonical_hash(contract['body'])
        elif binding and binding['body'].get('measurement_contract_hash'):
            contract_hash = binding['body']['measurement_contract_hash']
        else:
            raise Conflict('recovery requires frozen contract content or measurement binding')
        return graph_hash, contract_hash, canonical_hash(binding) if binding else None

    @staticmethod
    def _authority_in(tx, record, order):
        from .lifecycle_stop import LifecycleStopService
        LifecycleStopService.assert_admission_in(tx,record)
        if (record.phase != 'reentering' or record.terminal_intent is not None
                or tx.now >= min(record.deadline, order['deadline'])):
            raise Conflict('lifecycle does not authorize recovery')
        run, gate = tx.get('runs', order['run_ref']), tx.get('dispatch_gates', order['run_ref'])
        auth = tx.get('repair_authorizations', order['authorization_ref'])
        if (not run or not gate or gate['body'].get('open') is not True
                or gate['body'].get('generation') != order['dispatch_generation']
                or run['body'].get('dispatch_generation') != order['dispatch_generation']
                or run['body'].get('graph_ref') != order['graph_ref']
                or run['body'].get('active_plan_ref') != order['plan_ref']):
            raise Conflict('recovery dispatch version or gate changed')
        if (not auth or auth['revision'] != order['authorization_revision']
                or auth['body'].get('revoked') is not False
                or auth['body'].get('expires_at', 0) <= tx.now
                or any(auth['body'].get(k) != order[k] for k in
                       ('principal','run_ref','plan_ref','graph_ref','contract_ref'))
                or auth['body'].get('scope_ref') != order['work_ref']
                or auth['body'].get('work_revision') != order['work_revision']
                or order['action_ref'] not in auth['body'].get('actions', ())):
            raise Conflict('recovery authority expired or changed')

    @staticmethod
    def _receipt_in(tx, ref, order, *, node=None, source_attempt=None):
        row = tx.get('receipts', ref)
        if not row:
            raise Conflict('recovery receipt missing')
        identity = row['body'].get('identity', {})
        expected = {'scope': order['work_ref'], 'run_ref': order['run_ref'],
                    'plan_ref': order['plan_ref'], 'graph_ref': order['graph_ref']}
        if node is not None: expected['node_id'] = node
        if source_attempt is not None: expected['activation_id'] = source_attempt
        if (any(identity.get(k) != v for k,v in expected.items())
                or identity.get('input_refs') != order['input_refs']):
            raise Conflict('recovery receipt provenance changed')
        return row

    @classmethod
    def compile_recovery_in(cls, tx, lifecycle_id, receipt_refs=None, *, token, catalog=None):
        LifecycleService.assert_owner_in(tx, lifecycle_id, token)
        record = LifecycleService.load_in(tx, lifecycle_id)
        if record.key.scope_kind != 'work': raise Conflict('WorkGraph recovery requires work lifecycle')
        LifecycleSafetyService.assert_work_in(tx, record.key.scope_ref, token)
        if not record.attempt_refs: raise Conflict('accepted repair attempt required')
        order_ref = record.attempt_refs[-1]
        row, outcome_row = tx.get('repair_orders', order_ref), tx.get('repair_outcomes', order_ref)
        if not row or not outcome_row: raise Conflict('accepted repair outcome missing')
        order, outcome = row['body'], outcome_row['body']
        if outcome.get('postcheck_status')=='unknown':
            from .lifecycle_reconciliation import LifecycleReconciliationService
            outcome=LifecycleReconciliationService.accepted_outcome_in(tx,order,outcome,token=token,catalog=catalog)
        if (order['lifecycle_id'] != lifecycle_id or order['node_id'] != record.key.node_id
                or order['source_receipt'] != record.source_receipt
                or (order['work_ref'], order['run_ref'], order['plan_ref'], order['graph_ref'], order['contract_ref']) !=
                   (record.key.scope_ref, record.key.run_ref, record.plan_ref, record.graph_ref, record.contract_ref)
                or outcome.get('postcheck_status') != 'accepted'
                or outcome.get('interrupted') is not False
                or outcome['outcome']['effect_certainty'] == 'unknown'
                or outcome['outcome']['order_ref'] != order_ref):
            raise Conflict('repair outcome is not accepted for this lifecycle')
        cls._authority_in(tx, record, order)
        work = tx.get('work_instances', order['work_ref'])
        if not work or work['revision'] != order['work_revision']:
            raise Conflict('recovery work revision changed')
        if any(work['body'].get(k) != order[v] for k,v in
               (('run_id','run_ref'),('plan_ref','plan_ref'),('graph_ref','graph_ref'),('contract_ref','contract_ref'))):
            raise Conflict('recovery work identity changed')
        source = cls._receipt_in(tx, record.source_receipt, order, node=record.key.node_id)
        attempt = source['body']['identity']['activation_id']
        refs = dict(order['result_refs'])
        if receipt_refs is not None and dict(receipt_refs) != refs:
            raise Conflict('caller cannot replace frozen recovery result refs')
        supplied_refs = dict(refs)
        refs[record.key.node_id] = record.source_receipt
        original = {n: cls._receipt_in(tx, ref, order, node=n)
                    for n,ref in refs.items()}
        if set(original) - set(NODE_GROUPS['work']): raise Conflict('unknown source work node')
        repaired_ref = outcome['return_result']['output']['receipt_ref']
        repaired = cls._receipt_in(tx, repaired_ref, order, node=record.key.node_id)
        review = tx.get('node_reviews', repaired_ref)
        if (repaired['body']['result']['execution_status'] != 'succeeded'
                or repaired['body']['result'].get('review_status') == 'rejected'
                or not review or review['body']['decision'] != 'accept'):
            raise Conflict('accepted repair receipt no longer verifies')
        reconcile_operation_refs = []
        reconcile_operation_identities = {}
        if record.key.node_id == 'N01':
            execution = tx.get('repair_executions', order_ref)
            reconcile_operation_refs = sorted(set(repaired['body']['result']['operation_refs']))
            inventory = execution['body']['operation_refs'] if execution else ()
            if not reconcile_operation_refs or not set(reconcile_operation_refs).issubset(inventory):
                raise Conflict('accepted reconciliation is outside the original operation inventory')
            reconcile_operation_identities = {ref:cls._operation_identity_in(tx, ref)
                                               for ref in reconcile_operation_refs}
            for operation_ref in reconcile_operation_refs:
                operation = tx.get('operations', operation_ref)
                if (not operation or operation['body'].get('scope') != order['work_ref']
                        or operation['body'].get('run_id') != order['run_ref']):
                    raise Conflict('original reconciliation operation identity missing or changed')
        graph_hash, contract_hash, binding_hash = cls._versions_in(tx, work['body'])
        measured = any(n in original for n in ('N04','N05','N06'))
        measured = measured or any(r['body']['result'].get('performance_status') == 'fail'
            for r in original.values())
        # A partial attempt can have operations but no receipt (response lost).
        measured = measured or bool(LifecycleSafetyService._rows_in(tx, 'observations', order['work_ref']))
        measured = measured or any(r['body'].get('node_id') in {'N04','N05','N06'} or
            r['body'].get('request', {}).get('node_id') in {'N04','N05','N06'} or
            not (r['body'].get('node_id') or r['body'].get('request', {}).get('node_id'))
            for r in LifecycleSafetyService._rows_in(tx, 'operations', order['work_ref']))
        measured = measured or any(r['body'].get('identity', {}).get('activation_id') == attempt and
            r['body'].get('identity', {}).get('scope') == order['work_ref'] and
            r['body'].get('identity', {}).get('node_id') in {'N04','N05','N06'} for r in tx.scan('receipts'))
        prefix = WORK_RECOVERY_CONTRACT['recovery'][record.key.node_id]
        if record.key.node_id in {'N00','N01','N02','N03'}:
            route = prefix + (() if measured else ('N04','N05','N06')) + ('N07','N08','N09','N10')
            revalidate = ('N01','N02','N03')
            invalid_nodes = set(prefix) | ({'N04','N05','N06'} if not measured else set()) | {'N07','N08','N09','N10'}
        else:
            tails = {'N04':('N07','N08','N09','N10'), 'N05':('N07','N08','N09','N10'),
                'N06':('N07','N08','N09','N10'), 'N07':('N07','N08','N09','N10'),
                'N08':('N07','N08','N09','N10'), 'N09':('N07','N09','N10'), 'N10':('N07','N09','N10')}
            route, revalidate = tails[record.key.node_id], ()
            invalid_nodes = set(route)
            if record.key.node_id == 'N08':
                seal = original.get('N07', {}).get('body', {}).get('result', {})
                if (seal.get('execution_status') != 'succeeded' or seal.get('data_status') != 'valid'
                        or not seal.get('artifact_refs')):
                    raise Conflict('analysis recovery requires original frozen raw evidence')
        # Sealing for this attempt's closure must not replace an already
        # frozen analysis input. Bind both its receipt and immutable raw refs.
        analysis_dependency = None
        if 'N08' in route:
            sealed_ref = refs.get('N07')
            sealed_row = original.get('N07')
            sealed_result = sealed_row['body']['result'] if sealed_row else {}
            if (sealed_result.get('execution_status') == 'succeeded'
                    and sealed_result.get('data_status') == 'valid' and sealed_result.get('artifact_refs')):
                analysis_dependency = {'receipt_ref':sealed_ref,
                    'receipt_hash':canonical_hash(sealed_row),
                    'artifact_refs':sorted(set(sealed_result['artifact_refs']))}
        # Preserve failed/interrupted measurement receipts as provenance, never as
        # permission to sample. Summaries retain their original data/performance.
        reuse = {n:ref for n,ref in refs.items() if n not in invalid_nodes}
        if record.key.node_id in {'N07','N08','N09','N10'}:
            reuse[record.key.node_id] = repaired_ref
        # Lifetime comes from the accepted repair's trusted disposition, never
        # a compiler constant or an extension of its predecessor's permission.
        proof_expiries = []
        disposition_refs=([outcome['reconciliation_disposition_ref']]
            if outcome.get('reconciliation_disposition_ref') else outcome['outcome']['raw_refs'])
        for raw_ref in disposition_refs:
            evidence = tx.get('resource_disposition_receipts', raw_ref)
            if evidence:
                body = evidence['body']
                if (raw_ref != 'disposition:' + canonical_hash(body)
                        or body['identity']['scope'] != order['work_ref']):
                    raise Conflict('repair disposition evidence identity changed')
                proof_expiries.append(body['receipt']['expires_at'])
        if not proof_expiries:
            raise Conflict('accepted repair disposition lifetime missing')
        expiry = min(*proof_expiries, record.deadline, order['deadline'],
            tx.get('repair_authorizations', order['authorization_ref'])['body']['expires_at'])
        if expiry <= tx.now:
            raise Conflict('accepted repair disposition expired; fresh reconciliation required')
        previous = tx.get('recovery_order_plans', order_ref)
        if previous:
            previous_plan, previous_context = cls._load_in(tx, previous['body']['plan_ref'])
            if (previous_plan.work_revision == work['revision']
                    and (previous_plan.graph_hash, previous_plan.contract_hash, previous_context['binding_hash']) ==
                        (graph_hash, contract_hash, binding_hash)
                    and previous_context['input_evidence_hash'] == cls._input_evidence_in(tx, order['input_refs'])
                    and previous_context['proof_expires_at'] > tx.now
                    and previous_context['resource_snapshot_hash'] == canonical_hash(
                        LifecycleSafetyService._snapshot_in(tx, order['work_ref']))
                    and all(canonical_hash(tx.get('receipts', ref)) == digest
                            for ref,digest in previous_context['receipt_hashes'].items())):
                return previous_plan
        proof = LifecycleSafetyService.produce_disposition_in(tx, order['work_ref'], token,
            ttl=expiry - tx.now)
        disposition = LifecycleSafetyService.verify_disposition_in(tx, proof, order['work_ref'], token,
            require_closed=bool(set(route) & {'N00','N01','N02','N03','N04','N05','N06'}))
        if not disposition.stopped or disposition.sampler_status not in {'stopped','not_started'}:
            raise Conflict('prior attempt or sampler is not stopped')
        generation = work['body'].get('key', {}).get('measurement_generation', 1)
        context = {'schema_version':1, 'lifecycle_id':lifecycle_id, 'order_ref':order_ref,
            'scope':order['work_ref'], 'run_ref':order['run_ref'], 'plan_ref':order['plan_ref'],
            'input_refs':dict(order['input_refs']), 'result_refs':reuse, 'source_result_refs':refs,
            'source_receipt':record.source_receipt, 'source_node':record.key.node_id, 'repair_receipt':repaired_ref,
            'reconcile_operation_refs':reconcile_operation_refs,
            'reconcile_operation_identities':reconcile_operation_identities,
            'analysis_dependency':analysis_dependency,
            'input_evidence_hash':cls._input_evidence_in(tx, order['input_refs']),
            'receipt_hashes':{ref:canonical_hash(r) for ref,r in [(record.source_receipt,source),
                (repaired_ref,repaired), *((refs[n],r) for n,r in original.items())]},
            'route':list(route), 'measurement_blocked':measured or not bool(set(route)&{'N04','N05','N06'}),
            'binding_hash':binding_hash, 'proof_expires_at':disposition.expires_at,
            'resource_snapshot_hash':canonical_hash(LifecycleSafetyService._snapshot_in(tx, order['work_ref']))}
        fields = dict(schema_version=1, graph_ref=order['graph_ref'], graph_hash=graph_hash,
            contract_ref=order['contract_ref'], contract_hash=contract_hash,
            input_hash=canonical_hash(order['input_refs']), work_revision=work['revision'],
            source_attempt=attempt, entry_node=route[0], revalidate_nodes=revalidate,
            reuse_receipts=tuple(reuse.values()), invalidated_refs=tuple(dict.fromkeys([ref for n,ref in supplied_refs.items() if n in invalid_nodes] + [ref for n,ref in refs.items() if n in invalid_nodes])),
            resource_proofs=(proof,), measurement_generation=generation)
        ref = 'recovery:' + canonical_hash({'plan':fields,'context':context})
        plan = RecoveryPlan(ref=ref, **fields)
        tx.put('recovery_plans', ref, plan.model_dump(mode='json'), immutable=True)
        tx.put('recovery_contexts', ref, context, immutable=True)
        tx.put('recovery_order_plans', order_ref, {'plan_ref':ref})
        return plan

    def compile_recovery(self, lifecycle_id, receipt_refs=None, *, token):
        with self.repository.transaction() as tx:
            return self.compile_recovery_in(tx, lifecycle_id, receipt_refs, token=token)

    @classmethod
    def _load_in(cls, tx, ref):
        row, ctx = tx.get('recovery_plans', ref), tx.get('recovery_contexts', ref)
        if not row or not ctx: raise Conflict('compiled recovery plan missing')
        plan, context = decode(RecoveryPlan, row['body']), ctx['body']
        fields = plan.model_dump(mode='json', exclude={'ref'})
        if ref != 'recovery:' + canonical_hash({'plan':fields, 'context':context}):
            raise Conflict('compiled recovery plan content changed')
        return plan, context

    @classmethod
    def revalidate_in(cls, tx, ref, *, scope, run_ref, plan_ref, graph_ref, input_refs, activation_id, token):
        plan, context = cls._load_in(tx, ref)
        LifecycleSafetyService.assert_work_in(tx, scope, token)
        record = LifecycleService.load_in(tx, context['lifecycle_id'])
        order = tx.get('repair_orders', context['order_ref'])['body']
        cls._authority_in(tx, record, order)
        if (scope,run_ref,plan_ref,graph_ref,input_refs) != (
                context['scope'],context['run_ref'],context['plan_ref'],plan.graph_ref,context['input_refs']):
            raise Conflict('activation differs from compiled recovery identity')
        work = tx.get('work_instances', scope)
        if not work or work['revision'] != plan.work_revision:
            raise Conflict('recovery work revision changed')
        graph_hash, contract_hash, binding_hash = cls._versions_in(tx, work['body'])
        if (graph_hash,contract_hash,binding_hash,canonical_hash(input_refs)) != (
                plan.graph_hash,plan.contract_hash,context['binding_hash'],plan.input_hash):
            raise Conflict('recovery version or inputs changed')
        if cls._input_evidence_in(tx, input_refs) != context['input_evidence_hash']:
            raise Conflict('recovery referenced input version changed')
        if tx.now >= context['proof_expires_at']:
            raise Conflict('recovery resource evidence expired; recompile dependencies')
        if canonical_hash(LifecycleSafetyService._snapshot_in(tx, scope)) != context['resource_snapshot_hash']:
            raise Conflict('recovery resource evidence changed; recompile dependencies')
        for receipt, digest in context['receipt_hashes'].items():
            if canonical_hash(tx.get('receipts', receipt)) != digest:
                raise Conflict('recovery receipt evidence changed')
        # The predecessor's proof is evidence only. Mint and verify permission
        # using the live owner, the live ledger, and the original expiry bound.
        proof = LifecycleSafetyService.produce_disposition_in(tx, scope, token,
            ttl=context['proof_expires_at'] - tx.now)
        disposition = LifecycleSafetyService.verify_disposition_in(tx, proof, scope, token,
            require_closed=bool(set(context['route']) & {'N00','N01','N02','N03','N04','N05','N06'}))
        if not disposition.stopped or disposition.sampler_status not in {'stopped','not_started'}:
            raise Conflict('recovery prior attempt is not conclusively stopped')
        claim = tx.get('recovery_plan_activations', ref)
        identity = {'activation_id':activation_id, 'thread_id':token.thread_id}
        if claim and claim['body'] != identity: raise Conflict('recovery plan already assigned to another activation')
        tx.put('recovery_plan_activations', ref, identity, immutable=True)
        tx.put('recovery_starts', activation_id, {'plan_ref':ref,
            'owner':LifecycleService._owner_body(token), 'disposition_ref':proof})
        return context

    @classmethod
    def assert_execution_in(cls, tx, ref, state, token, *, drain=False):
        plan, context = cls._load_in(tx, ref)
        LifecycleSafetyService.assert_work_in(tx, state['scope'], token)
        started = tx.get('recovery_starts', state['activation_id'])
        if (not started or started['body']['plan_ref'] != ref
                or started['body']['owner'] != LifecycleService._owner_body(token)):
            raise Conflict('recovery current execution owner changed')
        if (state['scope'],state['run_ref'],state['plan_ref'],state['graph_ref'],state['input_refs']) != (
                context['scope'],context['run_ref'],context['plan_ref'],plan.graph_ref,context['input_refs']):
            raise Conflict('recovery execution scope changed')
        if 'recovery_route' in state and state['recovery_route'] and state['recovery_route'] != context['route']:
            raise Conflict('recovery execution route changed')
        if not drain:
            cls._authority_in(tx, LifecycleService.load_in(tx, context['lifecycle_id']),
                              tx.get('repair_orders', context['order_ref'])['body'])
        work = tx.get('work_instances', context['scope'])
        if (not work or (not drain and work['revision'] != plan.work_revision)
                or any(work['body'].get(k) != value for k,value in {
                    'run_id':context['run_ref'], 'plan_ref':context['plan_ref'],
                    'graph_ref':plan.graph_ref, 'contract_ref':plan.contract_ref}.items())
                or cls._versions_in(tx, work['body']) !=
                   (plan.graph_hash, plan.contract_hash, context['binding_hash'])
                or state['input_refs'] != context['input_refs']):
            raise Conflict('recovery execution versions changed')

    @staticmethod
    def _operation_identity_in(tx, operation_ref):
        row = tx.get('operations', operation_ref)
        if not row:
            return None
        body = row['body']
        return canonical_hash({key:body.get(key) for key in ('scope','run_id','request_hash','request')})

    @classmethod
    def reentry_dependency_error_in(cls, tx, ref, *, node_id, state, token, receipt_ref=None):
        """A dependency failure blocks business continuation, not safe closure."""
        cls.assert_execution_in(tx, ref, state, token, drain=True)
        _, context = cls._load_in(tx, ref)
        if node_id == 'N01' and context['reconcile_operation_refs']:
            receipt = tx.get('receipts', receipt_ref) if receipt_ref else None
            refs = receipt['body']['result'].get('operation_refs', ()) if receipt else ()
            if (not refs or not set(refs).issubset(context['reconcile_operation_refs'])
                    or any(cls._operation_identity_in(tx, operation) != digest
                           for operation,digest in context['reconcile_operation_identities'].items())):
                return 'original_operation_mismatch'
        if node_id == 'N08' and context['analysis_dependency']:
            dependency = context['analysis_dependency']
            if canonical_hash(tx.get('receipts', dependency['receipt_ref'])) != dependency['receipt_hash']:
                return 'analysis_raw_dependency_changed'
            current_ref = state.get('result_refs', {}).get('N07')
            current = tx.get('receipts', current_ref) if current_ref else None
            identity = current['body'].get('identity', {}) if current else {}
            expected = {key:state[key] for key in ('thread_id','activation_id','scope',
                'run_ref','plan_ref','graph_ref','input_refs')}
            expected['node_id'] = 'N07'
            sealed = current['body'].get('result', {}) if current else {}
            review = tx.get('node_reviews', current_ref) if current_ref else None
            if (any(identity.get(key) != value for key,value in expected.items())
                    or sealed.get('execution_status') != 'succeeded'
                    or sealed.get('data_status') != 'valid' or sealed.get('review_status') == 'rejected'
                    or not review or review['body'].get('decision') != 'accept'
                    or sorted(set(sealed.get('artifact_refs', ()))) != dependency['artifact_refs']):
                return 'analysis_raw_mismatch'
        return None

    @classmethod
    def finalize_acceptance_in(cls, tx, ref, state, token, *, catalog=None):
        """Verify the new source receipt after this attempt's strict closure.

        Generic review is recorded separately. Only catalog-available postchecks
        can assert node acceptance; missing contracts/evidence remain false.
        """
        from ..catalog.repair_actions import standard_repair_catalog
        from ..domain.models import NodeResult
        from ..runtime.node_agent import NodeReview
        from .repair_outcomes import RepairOutcomeService

        cls.assert_execution_in(tx, ref, state, token, drain=True)
        _, context = cls._load_in(tx, ref)
        node = context['source_node']
        acceptance = {'source_node':node, 'source_receipt':context['source_receipt'],
            'repair_receipt':context['repair_receipt'], 'accepted':False,
            'measurement_blocked':context['measurement_blocked'],
            'execution_review_accepted':False, 'reason':'source_node_not_executed'}
        receipt_ref = state.get('result_refs', {}).get(node)
        row = tx.get('receipts', receipt_ref) if receipt_ref else None
        identity = row['body'].get('identity', {}) if row else {}
        expected = {k:state[k] for k in ('thread_id','activation_id','scope','run_ref',
                                       'plan_ref','graph_ref','input_refs')}
        expected['node_id'] = node
        if not row or any(identity.get(k) != value for k,value in expected.items()):
            return acceptance
        acceptance['receipt_ref'] = receipt_ref
        result = NodeResult.model_validate(row['body']['result'])
        review_row = tx.get('node_reviews', receipt_ref)
        review = NodeReview.model_validate(review_row['body']) if review_row else None
        generic = (result.execution_status == 'succeeded' and result.error is None
            and result.review_status != 'rejected' and review is not None
            and review.node_id == node and review.owner_agent_id == f'agent:work:{context["scope"]}'
            and review.decision == 'accept')
        acceptance['execution_review_accepted'] = generic
        acceptance['reason'] = 'execution_or_review_rejected'
        if not generic:
            return acceptance
        if node == 'N01':
            error = cls.reentry_dependency_error_in(tx, ref, node_id=node, state=state,
                token=token, receipt_ref=receipt_ref)
            if error:
                acceptance['reason'] = error
                return acceptance
        order = tx.get('repair_orders', context['order_ref'])['body']
        action_id, _, version = order['action_ref'].rpartition('@')
        try:
            action = (catalog or standard_repair_catalog()).resolve(action_id, version, node).require_available()
        except ValueError:
            acceptance['reason'] = 'node_acceptance_contract_unavailable'
            return acceptance
        acceptance['postcheck_ref'] = action.postcheck_ref
        remaining = context['proof_expires_at'] - tx.now
        if remaining <= 0:
            acceptance['reason'] = 'resource_evidence_expired'
            return acceptance
        proof = LifecycleSafetyService.produce_disposition_in(tx, context['scope'], token, ttl=remaining)
        disposition = LifecycleSafetyService.verify_disposition_in(tx, proof, context['scope'], token,
            require_closed=False)
        acceptance['disposition_ref'] = proof
        closed = (disposition.stopped and disposition.admission_status == 'closed'
            and disposition.sampler_status in {'stopped','not_started'}
            and not disposition.residual_resources and not disposition.isolation_domains)
        order = tx.get('repair_orders', context['order_ref'])['body']
        try:
            statuses = RepairOutcomeService.operation_statuses_in(tx, order, result.operation_refs)
            collection, _ = (RepairOutcomeService.collection_in(tx, order, result)
                             if node == 'N07' else (None, []))
            accepted = closed and action.postcheck(result, review, disposition=disposition,
                checked_at=tx.now, operation_statuses=statuses, collection=collection)
        except ValueError:
            accepted = False
        acceptance['accepted'] = bool(accepted)
        acceptance['reason'] = 'node_postcheck_accepted' if accepted else 'node_postcheck_rejected'
        return acceptance

    @classmethod
    def execution_state_in(cls, tx, ref, state, token):
        cls.assert_execution_in(tx, ref, state, token)
        plan, context = cls._load_in(tx, ref)
        LifecycleSafetyService.assert_work_in(tx, state['scope'], token)
        started = tx.get('recovery_starts', state['activation_id'])
        if (not started or started['body']['plan_ref'] != ref
                or started['body']['owner'] != LifecycleService._owner_body(token)
                or state['input_refs'] != context['input_refs']):
            raise Conflict('recovery must pass current-owner Driver startup verification')
        summaries = {}
        for node, receipt in context['result_refs'].items():
            result = tx.get('receipts', receipt)['body']['result']
            summary = {k:result[k] for k in ('execution_status','data_status','review_status',
                'performance_status','cleanup_status','coverage_status')}
            review = tx.get('node_reviews', receipt)
            summary['node_review_decision'] = review['body']['decision'] if review else 'unavailable'
            summaries[node] = summary
        return {'result_refs':dict(context['result_refs']), 'results':summaries,
                'recovery_acceptance':{'source_node':context['source_node'],
                    'source_receipt':context['source_receipt'], 'repair_receipt':context['repair_receipt'], 'accepted':False,
                    'measurement_blocked':context['measurement_blocked']},
                'recovery_route':context['route'], 'measurement_blocked':context['measurement_blocked'],
                'next_node':plan.entry_node}
