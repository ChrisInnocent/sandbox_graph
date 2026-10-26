"""One measurement attempt per work; takeover retains original node/operation IDs."""
from ..domain.models import ErrorRecord, NodeResult, canonical_hash
from .recovery import RecoveryService


class MeasurementContinuityService:
    @staticmethod
    def guard_in(tx, node, state):
        scope = state['scope']
        if node not in {'N07','N08','N09','N10'} and tx.get('run_generation_bindings',state['run_ref']):
            from .measurement_generation import MeasurementGenerationService
            from ..persistence.repository import Conflict
            try:
                plan=MeasurementGenerationService.validate_run_in(tx,state['run_ref'],plan_ref=state['plan_ref'])
                work=tx.get('work_instances',scope)
                if work: MeasurementGenerationService.validate_work_in(tx,plan,work['body'])
            except (Conflict,PermissionError,ValueError) as exc:
                return NodeResult(execution_status='blocked',data_status='insufficient',
                    error=ErrorRecord(code='MEASUREMENT_AUTHORITY',observed_at=tx.now,message=str(exc)))
        if node not in {'N04','N05','N06'} or not tx.get('work_instances', scope):
            return None
        claim = tx.get('measurement_attempts', scope)
        reason = None
        if claim and claim['body']['activation_id'] != state['activation_id']:
            reason = 'measurement_generation_already_started'
        elif not claim:
            # Historical observations/receipts are evidence of a prior start even
            # before this version introduced the attempt index.
            if any(r['body'].get('scope') == scope for r in tx.scan('observations')) or any(
                r['body'].get('scope') == scope and (r['body'].get('node_id') or r['body'].get('request',{}).get('node_id'))
                not in {'N00','N01','N02','N03','N07','N08','N09','N10'} for r in tx.scan('operations')) or any(
                r['body'].get('identity', {}).get('scope') == scope
                and r['body']['identity'].get('node_id') in {'N04','N05','N06'}
                and r['body']['identity'].get('activation_id') != state['activation_id']
                for r in tx.scan('receipts')):
                reason = 'historical_measurement_requires_new_generation'
            else:
                body = {'activation_id':state['activation_id'],
                    'input_hash':RecoveryService._input_evidence_in(tx, state['input_refs']),
                    'preparation_refs':{n:state.get('result_refs',{}).get(n) for n in ('N02','N03')}}
                tx.put('measurement_attempts', scope, body, immutable=True)
                claim = tx.get('measurement_attempts', scope)
        if not reason and claim:
            if claim['body']['input_hash'] != RecoveryService._input_evidence_in(tx, state['input_refs']):
                reason = 'measurement_fixture_or_baseline_changed'
            elif any(r['body'].get('scope') == scope and (r['body'].get('interrupted')
                     or len(r['body'].get('segments', [])) > 1
                     or r['body'].get('data_status') == 'insufficient') for r in tx.scan('observations')):
                reason = 'measurement_segment_discontinuous'
        if reason:
            tx.put('measurement_discontinuities', scope + ':' + state['activation_id'],
                {'scope':scope,'activation_id':state['activation_id'],'node_id':node,'reason':reason}, immutable=True)
            return NodeResult(execution_status='blocked', data_status='insufficient',
                error=ErrorRecord(code='MEASUREMENT_CONTINUITY', observed_at=tx.now, message=reason))
        return None

    @staticmethod
    def qualify_in(tx, node, state, result):
        if node != 'N08': return result
        scope = state['scope']
        interrupted = tx.get('measurement_discontinuities', scope + ':' + state['activation_id'])
        interrupted = interrupted or any(r['body'].get('scope') == scope and (
            r['body'].get('interrupted') or r['body'].get('data_status') == 'insufficient')
            for r in tx.scan('observations'))
        interrupted = interrupted or any(v.get('data_status') == 'insufficient'
            or v.get('execution_status') in {'failed','blocked','cancelled'}
            for n,v in state.get('results', {}).items() if n in {'N04','N05','N06'})
        if interrupted:
            return result.model_copy(update={'data_status':'insufficient'})
        return result
