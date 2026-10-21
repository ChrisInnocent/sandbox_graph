"""Budgeted snapshot restore verification, outside performance request accounting.

A separate durable operation is reserved before the single create request. Unknown
submission or validation never triggers a replacement create. Known identities are
kept for owned cleanup even when readiness or the independent assertion fails.
"""
from __future__ import annotations
import asyncio
import copy
from dataclasses import asdict
import inspect
import time

from .completion import Acceptance, CompletionObserver, CompletionSpec, StateObservation
from .journal import FileJournal
from .load import OperationResult
from .runner import digest


class RestoreBlocked(ValueError):
    pass


class RestoreVerifier:
    def __init__(self,*,case,adapter,folder,max_requests,timeout_s,environment,on_owned,
                 validator,can_submit,process_epoch,clock=time.monotonic,sleep=asyncio.sleep,
                 pin_source=None,bind_child_source=None):
        if type(max_requests) is not int or max_requests<=0 or max_requests>case.load.requests:
            raise ValueError('restore budget must be positive and fit the declared number of captures')
        CompletionSpec('STATE_READY',timeout_s,case.completion.poll_interval_s)
        self.case=case;self.adapter=adapter;self.timeout_s=timeout_s;self.environment=environment
        self.on_owned=on_owned;self.validator=validator;self.can_submit=can_submit
        self.pin_source=pin_source;self.bind_child_source=bind_child_source
        self.clock=clock;self.process_epoch=process_epoch
        self.observer=CompletionObserver(process_epoch=process_epoch,clock=clock,sleep=sleep)
        self.journal=FileJournal(folder,case.work_id+':snapshot-restore',child_budget=max_requests,parent_budget=max_requests)

    async def _call(self,function,*args,**kwargs):
        if inspect.iscoroutinefunction(function):return await function(*args,**kwargs)
        result=await asyncio.to_thread(function,*args,**kwargs)
        return await result if inspect.isawaitable(result) else result

    async def verify(self,source):
        index=source.get('request_index')
        if (type(index) is not int or index<0 or source.get('kind')!='snapshot'
                or not isinstance(source.get('source_id'),str) or not source['source_id']):
            raise RestoreBlocked('explicit captured source and request identity required')
        binding=digest({'case':self.case.digest(),'source_id':source['source_id'],'kind':source['kind'],
            'request_index':index,'environment':self.environment,'timeout_s':self.timeout_s})
        step='restore:'+str(index)
        cached=self.journal.receipt(step,binding)
        if cached is not None:
            if cached['status']=='unknown':raise RestoreBlocked('restore outcome requires reconciliation')
            return cached['status']=='valid'
        if not self.can_submit():raise RestoreBlocked('fixture submission not permitted')
        if not self.journal.begin_step(step,binding):
            raise RestoreBlocked('restore already started; reconcile without reposting')
        try:token=await self.journal.reserve(index,1)
        except ValueError as error:raise RestoreBlocked('restore request budget exhausted') from error
        began=self.clock();deadline=began+self.timeout_s
        submitted=False;handle=None;completion=None;assertion=None
        async def operation():
            nonlocal submitted,handle,completion,assertion
            adapter=copy.copy(self.adapter);adapter.config=copy.deepcopy(self.adapter.config)
            adapter.config['source']={'kind':source['kind'],'id':source['source_id']}
            body=adapter.config.setdefault('create_body',{})
            body['metadata']={**body.get('metadata',{}),'benchmark_fixture_role':'snapshot_restore',
                              'parent_source_id':source['source_id'],'benchmark_fixture_operation':token}
            self.journal.audit('restore_intent',operation_id=token,source_id=source['source_id'],
                               request_index=index,source_kind=source['kind'])
            consumer_ref=await self.pin_source(source,'restore:'+token) if self.pin_source else None
            if not self.can_submit():raise RestoreBlocked('fixture submission no longer permitted')
            await self.journal.submission_started(token)
            submitted=True
            handle=await self._call(adapter.create,self.case.work_id,deadline-self.clock())
            sid=handle['sandbox_id']
            self.journal.own('sandbox',sid,{'environment':self.environment,'source_id':source['source_id'],
                                         'fixture_role':'snapshot_restore'})
            self.journal.audit('restore_identity',operation_id=token,sandbox_id=sid,source_id=source['source_id'])
            await self.on_owned({'kind':'sandbox','resource_id':sid,'source_id':source['source_id'],
                                 'owner':self.case.work_id,'environment':self.case.environment_ref})
            if self.bind_child_source:await self.bind_child_source(source,sid,consumer_ref)
            async def observe(handle):
                row=await self._call(adapter.inspect,handle,deadline-self.clock())
                return StateObservation(row.get('state','unknown'),self.process_epoch)
            completion=await self.observer.observe(CompletionSpec('STATE_READY',self.timeout_s,
                self.case.completion.poll_interval_s),submitted_at=began,process_epoch=self.process_epoch,
                accepted=Acceptance(True,self.clock(),self.process_epoch,handle),inspect=observe)
            if completion.status!='success':return False
            assertion=await self.validator(handle,source)
            if type(assertion) is not bool:raise ValueError('restore validator must return an explicit boolean')
            return assertion
        try:
            valid=await self.observer._bounded(operation,deadline)
            status='success' if valid else 'failed'
            result_status='valid' if valid else 'invalid'
            reason=None if valid else 'restore_or_validation_failed'
        except asyncio.CancelledError:
            self.journal.audit('restore_interrupted',operation_id=token,sandbox_id=handle.get('sandbox_id') if handle else None)
            raise
        except Exception as error:
            status=('timeout' if isinstance(error,TimeoutError) else 'unknown') if submitted else 'unsent'
            result_status='unknown' if submitted else 'invalid'
            reason=type(error).__name__
        value={'status':result_status,'source_id':source['source_id'],'request_index':index,
            'sandbox_id':handle.get('sandbox_id') if handle else None,'reason':reason,
            'completion':asdict(completion) if completion else None,'validation':assertion,
            'performance_request':False,'started_s':began,'ended_s':self.clock(),'process_epoch':self.process_epoch}
        await self.journal.record_terminal(token,OperationResult(status,int(status in {'unknown','timeout'}),reason,value))
        self.journal.receipt(step,binding,value)
        return result_status=='valid'
