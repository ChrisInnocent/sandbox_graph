"""Common measurement boundaries, independent of each product's HTTP semantics."""

import math
import time
import uuid


def error_record(exc):
    """Record a classification, never raw exception text or HTTP response secrets."""
    result = {"error_type": type(exc).__name__}
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        result["http_status"] = status
    return result


def operation_completion(adapter, operation, *, topology=None):
    """BCD-17 / D20：N05 按此决定操作是否轮询到终点（同步操作不重复计量）。"""
    from .operation_sync import completion_plan
    return completion_plan(getattr(adapter, "product", None), operation, topology=topology)


def probe(adapter, handle, timeout, marker=None):
    marker = marker or "sandbox-bench-" + uuid.uuid4().hex
    result = adapter.execute(handle, "printf '%s' '" + marker + "'", timeout)
    if result.get("exit_code") != 0:
        return False, "probe_exit_nonzero"
    if result.get("stdout") != marker:
        return False, "probe_output_mismatch"
    return True, None


def create_sync(adapter, run_id, timeout, poll_interval, *, clock=time.monotonic,
                sleep=time.sleep, on_created=None, completion="STATE_READY", endpoint=None):
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("timeout and poll interval must be positive")
    if completion not in ("API_COMPLETE", "STATE_READY", "EXEC_COMPLETE"):
        raise ValueError("unsupported completion; server logs require the audited observer")
    if endpoint is not None and getattr(endpoint, "endpoint_kind", None) == "guest_ready":
        raise ValueError("guest_ready endpoints are read via readiness, not create_sync")
    synchronous = bool(endpoint is not None and getattr(endpoint, "synchronous", False))
    started = clock()
    deadline = started + timeout
    row = {"operation_id": uuid.uuid4().hex, "run_id": run_id,
           "started_at_ns": time.time_ns(), "status": "failed",
           "unknown_create_outcome": False, "observations": [], "completion": completion}
    handle = None

    def finish(status, reason=None):
        row.update(status=status, elapsed_s=clock() - started)
        if reason:
            row["reason"] = reason
        return row, handle

    try:
        handle = adapter.create(run_id, max(.001, deadline - clock()))
        row["sandbox_id"] = handle["sandbox_id"]
        row["api_headers_s"] = handle["api_headers_s"]
        row["api_complete_s"] = handle["api_complete_s"]
        if on_created:
            on_created(handle)
    except Exception as exc:
        row["observations"].append(error_record(exc))
        row["unknown_create_outcome"] = handle is None
        return finish("timeout" if isinstance(exc, TimeoutError) else "failed", "create_error")

    if completion == "API_COMPLETE":
        if synchronous:
            row["state_ready_s"] = row["api_complete_s"]
        return finish("success" if clock() < deadline else "timeout")
    if synchronous:
        # A synchronous entry already returned after ready: record readiness at the same
        # moment and do not fabricate a second readiness delay by polling (BCD-13 §13.5).
        row.setdefault("state_ready_s", row["api_complete_s"])
        if completion == "STATE_READY":
            return finish("success" if clock() < deadline else "timeout")
    while clock() < deadline:
        try:
            if completion == "STATE_READY" or (adapter.product == "kuasar" and not synchronous):
                state = adapter.info(handle, deadline - clock()).get("state")
                row["observations"].append({"state": state, "offset_s": clock() - started})
                if state in ("failed", "stopped", "deleted", "paused"):
                    return finish("failed", "terminal_state_before_ready")
                if state != "running":
                    sleep(min(poll_interval, max(0, deadline - clock())))
                    continue
                row.setdefault("state_ready_s", clock() - started)
                if completion == "STATE_READY":
                    return finish("success" if clock() < deadline else "timeout")
            if clock() >= deadline:
                break
            ok, reason = probe(adapter, handle, deadline - clock())
            row["probe_attempts"] = row.get("probe_attempts", 0) + 1
            if clock() > deadline:
                return finish("timeout", "total_deadline")
            return finish("success" if ok else "failed", reason)
        except Exception as exc:
            row["probe_attempts"] = row.get("probe_attempts", 0) + 1
            row["observations"].append({**error_record(exc), "offset_s": clock() - started})
            if getattr(exc, "status", None) in (400, 401, 403, 405, 415):
                return finish("failed", "probe_contract_or_auth_error")
            sleep(min(poll_interval, max(0, deadline - clock())))
    return finish("timeout", "total_deadline")


def cleanup_owned(adapter, handles, timeout, *, on_result=None, total_timeout=None, clock=time.monotonic):
    results = []
    seen = set()
    deadline=clock()+total_timeout if total_timeout is not None else None
    for handle in handles:
        sid = handle["sandbox_id"]
        if sid in seen:
            continue
        seen.add(sid)
        row = {"sandbox_id": sid, "status": "api_deleted",
               "physical_resource_release": "unverified"}
        try:
            remaining=timeout if deadline is None else min(timeout,deadline-clock())
            if remaining<=0:raise TimeoutError('cleanup budget exhausted')
            adapter.delete(handle, remaining)
        except Exception as exc:
            row.update(status="failed", **error_record(exc))
        results.append(row)
        if on_result:
            on_result(row)
    return results


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(rows, wall_s):
    durations = [r["elapsed_s"] for r in rows if r["status"] == "success"]
    result = {"sample_count": len(rows), "success_count": len(durations),
              "failed_count": sum(r["status"] != "success" for r in rows),
              "batch_wall_s": wall_s,
              "successful_throughput_per_s": len(durations) / wall_s if wall_s > 0 else None,
              "latency_s": None, "percentile_method": "linear_interpolation"}
    if durations:
        result["latency_s"] = {
            "mean": sum(durations) / len(durations), "min": min(durations),
            "max": max(durations), **{name: percentile(durations, q)
                                    for name, q in (("p50", .5), ("p95", .95), ("p99", .99))}}
    return result
