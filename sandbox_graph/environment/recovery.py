"""Rebuild a runner for the original environment operation under its new owner."""
from types import SimpleNamespace


class EnvironmentRecoveryOperations:
    def __init__(self,repository,factory):self.repository,self.factory=repository,factory

    def _service(self,scope,token,operation_id=None,resource_ids=()):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            ids=[operation_id] if operation_id else []
            for resource in resource_ids:
                row=tx.get('resources',resource)
                if not row or row['body']['scope']!=scope:raise PermissionError('original resource scope differs')
                ids.append(row['body']['operation_id'])
            records=[tx.get('operations',ref) for ref in ids]
            if not records or any(not row or row['body']['scope']!=scope for row in records):
                raise PermissionError('original environment operations required')
            runs={row['body']['run_id'] for row in records}
            if len(runs)!=1:raise PermissionError('recovery cannot combine environment runs')
            run_id=next(iter(runs))
            originals=[row['body']['run'] for row in tx.scan('environment_commands') if row['body']['run']['run_id']==run_id]
            if len(originals)!=1:raise PermissionError('immutable original environment run binding required')
            original=originals[0]
        activation=SimpleNamespace(run_id=run_id,plan_ref=original['active_plan_ref'],graph_ref=original['graph_ref'])
        return self.factory(token,activation).gateway.operation_service

    async def reconcile(self,operation_id,scope,token,**kwargs):
        return await self._service(scope,token,operation_id).reconcile(operation_id,scope,token,**kwargs)

    async def cancel(self,operation_id,scope,reason,token,**kwargs):
        return await self._service(scope,token,operation_id).cancel(operation_id,scope,reason,token,**kwargs)

    async def cleanup(self,scope,resource_ids,token,**kwargs):
        if not resource_ids:
            from ..runtime.runner import CleanupResult
            return CleanupResult(status='complete',resource_ids=())
        return await self._service(scope,token,resource_ids=resource_ids).cleanup(scope,resource_ids,token,**kwargs)
