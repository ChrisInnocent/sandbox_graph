"""有限原 ID 安全调用；每个持久轮次复用真实 job/thread/lifecycle/work fences。"""
from __future__ import annotations

import asyncio
from contextlib import suppress

from ..persistence.repository import Conflict, StaleOwner, positive_duration
from ..services.agent_lifecycle import LifecycleService, decode
from ..services.agent_team import AgentTeamService
from ..services.lifecycle_safety import LifecycleSafetyService
from ..services.lifecycle_stop import LifecycleStopService
from .agent_bridge import AgentBridge, SubmitEnvelope
from .tasks import TaskBackend


class _SafetyProvider:
    """Bridge 窄代理：没有 submit/collect，不改普通团队网络 fence。"""
    def __init__(self, handler, job, token, call_ref):
        self.handler,self.job,self.token,self.call_ref=handler,job,token,call_ref

    def check(self, *, start=False):
        with self.handler.repository.transaction() as tx:
            session=self.handler.service.network_in(tx,self.job,self.token,self.call_ref,start=start)
            task,contract,record,remote=AgentTeamService.safety_target_in(tx,session['identity']['target_ref'])
            return remote

    async def capabilities(self):
        self.check()
        return await self.handler.agent_provider.capabilities()

    async def lookup(self, request_id):
        remote=self.check(start=True)
        if request_id!=remote.envelope.request_id:
            raise PermissionError('只允许原 provider request ID')
        return await self.handler.agent_provider.lookup(request_id)

    async def status(self, handle):
        remote=self.check(start=True)
        if remote.receipt is None or handle!=remote.receipt.handle:
            raise PermissionError('只允许原 provider handle')
        return await self.handler.agent_provider.status(handle)

    async def cancel(self, handle):
        remote=self.check(start=True)
        if remote.receipt is None or handle!=remote.receipt.handle:
            raise PermissionError('只允许原 provider handle')
        return await self.handler.agent_provider.cancel(handle)


