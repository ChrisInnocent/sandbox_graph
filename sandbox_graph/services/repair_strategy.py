"""Deterministic, evidence-bound selection from a trusted repair catalog.

A new failed receipt is history, not automatically a new input or progress.
Only an explicit rule consuming that failure can select a different strategy.
"""
from ..domain.models import canonical_hash
from ..domain.agent_lifecycle import RepairPostcheckEvidence
from .agent_lifecycle import LifecycleService, decode


def strategy_input_hash(record, input_refs):
    return canonical_hash({'source_receipt':record.source_receipt, 'input_refs':input_refs})


def failure_count_in(tx, record, action_ref, input_hash, stable_hash):
    return sum(1 for row in tx.scan('repair_failure_fingerprints') if
        row['body'].get('lineage_budget_ref') == record.key.lineage_budget_ref
        and row['body'].get('action_ref') == action_ref
        and row['body'].get('failure_fingerprint') == record.issue_fingerprint
        and (row['body'].get('strategy_input_hash') == stable_hash
             or ('strategy_input_hash' not in row['body'] and row['body'].get('input_hash') == input_hash)))


def _postcheck_rejection_in(tx, record, order, receipt_ref, rule, catalog):
    """Accept only the independent service's bound, known-effect check fact.

    During record_in its check and failure row already exist in this UoW; the
    final outcome commits with them. Later diagnosis also verifies that outcome.
    """
    failure = tx.get('repair_failure_fingerprints', order['order_ref'])['body']
    evidence_ref = failure.get('postcheck_evidence_ref')
    evidence = tx.get('repair_postchecks', evidence_ref) if evidence_ref else None
    if not evidence:
        return None
    try:
        check = decode(RepairPostcheckEvidence, evidence['body'])
    except ValueError:
        return None
    receipt = tx.get('receipts', receipt_ref)
    predecessor_id, _, version = order['action_ref'].rpartition('@')
    predecessor = catalog.resolve(predecessor_id, version, record.key.node_id)
    if (evidence_ref != 'repair-postcheck:' + canonical_hash(evidence['body'])
            or check.postcheck_status != 'rejected' or check.effect_certainty == 'unknown'
            or check.lifecycle_id != record.key.lifecycle_id or check.order_ref != order['order_ref']
            or check.action_ref != order['action_ref'] or check.action_ref != rule.preceding_action_ref
            or check.postcheck_ref != rule.preceding_postcheck_ref
            or check.postcheck_ref != predecessor.postcheck_ref
            or check.receipt_ref != receipt_ref or not receipt
            or check.receipt_hash != canonical_hash(receipt['body'])):
        return None
    execution = tx.get('repair_executions', order['order_ref'])
    job = tx.get('jobs', execution['body']['job_ref']) if execution else None
    if not job or any(receipt['body']['identity'].get(k) != v for k, v in {
            'thread_id':job['body']['thread_id'], 'activation_id':job['body']['activation_id'],
            'plan_ref':order['plan_ref'], 'graph_ref':order['graph_ref'],
            'visit_number':order['attempt']}.items()):
        return None
    disposition = tx.get('resource_disposition_receipts', check.disposition_ref)
    if (not disposition or check.disposition_ref != 'disposition:' + canonical_hash(disposition['body'])
            or disposition['body']['identity']['scope'] != order['work_ref']):
        return None
    outcome = tx.get('repair_outcomes', order['order_ref'])
    if outcome:
        body = outcome['body']
        fact = body['outcome']
        if (body.get('postcheck_evidence_ref') != evidence_ref or body['postcheck_status'] != 'rejected'
                or fact['order_ref'] != check.order_ref or fact['postcheck_ref'] != check.postcheck_ref
                or fact['effect_certainty'] != check.effect_certainty
                or receipt_ref not in fact['raw_refs'] or check.disposition_ref not in fact['raw_refs']
                or body['return_result']['output']['receipt_ref'] != receipt_ref):
            return None
    elif record.phase != 'remediating':
        return None
    return evidence_ref


def select_strategy_in(tx, record, policy, catalog):
    source=tx.get('lifecycle_sources',record.key.lifecycle_id)['body']
    source_result=tx.get('receipts',record.source_receipt)['body']['result']
    previous=None
    if record.attempt_refs:
        ref=record.attempt_refs[-1]
        order=tx.get('repair_orders',ref)
        failure=tx.get('repair_failure_fingerprints',ref)
        if order and failure and order['body']['lifecycle_id']==record.key.lifecycle_id:
            receipt_ref=failure['body']['receipt_ref']
            receipt=tx.get('receipts',receipt_ref)
            if receipt:
                identity=receipt['body']['identity']
                if all(identity.get(k)==v for k,v in {'scope':record.key.scope_ref,
                        'run_ref':record.key.run_ref,'node_id':record.key.node_id,
                        'input_refs':source['identity']['input_refs']}.items()):
                    previous=(order['body'],receipt_ref,receipt['body']['result'])
    stable_hash=strategy_input_hash(record,source['identity']['input_refs'])
    exhausted=[]
    # Explicit new failure predicates take precedence over original-source rules.
    for rule in sorted(catalog.rules,key=lambda rule: rule.predicate=='source_error'):
        if rule.node_id!=record.key.node_id or rule.action_ref not in policy.actions:
            continue
        result=source_result
        evidence=[record.source_receipt]
        if rule.predicate!='source_error':
            if previous is None or previous[0]['action_ref']!=rule.preceding_action_ref:
                continue
            result=previous[2]
            evidence.append(previous[1])
        if rule.predicate=='postcheck_rejected':
            check_ref=_postcheck_rejection_in(tx,record,previous[0],previous[1],rule,catalog)
            if check_ref is None:
                continue
            evidence.append(check_ref)
        elif (result.get('execution_status') not in {'failed','blocked'}
                or (result.get('error') or {}).get('code') not in rule.error_codes):
            continue
        LifecycleService._evidence_in(tx,record,evidence)
        action_id,_,version=rule.action_ref.rpartition('@')
        catalog.resolve(action_id,version,record.key.node_id).require_available()
        manifest={'schema_version':1,'source_receipt':record.source_receipt,
            'evidence_refs':evidence,'input_refs':source['identity']['input_refs']}
        body={'lifecycle_id':record.key.lifecycle_id,'manifest':manifest}
        digest=canonical_hash(body)
        failures=failure_count_in(tx,record,rule.action_ref,digest,stable_hash)
        if failures>=policy.max_same_failure:
            exhausted.append(rule.action_ref)
            continue
        return {'schema_version':1,'rule':rule.model_dump(mode='json'),
            'action_ref':rule.action_ref,'evidence_refs':evidence,
            'manifest':manifest,'input_hash':digest,'strategy_input_hash':stable_hash,
            'same_failure_count':failures,'issue_fingerprint':record.issue_fingerprint}, exhausted
    return None, exhausted
