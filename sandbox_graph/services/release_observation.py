"""G6.post observes target-run ledger evidence; candidate/model verdicts are ignored.

A failed or blocked business execution closes admission and requests the same
original-operation stop protocol. It never rolls back files or reopens an old run.
"""
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from ..runtime.tasks import business_job_kind
from .lifecycle_stop import LifecycleStopService


class ReleaseObservationService:
    @staticmethod
    def observe_in(tx,run_ref):
        binding=tx.get('run_bundle_bindings',run_ref)
        if not binding:return {'status':'not_applicable','reason':'not_a_candidate_activation'}
        binding=binding['body'];promotion=(tx.get('rollback_records',binding['rollback_ref']) if binding.get('rollback_ref')
            else tx.get('promotion_records',binding['promotion_ref']))
        if not promotion or promotion['body']['result'].get('run_ref')!=run_ref:
            raise Conflict('release observation has no original promotion')
        run=tx.get('runs',run_ref)['body'];plan=tx.get('plans',run['active_plan_ref'])
        if not plan:raise Conflict('release target plan missing')
        final_node={'all':'F4','report':'F4','analysis':'A7','check':'R3'}.get(plan['body'].get('stage'))
        if final_node is None:raise ValueError('release stage is unregistered')
        jobs=[row for row in tx.scan('jobs') if row['body'].get('run_id')==run_ref
            and business_job_kind(row['body'].get('job_kind','')) in {'run','group','work','analysis','report'}]
        failures=sorted(row['key'] for row in jobs if row['body']['status'] in {'failed','blocked','cancelled'})
        unresolved=any(row['body']['status'] not in {'complete','superseded'} for row in jobs)
        receipts=[]
        for row in tx.scan('receipts'):
            identity=row['body'].get('identity',{});result=row['body'].get('result',{})
            if (identity.get('run_ref'),identity.get('graph_ref'),identity.get('plan_ref'),identity.get('node_id'))!=(
                    run_ref,run['graph_ref'],run['active_plan_ref'],final_node):continue
            activation=tx.get('activations',identity.get('activation_id',''));record=activation['body'].get('record',{}) if activation else {}
            if (not activation or activation['body'].get('status')!='complete'
                    or (record.get('run_id'),record.get('graph_ref'),record.get('plan_ref'))!=(run_ref,run['graph_ref'],run['active_plan_ref'])):continue
            if result.get('execution_status')=='succeeded' and result.get('cleanup_status') in {'not_needed','complete'}:
                receipts.append(row['key'])
        state='fail' if failures else 'pass' if jobs and receipts and not unresolved else 'pending'
        result={'schema_version':1,'gate':'G6.post','producer':'release-observer.v1','status':state,
            'run_ref':run_ref,'promotion_ref':binding['promotion_ref'],'candidate_ref':binding['candidate_ref'],
            'bundle_hash':binding['bundle_hash'],'graph_ref':run['graph_ref'],'plan_ref':run['active_plan_ref'],
            'failure_refs':failures,'final_receipt_refs':sorted(receipts)}
        # Snapshot hashes bind the exact observations, including late failures.
        result['ledger_hash']=canonical_hash({'jobs':jobs,'receipts':[tx.get('receipts',name) for name in sorted(receipts)]})
        key='release-observation:'+canonical_hash(result)
        tx.put('release_observation_history',key,result,immutable=True)
        tx.put('release_observations',run_ref,result|{'observation_ref':key})
        if failures:
            gate=tx.get('dispatch_gates',run_ref)
            if gate and gate['body'].get('open'):
                tx.put('dispatch_gates',run_ref,gate['body']|{'open':False,'policy':'cancel','reason':'post_activation_failure'})
            LifecycleStopService.request_in(tx,request_id='release-failure:'+binding['promotion_ref'],run_ref=run_ref,
                plan_ref=run['active_plan_ref'],graph_ref=run['graph_ref'],reason='post activation business failure',policy='cancel')
            from ..runtime.tasks import TaskBackend,JobSpec
            identity='release-cleanup:'+canonical_hash({'run':run_ref,'promotion':binding['promotion_ref']})
            if not tx.get('jobs',identity):
                TaskBackend.enqueue_in(tx,JobSpec(job_id=identity,job_kind='cleanup',scope_ref=run_ref,
                    thread_id=identity,activation_id=identity,dedupe_key=identity,due_at=tx.now,
                    deadline=tx.now+3600,run_id=run_ref,dispatch_generation=run['dispatch_generation'],
                    payload={'plan_ref':run['active_plan_ref'],'graph_ref':run['graph_ref'],'policy':'cancel'}))
        return result
