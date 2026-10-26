"""Durable, transaction-local lifecycle/lineage reservations.

Only repository transactions serialize accounting. These methods never open a
nested transaction or execute providers. Costs are integers in an explicitly
configured smallest currency unit; callers must enforce provider maxima.
"""
from copy import deepcopy
import math

from ..domain.models import canonical_hash
from ..persistence.repository import Conflict


BUSINESS_COUNTERS = frozenset({'repair_attempts', 'diagnosis_calls', 'candidate_revisions',
                               'tokens', 'child_jobs', 'observations', 'cost'})
SAFETY_COUNTERS = frozenset({'stop_calls', 'query_calls', 'cleanup_calls', 'cost'})
SAFETY_OPERATIONS = {'stop': 'stop_calls', 'query': 'query_calls', 'cleanup': 'cleanup_calls'}


def _ref(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty reference')
    return value


def _amounts(value, counters, *, complete=False):
    if not isinstance(value, dict) or not value or not set(value) <= counters:
        raise ValueError('explicit known budget counters required')
    if complete and set(value) != counters:
        raise ValueError('all budget limits must be explicit')
    if any(type(amount) is not int or amount <= 0 for amount in value.values()):
        raise ValueError('budget amounts must be positive integers')
    return dict(value)


def _deadline(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('finite wall clock deadline required')
    return value


def _key(kind, ref):
    if kind not in {'lifecycle', 'lineage'}:
        raise ValueError('budget target must be lifecycle or lineage')
    return f'{kind}:{_ref(ref, "budget reference")}'


def _load(tx, kind, ref):
    row = tx.get('lifecycle_budgets', _key(kind, ref))
    if row is None:
        raise Conflict(f'{kind} budget missing')
    return row


def _save(tx, row):
    tx.put('lifecycle_budgets', row['key'], row['body'], expected_revision=row['revision'])


def _config(limits, deadline, safety_limits, safety_deadline, cost_unit):
    result = {'limits': _amounts(limits, BUSINESS_COUNTERS, complete=True),
              'deadline': _deadline(deadline),
              'safety_limits': _amounts(safety_limits, SAFETY_COUNTERS, complete=True),
              'safety_deadline': _deadline(safety_deadline),
              'cost_unit': _ref(cost_unit, 'cost unit')}
    if result['safety_deadline'] < result['deadline']:
        raise ValueError('safety deadline must cover business deadline')
    return result


class LifecycleBudgetService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def initialize_in(tx, *, lifecycle_id, lineage_budget_ref, limits, deadline,
                      safety_limits, safety_deadline, cost_unit, lineage_limits=None,
                      lineage_deadline=None, lineage_safety_limits=None,
                      lineage_safety_deadline=None):
        """Create bounds once; new lifecycle IDs share existing lineage accounting.

        Optional lineage bounds apply on first creation. Supplying them again
        asserts the original configuration; it never replaces current limits.
        """
        _ref(lifecycle_id, 'lifecycle ID')
        _ref(lineage_budget_ref, 'lineage budget reference')
        config = _config(limits, deadline, safety_limits, safety_deadline, cost_unit)
        lineage_config = _config(
            limits if lineage_limits is None else lineage_limits,
            deadline if lineage_deadline is None else lineage_deadline,
            safety_limits if lineage_safety_limits is None else lineage_safety_limits,
            safety_deadline if lineage_safety_deadline is None else lineage_safety_deadline,
            cost_unit)
        local = tx.get('lifecycle_budgets', _key('lifecycle', lifecycle_id))
        lineage = tx.get('lifecycle_budgets', _key('lineage', lineage_budget_ref))
        if local and (local['body']['original'] != config
                      or local['body']['lineage_budget_ref'] != lineage_budget_ref):
            raise Conflict('lifecycle budget initialization conflict; authorized adjustment required')
        if lineage:
            original = lineage['body']['original']
            explicit = {'limits': lineage_limits, 'deadline': lineage_deadline,
                        'safety_limits': lineage_safety_limits,
                        'safety_deadline': lineage_safety_deadline}
            if original['cost_unit'] != cost_unit or any(
                    value is not None and original[field] != value for field, value in explicit.items()):
                raise Conflict('lineage budget initialization conflict; authorized adjustment required')
        if local and not lineage:
            raise Conflict('lifecycle has missing lineage budget')
        creates = []
        for kind, ref, old, bounds in [('lineage', lineage_budget_ref, lineage, lineage_config),
                                       ('lifecycle', lifecycle_id, local, config)]:
            if old:
                continue
            if bounds['deadline'] <= tx.now or bounds['safety_deadline'] <= tx.now:
                raise Conflict('budget initialization deadline elapsed')
            body = {'kind': kind, 'ref': ref, 'lineage_budget_ref': lineage_budget_ref,
                    'original': deepcopy(bounds), **deepcopy(bounds),
                    'reserved': dict.fromkeys(BUSINESS_COUNTERS, 0),
                    'spent': dict.fromkeys(BUSINESS_COUNTERS, 0),
                    'safety_reserved': dict.fromkeys(SAFETY_COUNTERS, 0),
                    'safety_spent': dict.fromkeys(SAFETY_COUNTERS, 0)}
            creates.append((_key(kind, ref), body))
        for key, body in creates:
            tx.put('lifecycle_budgets', key, body, expected_revision=-1)
        return LifecycleBudgetService.snapshot_in(tx, lifecycle_id)

    @staticmethod
    def snapshot_in(tx, lifecycle_id):
        local = _load(tx, 'lifecycle', lifecycle_id)['body']
        lineage = _load(tx, 'lineage', local['lineage_budget_ref'])['body']
        def snapshot(body):
            result = deepcopy(body)
            result['consumed'] = deepcopy(body['spent'])
            result['safety_consumed'] = deepcopy(body['safety_spent'])
            return result
        return {'lifecycle': snapshot(local), 'lineage': snapshot(lineage)}

    @staticmethod
    def reserve_in(tx, *, request_id, lifecycle_id, amounts, input_ref,
                   metadata=None, safety=False, operation_kind=None,
                   provider_enforces_maximum=None):
        """Reserve both bounds in the enclosing fenced order/job transaction.

        Request identity includes content/version metadata. Safety requests are
        restricted to exactly their stop/query/cleanup counter and optional cost.
        """
        _ref(request_id, 'request ID')
        _ref(lifecycle_id, 'lifecycle ID')
        _ref(input_ref, 'input reference')
        if type(safety) is not bool:
            raise ValueError('safety must be explicit boolean')
        amounts = _amounts(amounts, SAFETY_COUNTERS if safety else BUSINESS_COUNTERS)
        if safety:
            counter = SAFETY_OPERATIONS.get(operation_kind)
            if counter is None or counter not in amounts or not set(amounts) <= {counter, 'cost'}:
                raise ValueError('safety envelope permits only matching stop/query/cleanup counter and cost')
        elif operation_kind is not None:
            raise ValueError('operation kind is only used for safety reservations')
        if not safety and (amounts.get('diagnosis_calls') or amounts.get('tokens')):
            if (provider_enforces_maximum is not True or not amounts.get('tokens')
                    or not amounts.get('cost') or not amounts.get('diagnosis_calls')):
                raise ValueError('model calls require enforceable maximum tokens and cost reservation')
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError('metadata must be an explicit object')
        content = {'request_id': request_id, 'lifecycle_id': lifecycle_id, 'amounts': amounts,
                   'input_ref': input_ref, 'metadata': deepcopy(metadata or {}), 'safety': safety,
                   'operation_kind': operation_kind, 'provider_enforces_maximum': provider_enforces_maximum}
        fingerprint = canonical_hash(content)
        old = tx.get('budget_reservations', request_id)
        if old:
            if old['body']['input_hash'] != fingerprint:
                raise Conflict('request ID budget content conflict')
            return old['body']
        local = _load(tx, 'lifecycle', lifecycle_id)
        lineage = _load(tx, 'lineage', local['body']['lineage_budget_ref'])
        reserved, spent, limits, deadline = (('safety_reserved', 'safety_spent', 'safety_limits', 'safety_deadline')
                                             if safety else ('reserved', 'spent', 'limits', 'deadline'))
        # Validate every bound before writing either, including if a caller catches
        # Conflict and continues its outer transaction.
        for row in (lineage, local):
            body = row['body']
            if tx.now >= body[deadline]:
                raise Conflict(f'{body["kind"]} budget deadline elapsed')
            for counter, amount in amounts.items():
                if body[reserved][counter] + body[spent][counter] + amount > body[limits][counter]:
                    raise Conflict(f'{body["kind"]} budget exhausted: {counter}')
        for row in (lineage, local):
            for counter, amount in amounts.items():
                row['body'][reserved][counter] += amount
            _save(tx, row)
        receipt = {**content, 'input_hash': fingerprint,
                   'lineage_budget_ref': local['body']['lineage_budget_ref'], 'reserved_at': tx.now}
        tx.put('budget_reservations', request_id, receipt, immutable=True)
        return receipt

    @staticmethod
    def settle_in(tx, *, request_id, actual_tokens=None, actual_cost=None, proven_not_submitted=False):
        """Append one settlement. Unknown dimensions consume their full maximum.

        Callers may assert proven_not_submitted only after validating provider
        evidence; the flag alone is not proof at an untrusted public boundary.
        Attempt/job/observation counts always remain consumed.
        """
        _ref(request_id, 'request ID')
        if type(proven_not_submitted) is not bool:
            raise ValueError('submission proof indicator must be boolean')
        for value in (actual_tokens, actual_cost):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('actual usage must be a nonnegative integer or unknown')
        if proven_not_submitted and any(value not in (None, 0) for value in (actual_tokens, actual_cost)):
            raise ValueError('unsubmitted provider cannot report positive usage')
        content = {'request_id': request_id, 'actual_tokens': actual_tokens,
                   'actual_cost': actual_cost, 'proven_not_submitted': proven_not_submitted}
        old = tx.get('budget_settlements', request_id)
        if old:
            if old['body']['input'] != content:
                raise Conflict('settlement content conflict')
            return old['body']
        reservation = tx.get('budget_reservations', request_id)
        if reservation is None:
            raise Conflict('budget reservation missing')
        binding = reservation['body']
        charged = dict(binding['amounts'])
        unknown = False
        for counter, actual in [('tokens', actual_tokens), ('cost', actual_cost)]:
            maximum = charged.get(counter, 0)
            if actual is not None and actual > maximum:
                raise Conflict(f'actual {counter} exceeds reserved provider maximum')
            if counter not in charged:
                continue
            if proven_not_submitted:
                charged[counter] = 0
            elif actual is None:
                unknown = True
            else:
                charged[counter] = actual
        reserved, spent = ('safety_reserved', 'safety_spent') if binding['safety'] else ('reserved', 'spent')
        rows = (_load(tx, 'lineage', binding['lineage_budget_ref']),
                _load(tx, 'lifecycle', binding['lifecycle_id']))
        for row in rows:
            if any(row['body'][reserved][counter] < amount for counter, amount in binding['amounts'].items()):
                raise Conflict('budget reservation accounting conflict')
        for row in rows:
            for counter, amount in binding['amounts'].items():
                row['body'][reserved][counter] -= amount
                row['body'][spent][counter] += charged[counter]
            _save(tx, row)
        receipt = {'input': content, 'charged': charged,
                   'usage_status': 'not_submitted' if proven_not_submitted else 'unknown' if unknown else 'known',
                   'settled_at': tx.now}
        tx.put('budget_settlements', request_id, receipt, immutable=True)
        return receipt

    @staticmethod
    def adjust_in(tx, *, adjustment_id, target_kind, target_ref, additions, authorization_ref,
                  safety=False):
        """Consume scoped, expiring authorization exactly once; append increments.

        The trusted authorization service owns immutable budget_authorizations.
        Each grant binds target, exact additions, cost unit, expiry and safety.
        """
        _ref(adjustment_id, 'adjustment ID')
        _ref(authorization_ref, 'authorization reference')
        if type(safety) is not bool:
            raise ValueError('safety must be boolean')
        additions = _amounts(additions, SAFETY_COUNTERS if safety else BUSINESS_COUNTERS)
        _key(target_kind, target_ref)
        content = {'adjustment_id': adjustment_id, 'target_kind': target_kind,
                   'target_ref': target_ref, 'additions': additions,
                   'authorization_ref': authorization_ref, 'safety': safety}
        old = tx.get('budget_adjustments', adjustment_id)
        if old:
            if old['body']['input'] != content:
                raise Conflict('budget adjustment content conflict')
            return old['body']
        grant = tx.get('budget_authorizations', authorization_ref)
        target = _load(tx, target_kind, target_ref)
        if grant is None:
            raise Conflict('explicit budget authorization required')
        authorization = grant['body']
        expiry = authorization.get('expires_at')
        try:
            authorized_additions = _amounts(authorization.get('additions'),
                                           SAFETY_COUNTERS if safety else BUSINESS_COUNTERS)
        except ValueError as exc:
            raise Conflict('budget authorization amounts invalid') from exc
        if (authorization.get('target_kind') != target_kind or authorization.get('target_ref') != target_ref
                or authorized_additions != additions
                or authorization.get('cost_unit') != target['body']['cost_unit']
                or authorization.get('safety') is not safety
                or authorization.get('revoked') is not False
                or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry) or expiry <= tx.now):
            raise Conflict('budget authorization scope, contents or validity mismatch')
        if tx.get('budget_authorization_uses', authorization_ref):
            raise Conflict('budget authorization already consumed')
        limit_key = 'safety_limits' if safety else 'limits'
        for counter, amount in additions.items():
            target['body'][limit_key][counter] += amount
        _save(tx, target)
        receipt = {'input': content, 'applied_at': tx.now}
        tx.put('budget_adjustments', adjustment_id, receipt, immutable=True)
        tx.put('budget_authorization_uses', authorization_ref, {'adjustment_id': adjustment_id}, immutable=True)
        return receipt

    def initialize(self, **kwargs):
        with self.repository.transaction() as tx:
            return self.initialize_in(tx, **kwargs)

    def reserve(self, **kwargs):
        with self.repository.transaction() as tx:
            return self.reserve_in(tx, **kwargs)

    def settle(self, **kwargs):
        with self.repository.transaction() as tx:
            return self.settle_in(tx, **kwargs)

    def adjust(self, **kwargs):
        with self.repository.transaction() as tx:
            return self.adjust_in(tx, **kwargs)

    def snapshot(self, lifecycle_id):
        with self.repository.transaction() as tx:
            return self.snapshot_in(tx, lifecycle_id)
