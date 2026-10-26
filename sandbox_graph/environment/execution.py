"""Adapter from frozen environment steps to the durable runtime OperationService."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..domain.models import Operation, ScriptRef, canonical_hash
from ..runtime.runner import RunnerStatus
from ..runtime.leases import LeaseService
from .contracts import ActionStep, RepairPlan, StepReceipt


class EnvironmentRunnerGateway:
    """Submits one immutable plan step at a time through existing fenced operations."""

    def __init__(self, operation_service: Any, *, action_scripts: dict[str, ScriptRef]):
        self.operation_service = operation_service
        self.action_scripts = dict(action_scripts)
        self.plan_refs = {}

    def bind_plan(self, plan: RepairPlan, ref: str) -> None:
        if not ref.startswith('artifact://'):
            raise ValueError('published plan artifact required')
        previous = self.plan_refs.get(plan.plan_hash)
        if previous is not None and previous != ref:
            raise ValueError('published plan reference changed')
        self.plan_refs[plan.plan_hash] = ref

    @staticmethod
    def _operation_id(plan: RepairPlan, step: ActionStep) -> str:
        return f"environment:{plan.plan_hash}:{step.step_id}"

    def _payload(self, plan: RepairPlan, step: ActionStep) -> dict[str, Any]:
        return {"plan_hash": plan.plan_hash, "plan_ref": self.plan_refs.get(plan.plan_hash, f"artifact://environment/plans/{plan.plan_hash}"),
                "step": step.model_dump(mode="json")}

    def _build_operation(self, *, plan: RepairPlan, step: ActionStep, scope: str,
                         run_id: str, deadline: float, dispatch_generation: int) -> Operation:
        if step not in plan.steps:
            raise ValueError("step is not part of the frozen repair plan")
        script = self.action_scripts.get(step.action_ref)
        if script is None:
            raise ValueError("environment action has no registered versioned script")
        payload = self._payload(plan, step)
        return Operation(operation_id=self._operation_id(plan, step), input_hash=canonical_hash(payload),
            script_ref=script, scope=scope, input=payload, deadline=deadline, node_id="C2",
            run_id=run_id, dispatch_generation=dispatch_generation)

    def _receipt(self, status: RunnerStatus, *, plan: RepairPlan, step: ActionStep) -> StepReceipt:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        state_map = {"not_submitted": "accepted", "running": "running", "completed": "completed",
                     "failed": "failed", "cancelled": "cancelled", "unknown": "unknown"}
        certainty = ("none" if status.state == "not_submitted" else
                     "confirmed" if status.authoritative and status.state in {"completed", "failed", "cancelled"} else
                     "unknown")
        payload = self._payload(plan, step)
        return StepReceipt(plan_ref=payload["plan_ref"], step_id=step.step_id,
            attempt=1, remote_handle=status.task_handle, started_at=now,
            finished_at=now if state_map[status.state] in {"completed", "failed", "cancelled"} else None,
            input_hash=canonical_hash(payload), output_refs=tuple(status.artifact_refs),
            effect_certainty=certainty, result=state_map[status.state], recovery_ref=status.reason)

    async def submit_step(self, *, plan: RepairPlan, step: ActionStep, scope: str, run_id: str,
                          deadline: float, dispatch_generation: int, token: Any) -> StepReceipt:
        operation = self._build_operation(plan=plan, step=step, scope=scope, run_id=run_id,
            deadline=deadline, dispatch_generation=dispatch_generation)
        status = RunnerStatus.model_validate(await self.operation_service.submit(operation, token))
        return self._receipt(status, plan=plan, step=step)

    async def query_step(self, *, plan: RepairPlan, step: ActionStep, scope: str, token: Any) -> StepReceipt:
        if step not in plan.steps:
            raise ValueError("step is not part of the frozen repair plan")
        operation_id = self._operation_id(plan, step)
        with self.operation_service.repository.transaction() as tx:
            tx.assert_fence(token)
            row = tx.get("operations", operation_id)
            if row is None:
                # Missing durable intent is not evidence that the remote side effect did not start.
                # Fence the plan domains before returning an unknown receipt.
                for domain in plan.conflict_domains:
                    lease=tx.get('resource_leases',domain)
                    if not lease or lease['body'].get('scope')!=scope:
                        raise PermissionError('cannot quarantine another environment scope')
                    tx.put('resource_leases',domain,lease['body']|{'status':'quarantine',
                        'reason':'durable environment operation record missing; resubmission prohibited'})
                status = RunnerStatus(state="unknown", authoritative=False,
                    reason="durable operation record is missing; plan domains quarantined")
            elif row["body"].get("scope") != scope:
                raise PermissionError("operation scope mismatch")
            elif row["body"].get("status"):
                status = RunnerStatus.model_validate(row["body"]["status"])
            else:
                status = RunnerStatus(state="unknown", authoritative=False,
                    reason="operation has no authoritative status; reconcile before any retry")
        return self._receipt(status, plan=plan, step=step)

    async def reconcile_step(self, *, plan: RepairPlan, step: ActionStep, scope: str, token: Any) -> StepReceipt:
        if step not in plan.steps or step.action_ref not in self.action_scripts:
            raise ValueError("step is not part of a registered frozen plan")
        # Never submit again here. Reconciliation is delegated to the durable operation service.
        status = RunnerStatus.model_validate(await self.operation_service.reconcile(
            self._operation_id(plan, step), scope, token))
        return self._receipt(status, plan=plan, step=step)


__all__ = ["EnvironmentRunnerGateway"]
