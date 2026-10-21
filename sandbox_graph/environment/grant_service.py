"""PostgreSQL-backed grant issuance fenced by thread and resource leases."""
from __future__ import annotations

from ..domain.models import canonical_hash
from .permits import ExecutionGrantIssuer


class FencedExecutionGrantService:
    def __init__(self, repository, issuer: ExecutionGrantIssuer):
        self.repository = repository
        self.issuer = issuer

    def issue(self, *, token, operation_id: str, step_id: str, run_id: str, thread_id: str,
              owner_epoch: int, scope: str, machine_id: str, resource_domains: tuple[str, ...] | list[str],
              action: str, input_data: dict, ttl_seconds: float = 30):
        domains = tuple(sorted(set(resource_domains)))
        if not domains:
            raise ValueError("execution grant requires at least one resource conflict domain")
        if not operation_id or not step_id or not run_id or not machine_id:
            raise ValueError("execution grant identity is incomplete")
        generations = {}
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            if getattr(token, "thread_id", thread_id) != thread_id or getattr(token, "owner_epoch", owner_epoch) != owner_epoch:
                raise PermissionError("thread fencing identity mismatch")
            for domain in domains:
                lease = tx.get("resource_leases", domain)
                body = lease["body"] if lease else None
                if not body or body.get("scope") != scope or body.get("run_id") != run_id or \
                   body.get("thread_id") != thread_id or body.get("owner_epoch") != owner_epoch or \
                   body.get("status") != "reserved":
                    raise PermissionError(f"resource lease is not held by this execution: {domain}")
                key = canonical_hash({"scope": scope, "domain": domain})
                previous = tx.get("execution_generations", key)
                generation = int(previous["body"]["generation"]) + 1 if previous else 1
                tx.put("execution_generations", key, {"scope": scope, "domain": domain,
                    "run_id": run_id, "thread_id": thread_id, "owner_epoch": owner_epoch,
                    "generation": generation})
                generations[domain] = generation
            return self.issuer.issue(operation_id=operation_id, step_id=step_id, run_id=run_id,
                thread_id=thread_id, owner_epoch=owner_epoch, machine_id=machine_id,
                resource_generations=generations, action=action, input_data=input_data,
                ttl_seconds=ttl_seconds)


__all__ = ["FencedExecutionGrantService"]