class LifecycleReconciliationHandler:
    def __init__(self, service, *, lease_seconds, operation_service=None, agent_provider=None, legacy=None, owner='safety'):
        self.service,self.repository=service,service.repository
        self.operation_service,self.agent_provider,self.legacy=operation_service,agent_provider,legacy
        self.lease_seconds,self.owner=positive_duration(lease_seconds),owner

    async def call(self, job, token, kind, target):
        with self.repository.transaction() as tx:
            ref,fresh=self.service.prepare_call_in(tx,job,token,kind,target)
            session=self.service.assert_owner_in(tx,job,token)
            identity=session['identity']
            if not fresh:
                return tx.get('reconciliation_calls',ref)['body']['state']=='completed'
        completed=False
        try:
            if identity['operation_kind']=='provider':
                with self.repository.transaction() as tx:
                    task,contract,record,remote=AgentTeamService.safety_target_in(tx,identity['target_ref'])
                    envelope=decode(SubmitEnvelope,task['envelope'])
                bridge=AgentBridge(self.repository,_SafetyProvider(self,job,token,ref),
                    provider_id=contract.provider_id,scope=envelope.scope,
                    commit_guard=lambda tx:self.service.network_in(tx,job,token,ref))
                operation=bridge.lookup(target) if kind=='query' else bridge.cancel(target)
            else:
                if self.operation_service is None:
                    raise Conflict('operation_service_not_connected')
                # OperationService checks before I/O and in its status/resource commit.
                with self.repository.transaction() as tx:
                    self.service.network_in(tx,job,token,ref,start=True)
                guard=lambda tx:self.service.network_in(tx,job,token,ref)
                scope=identity['work_ref']
                if kind=='query':
                    operation=self.operation_service.reconcile(target,scope,token,safety_guard=guard)
                elif kind=='stop':
                    operation=self.operation_service.cancel(target,scope,'原操作安全续办',token,safety_guard=guard,safety_call_ref=ref)
                else:
                    operation=self.operation_service.cleanup(scope,tuple(target),token,safety_guard=guard)
            with self.repository.transaction() as tx:
                remaining=min(job['deadline']-tx.now,token.lease_until-tx.now)
            await asyncio.wait_for(operation,timeout=remaining)
            with self.repository.transaction() as tx:
                self.service.assert_owner_in(tx,job,token)
            completed=True
        except (StaleOwner,asyncio.CancelledError):
            raise
        except Exception:
            # Never trust an adapter exception/timeout as nonexecution proof.
            completed=False
        finally:
            with self.repository.transaction() as tx:
                # 超时外层 Worker 已终结 job；watchdog 沿原意图结算未知费用。
                with suppress(StaleOwner):
                    self.service.finish_call_in(tx,job,token,ref,completed=completed)
        return completed

    async def site(self, job, token):
        if self.operation_service is None:
            raise Conflict('operation_service_not_connected')
        with self.repository.transaction() as tx:
            session=self.service.assert_owner_in(tx,job,token)
            identity=session['identity']
        from .observation import ObservationService
        for ref in identity['operation_refs']:
            with self.repository.transaction() as tx:
                self.service.assert_owner_in(tx,job,token)
                known=LifecycleSafetyService._operation_closed(tx.get('operations',ref)['body'])
                sampler_pending=any(r['body'].get('operation_id')==ref
                    and not ObservationService.closed_in(tx,r)
                    for r in LifecycleSafetyService._rows_in(tx,'observations',identity['work_ref']))
                stop_issued=bool(tx.get('sampler_stop_intents',ref)) or any(
                    r['body'].get('session_ref')==session['ref'] and r['body'].get('kind')=='stop'
                    and r['body'].get('target')==ref for r in tx.scan('reconciliation_calls'))
            # The original baseline can stay terminal while its already issued
            # stop is unconfirmed. Query that result under the original budget;
            # never change the business outcome to trigger reconciliation.
            if not known or (sampler_pending and stop_issued):
                await self.call(job,token,'query',ref)
        with self.repository.transaction() as tx:
            self.service.assert_owner_in(tx,job,token)
            pending=[ref for ref in identity['operation_refs']
                if not LifecycleSafetyService._operation_closed(tx.get('operations',ref)['body'])]
        if pending:
            for ref in pending:
                with self.repository.transaction() as tx:
                    self.service.assert_owner_in(tx,job,token)
                    record=LifecycleService.load_in(tx,identity['lifecycle_id'])
                    allowed=LifecycleStopService.stop_allowed_in(tx,record,operation_kind='site',target=ref)
                if allowed:
                    await self.call(job,token,'stop',ref)
            with self.repository.transaction() as tx:
                return self.service.wait_in(tx,job,token,'original_operations_or_cancel_children_not_terminal')
        from .observation import ObservationService
        with self.repository.transaction() as tx:
            self.service.assert_owner_in(tx,job,token)
            observations=LifecycleSafetyService._rows_in(tx,'observations',identity['work_ref'])
            sampler_pending=sorted({r['body'].get('operation_id') for r in observations
                if not ObservationService.closed_in(tx,r) and r['body'].get('operation_id') in identity['operation_refs']})
        for ref in sampler_pending:
            await self.call(job,token,'stop',ref)
        with self.repository.transaction() as tx:
            self.service.assert_owner_in(tx,job,token)
            resources=LifecycleSafetyService._rows_in(tx,'resources',identity['work_ref'])
            if any(r['body'].get('operation_id') not in identity['operation_refs'] for r in resources):
                raise Conflict('resource_original_operation_ownership_missing')
            active=sorted(r['key'] for r in resources if r['body'].get('status')!='closed')
        if active:
            complete=await self.call(job,token,'cleanup',active)
            if not complete:
                raise Conflict('cleanup_response_unknown; runner_original_cleanup_query_capability_missing')
        with self.repository.transaction() as tx:
            return self.service.close_site_in(tx,job,token,ttl=self.lease_seconds)

    async def provider(self, job, token):
        if self.agent_provider is None:
            raise Conflict('agent_provider_not_connected')
        with self.repository.transaction() as tx:
            session=self.service.assert_owner_in(tx,job,token)
            target=session['identity']['target_ref']
            task,contract,record,remote=AgentTeamService.safety_target_in(tx,target)
        if not remote.terminal:
            await self.call(job,token,'query',target)
        with self.repository.transaction() as tx:
            self.service.assert_owner_in(tx,job,token)
            task,contract,record,remote=AgentTeamService.safety_target_in(tx,target)
            allowed=LifecycleStopService.stop_allowed_in(tx,record,operation_kind='provider',target=target)
        if not remote.terminal:
            if remote.receipt is not None and not remote.cancel_accepted and allowed:
                await self.call(job,token,'stop',target)
            with self.repository.transaction() as tx:
                return self.service.wait_in(tx,job,token,'original_provider_request_or_children_not_terminal')
        with self.repository.transaction() as tx:
            self.service.assert_owner_in(tx,job,token)
            task,contract,record,remote=AgentTeamService.safety_target_in(tx,target)
            fact={'task_ref':target,'request_id':target,'lifecycle_id':contract.lifecycle_id,
                'lineage_budget_ref':contract.lineage_budget_ref,'provider_id':contract.provider_id,
                'handle':remote.receipt.handle,'state':remote.receipt.state,'children_stopped':True,
                'receipt':remote.receipt.model_dump(mode='json'),'late_usage':remote.receipt.usage.model_dump(mode='json')
                    if remote.receipt.usage else None,'session_ref':session['ref'],'job_ref':job['job_id']}
            # 原 unknown 模型 settlement 保持不变；迟到 usage 仅追加事实。
            tx.put('agent_team_reconciliations',target,fact,immutable=True)
            row=tx.get('agent_team_tasks',target)
            tx.put('agent_team_tasks',target,row['body'] | {'status':'reconciled','reconciliation_ref':target})
            tx.put('lifecycle_reconciliations',session['ref'],session | {'state':'closed'})
            self.service.resolve_blocker_in(tx,session)
            result={'execution_status':'succeeded','output':fact}
            tx.put('reconciliation_job_results',job['job_id'],result,immutable=True)
            return result

    async def __call__(self, job):
        if not job['payload'].get('reconciliation_ref'):
            if self.legacy is None:
                return {'execution_status':'blocked','error':{'code':'LEGACY_RECOVERY_NOT_CONNECTED'}}
            return await self.legacy(job)
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx,job)
            old=tx.get('reconciliation_job_results',job['job_id'])
            if old:
                return old['body']
            closed=self.service.closed_result_in(tx,job)
            if closed is not None:
                tx.put('reconciliation_job_results',job['job_id'],closed,immutable=True)
                return closed
            session=tx.get('lifecycle_reconciliations',job['payload']['reconciliation_ref'])['body']
            token=tx.acquire_thread(job['thread_id'],self.owner,min(self.lease_seconds,job['deadline']-tx.now))
        owns=False
        try:
            LifecycleService(self.repository).claim(session['identity']['lifecycle_id'],token=token)
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx,job)
                LifecycleSafetyService.acquire_work_in(tx,job['scope_ref'],token,purpose='reconcile')
                owns=True
                self.service.assert_owner_in(tx,job,token)
            result=await (self.site(job,token) if session['identity']['operation_kind']=='site' else self.provider(job,token))
        except (Conflict,PermissionError,ValueError) as exc:
            if isinstance(exc,StaleOwner):
                raise
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx,job)
                LifecycleService.assert_owner_in(tx,session['identity']['lifecycle_id'],token)
                current=tx.get('lifecycle_reconciliations',session['ref'])['body']
                closed=self.service.closed_result_in(tx,job)
                if closed is not None:
                    result=closed
                else:
                    tx.put('lifecycle_reconciliations',session['ref'],current | {'state':'blocked'})
                    result=self.service.block_in(tx,session,str(exc))
        finally:
            with self.repository.transaction() as tx:
                if owns:
                    with suppress(StaleOwner):
                        LifecycleSafetyService.release_work_in(tx,job['scope_ref'],token)
                with suppress(StaleOwner):
                    tx.release_thread(token)
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx,job)
            tx.put('reconciliation_job_results',job['job_id'],result,immutable=True)
        return result
