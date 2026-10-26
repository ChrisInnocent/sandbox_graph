"""Modern repair orders use lifecycle ownership, never numeric node continuation."""
import asyncio
from contextlib import suppress

from ..graphs.executor import NodeExecutor
from ..persistence.repository import StaleOwner
from ..services.agent_lifecycle import LifecycleService
from ..services.lifecycle_safety import LifecycleSafetyService
from ..services.repair_outcomes import RepairOutcomeService
from .driver import Activation
from .tasks import TaskBackend


class LifecycleRepairWorker:
    def __init__(self, repository, node_service_factory, *, lease_seconds,
                 segment_seconds, owner='remedy', repair_catalog=None):
        self.repository = repository
        self.node_service_factory = node_service_factory
        self.lease_seconds = lease_seconds
        self.segment_seconds = segment_seconds
        self.owner = owner
        self.repair_catalog = repair_catalog

    async def __call__(self, job):
        with self.repository.transaction() as tx:
            order = RepairOutcomeService.load_order_in(tx, job)
            previous = tx.get('repair_outcomes', order['order_ref'])
            if previous:
                return previous['body']['return_result']
            token = tx.acquire_thread(job['thread_id'], self.owner,
                                      min(self.lease_seconds, job['deadline'] - tx.now))
        pulse = execution = None
        try:
            LifecycleService(self.repository).claim(order['lifecycle_id'], token=token)
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx, job)
                LifecycleSafetyService.acquire_work_in(tx, order['work_ref'], token, purpose='recovery')
                fresh = RepairOutcomeService.start_in(tx, job, order, token, ttl=self.lease_seconds, catalog=self.repair_catalog)
            if fresh:
                activation = Activation(activation_id=job['activation_id'], run_id=order['run_ref'],
                    thread_id=job['thread_id'], scope=order['work_ref'], plan_ref=order['plan_ref'],
                    graph_ref=order['graph_ref'], input_refs=order['input_refs'],
                    lease_seconds=self.lease_seconds, segment_seconds=self.segment_seconds)
                handlers = self.node_service_factory(token, activation)
                def fence(tx):
                    RepairOutcomeService.assert_current_in(tx, job, order, token, catalog=self.repair_catalog)
                executor = NodeExecutor(self.repository, handlers, token, additional_fence=fence)
                _, action, _ = self._current(job, order, token)

                async def heartbeat():
                    while True:
                        await asyncio.sleep(self.lease_seconds / 3)
                        with self.repository.transaction() as tx:
                            RepairOutcomeService.assert_current_in(tx, job, order, token, catalog=self.repair_catalog)
                            tx.renew_thread(token, min(self.lease_seconds, job['deadline'] - tx.now))

                pulse = asyncio.create_task(heartbeat())
                execution = asyncio.create_task(action.executor_route.execute(
                    executor, RepairOutcomeService.state(order, job)))
                with self.repository.transaction() as tx:
                    remaining = min(self.segment_seconds, job['deadline'] - tx.now)
                done, _ = await asyncio.wait({pulse, execution}, timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)
                if pulse in done:
                    pulse.result()
                if execution in done:
                    execution.result()
                else:
                    # Cancellation cannot prove a remote effect did not happen.
                    execution.cancel()
                    with suppress(asyncio.CancelledError):
                        await execution
            # A started execution without its exact receipt is reconciled, never
            # sent back through the action route. A receipt survives outcome cuts.
            with self.repository.transaction() as tx:
                return RepairOutcomeService.record_in(tx, job, order, token, ttl=self.lease_seconds, catalog=self.repair_catalog)
        except asyncio.CancelledError:
            # The enclosing Worker can time out before this handler's segment
            # timeout, and marks its job failed before cancellation reaches us.
            # Stop local children, then persist only uncertainty while the real
            # thread/lifecycle/work and exact task ownership are still current.
            for task in (execution, pulse):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
            with self.repository.transaction() as tx:
                if tx.get('repair_executions', order['order_ref']) and not tx.get('repair_outcomes', order['order_ref']):
                    with suppress(StaleOwner):
                        RepairOutcomeService.record_in(tx, job, order, token,
                            ttl=self.lease_seconds, interrupted=True, catalog=self.repair_catalog)
            raise
        finally:
            for task in (execution, pulse):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
            with self.repository.transaction() as tx:
                with suppress(StaleOwner):
                    LifecycleSafetyService.release_work_in(tx, order['work_ref'], token)
                with suppress(StaleOwner):
                    tx.release_thread(token)

    def _current(self, job, order, token):
        with self.repository.transaction() as tx:
            return RepairOutcomeService.assert_current_in(tx, job, order, token, catalog=self.repair_catalog)
