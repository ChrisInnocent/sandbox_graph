"""Epoch-fenced adapter for langgraph-checkpoint-postgres 3.2.0.

All saver SQL uses one transaction on one connection, locks thread_leases, and
validates the immutable activation token before checkpoint/blob/pending writes.
Pipeline mode is disabled. Administrative setup/deletion is not a worker API.
"""
import asyncio
from contextlib import contextmanager
from langgraph.checkpoint.postgres import PostgresSaver, _internal
from psycopg.rows import dict_row
from .repository import assert_fence_cursor

class FencedPostgresSaver(PostgresSaver):
    def __init__(self,conn,token,*,serde=None):
        super().__init__(conn,serde=serde)
        self._activation_token=token

    @contextmanager
    def _cursor(self,*,pipeline=False):
        with self.lock, _internal.get_connection(self.conn) as conn:
            # Transaction and lease-row lock cover every actual saver statement.
            # No separate preflight connection or deferred pipeline flush is used.
            with conn.transaction():
                with conn.cursor(binary=True,row_factory=dict_row) as cursor:
                    assert_fence_cursor(cursor,self._activation_token)
                    yield cursor

    def _scope(self,config):
        if config is None or config.get('configurable',{}).get('thread_id')!=self._activation_token.thread_id:
            raise PermissionError('saver thread differs from immutable activation lease')

    def put(self,config,checkpoint,metadata,new_versions):
        self._scope(config)
        return super().put(config,checkpoint,metadata,new_versions)
    def put_writes(self,config,writes,task_id,task_path=''):
        self._scope(config)
        return super().put_writes(config,writes,task_id,task_path)
    def get_tuple(self,config):
        self._scope(config)
        return super().get_tuple(config)
    def list(self,config,*,filter=None,before=None,limit=None):
        self._scope(config)
        if before: self._scope(before)
        yield from super().list(config,filter=filter,before=before,limit=limit)
    async def aput(self,config,checkpoint,metadata,new_versions):
        return await asyncio.to_thread(self.put,config,checkpoint,metadata,new_versions)
    async def aput_writes(self,config,writes,task_id,task_path=''):
        return await asyncio.to_thread(self.put_writes,config,writes,task_id,task_path)
    async def aget_tuple(self,config):
        return await asyncio.to_thread(self.get_tuple,config)
    async def alist(self,config,*,filter=None,before=None,limit=None):
        values=await asyncio.to_thread(lambda:list(self.list(config,filter=filter,before=before,limit=limit)))
        for value in values: yield value
    def setup(self): raise PermissionError('provision saver tables with an administrative connection before activating workers')
    def delete_thread(self,*args,**kwargs): raise PermissionError('worker cannot delete checkpoints')
    def delete_for_runs(self,*args,**kwargs): raise PermissionError('worker cannot delete checkpoints')
    def copy_thread(self,*args,**kwargs): raise PermissionError('worker cannot copy checkpoints across graph versions')
    def prune(self,*args,**kwargs): raise PermissionError('worker cannot prune checkpoints')
    async def adelete_thread(self,*args,**kwargs): self.delete_thread(*args,**kwargs)
    async def adelete_for_runs(self,*args,**kwargs): self.delete_for_runs(*args,**kwargs)
    async def acopy_thread(self,*args,**kwargs): self.copy_thread(*args,**kwargs)
    async def aprune(self,*args,**kwargs): self.prune(*args,**kwargs)
