"""Route registered installation and canary operations without changing old runners."""
from ..domain.models import Operation
from ..runtime.runner import CleanupResult
from .contracts import ActionStep


class EnvironmentGateway:
    def __init__(self,repository,*,token,remote,canaries):
        self.repository=repository;self.token=token;self.remote=remote;self.canaries=dict(canaries)

    def _runner(self,operation):
        step=ActionStep.model_validate(operation.input['step'])
        if step.action_ref=='environment.canary.v1':
            runner=self.canaries.get(step.node_id)
            if runner is None:raise ValueError('node has no registered canary adapter')
            return runner
        return self.remote

    def _original(self,operation_id,scope):
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token);row=tx.get('operations',operation_id)
            if not row or row['body']['scope']!=scope:raise PermissionError('environment operation scope differs')
            return Operation.model_validate(row['body']['request'])

    async def submit(self,operation):return await self._runner(operation).submit(operation)
    async def reconcile(self,operation_id,scope):
        return await self._runner(self._original(operation_id,scope)).reconcile(operation_id,scope)
    async def cancel(self,operation_id,scope,reason):
        return await self._runner(self._original(operation_id,scope)).cancel(operation_id,scope,reason)
    async def collect(self,operation_id,scope,last_seq=0):
        return await self._runner(self._original(operation_id,scope)).collect(operation_id,scope,last_seq)
    async def cleanup(self,scope,resource_ids):
        groups={}
        with self.repository.transaction() as tx:
            tx.assert_fence(self.token)
            for identity in resource_ids:
                row=tx.get('resources',identity)
                if not row or row['body']['scope']!=scope:raise PermissionError('environment cleanup ownership differs')
                groups.setdefault(row['body']['operation_id'],[]).append(identity)
        remaining=[];refs=[]
        for op_id,ids in groups.items():
            result=await self._runner(self._original(op_id,scope)).cleanup(scope,tuple(ids))
            remaining.extend(result.remaining);refs.extend(result.evidence_refs)
            if result.status=='unknown' and not result.remaining:remaining.extend(ids)
        return CleanupResult(status='incomplete' if remaining else 'complete',resource_ids=resource_ids,
            remaining=tuple(remaining),evidence_refs=tuple(refs))
