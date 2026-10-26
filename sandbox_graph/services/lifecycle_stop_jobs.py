"""Bounded stop coordinators. Only immutable target-bound jobs bypass admission."""
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from ..runtime.tasks import JobSpec, TaskBackend, business_job_kind


class StopJobs:
    FIELDS = ('job_id','scope_ref','thread_id','run_id','deadline','payload')
    MAX_POLLS = 16
    WALL_SECONDS = 300

    @classmethod
    def valid_in(cls, tx, job):
        if business_job_kind(job.get('job_kind','')) != 'lifecycle_stop':
            return False
        binding = tx.get('stop_job_bindings',job.get('job_id',''))
        if not binding or any(binding['body'].get(k) != job.get(k) for k in cls.FIELDS):
            return False
        payload=job.get('payload',{})
        target=tx.get('lifecycle_stop_requests',payload.get('stop_target_ref',''))
        if not target:
            return False
        request=target['body']
        command=tx.get('run_stop_requests',request['command_ref'])
        return bool(command and all(request.get(k)==command['body'].get(k) for k in command['body'])
            and job['run_id']==request['run_ref'] and job['scope_ref']==request['scope_ref'])

    @classmethod
    def enqueue_in(cls,tx,route_ref,body,*,attempt=0,deadline=None,due_at=None):
        route=tx.get('outbox',route_ref)
        target=tx.get('lifecycle_stop_requests',body['request_ref'])
        if not route or route['body']!=body or not target:
            raise Conflict('stop route must bind original immutable target')
        request=target['body']
        identity='stop-job:'+canonical_hash({'route':route_ref,'attempt':attempt})
        existing=tx.get('jobs',identity)
        if existing:
            return existing['body']
        spec=JobSpec(job_id=identity,job_kind='lifecycle_stop',scope_ref=request['scope_ref'],
            thread_id='stop-coordinator:'+body['request_ref'],activation_id=identity,
            dedupe_key=identity,due_at=tx.now if due_at is None else due_at,
            deadline=tx.now+cls.WALL_SECONDS if deadline is None else deadline,
            run_id=request['run_ref'],max_attempts=3,payload={'stop_target_ref':body['request_ref'],
                'route_ref':route_ref,'attempt':attempt,'plan_ref':request['plan_ref'],'graph_ref':request['graph_ref']})
        value=spec.model_dump(mode='json')
        tx.put('stop_job_bindings',identity,{k:value[k] for k in cls.FIELDS},immutable=True)
        TaskBackend.enqueue_in(tx,spec)
        return tx.get('jobs',identity)['body']

    def __init__(self,repository):
        self.repository=repository

    async def __call__(self,job):
        from .lifecycle_stop import LifecycleStopService
        from .lifecycle_disposition import DispositionVersions
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx,job)
            if not self.valid_in(tx,job):
                raise Conflict('stop coordinator original binding differs')
            payload=job['payload']; target=payload['stop_target_ref']
            status=tx.get('lifecycle_stop_status',target)
            if status and status['body']['status']=='closed':
                proof=tx.get('lifecycle_stop_proofs',status['body']['proof_ref'])
                if proof:
                    try:
                        DispositionVersions.verify_stop_in(tx,proof['body'])
                    except Conflict:
                        pass
                    else:
                        return {'execution_status':'succeeded','proof_ref':proof['key']}
            route=tx.get('outbox',payload['route_ref'])
            closed=LifecycleStopService.consume_in(tx,payload['route_ref'],route['body'])
            status=tx.get('lifecycle_stop_status',target)['body']
            if closed:
                return {'execution_status':'succeeded','proof_ref':status['proof_ref']}
            attempt=payload['attempt']+1
            due=tx.now+min(30,2**min(attempt-1,5))
            if attempt<self.MAX_POLLS and due<job['deadline']:
                follow=self.enqueue_in(tx,payload['route_ref'],route['body'],attempt=attempt,
                    deadline=job['deadline'],due_at=due)
                tx.put('lifecycle_stop_status',target,status|{'due_at':due,'stop_job_ref':follow['job_id']})
            else:
                tx.put('lifecycle_stop_status',target,status|{'poll_budget_exhausted':True,
                    'recovery_entry':'new original-effect evidence wakes this same target'})
            return {'execution_status':'blocked','stop_target_ref':target,'reason':status['reason']}
