"""Read-only Linux host observations. No quota-based memory substitutions."""

import argparse
import json
import math
from pathlib import Path
import socket
import sys
import time


CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq",
              "steal", "guest", "guest_nice")


def _counters(fields):
    values = [int(value) for value in fields]
    if any(value < 0 for value in values):
        raise ValueError("negative kernel counter")
    return values


def sample_proc(proc_root="/proc") -> dict:
    """Return one host-scoped sample, raising on unavailable required evidence."""
    root = Path(proc_root)
    memory = {}
    for line in (root / "meminfo").read_text().splitlines():
        key, separator, raw = line.partition(":")
        if separator and key in {"MemTotal", "MemAvailable"}:
            fields = raw.split()
            if len(fields) != 2 or fields[1] != "kB" or key in memory:
                raise ValueError("invalid required memory metric")
            memory[key] = _counters(fields[:1])[0] * 1024
    if (set(memory) != {"MemTotal", "MemAvailable"}
            or memory["MemTotal"] <= 0
            or memory["MemAvailable"] > memory["MemTotal"]):
        raise ValueError("missing or inconsistent available memory evidence")

    cpu = None
    for line in (root / "stat").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            values = _counters(fields[1:])
            if len(values) < 4:
                raise ValueError("incomplete aggregate CPU counters")
            cpu = dict(zip(CPU_FIELDS, values))
            break
    if cpu is None:
        raise ValueError("missing aggregate CPU counters")

    fields = (root / "loadavg").read_text().split()
    if len(fields) != 5:
        raise ValueError("invalid load average record")
    loads = [float(value) for value in fields[:3]]
    if any(not math.isfinite(value) or value < 0 for value in loads):
        raise ValueError("invalid load average values")
    processes = _counters(fields[3].split("/"))
    if len(processes) != 2 or processes[0] > processes[1]:
        raise ValueError("invalid process counts")
    last_pid = _counters(fields[4:])[0]
    result = {
        "schema_version": "host_sample.v1", "time_ns": time.time_ns(),
        "host_id": socket.gethostname(),
        "mem_available_bytes": memory["MemAvailable"],
        "mem_total_bytes": memory["MemTotal"], "cpu_ticks": cpu,
        "loadavg": dict(zip(("one", "five", "fifteen"), loads),
                       runnable=processes[0], processes=processes[1], last_pid=last_pid),
    }
    try:
        disk_lines = (root / "diskstats").read_text().splitlines()
    except OSError as exc:
        result.setdefault("optional_errors", {})["diskstats"] = type(exc).__name__
    else:
        disks = {}
        for line in disk_lines:
            fields = line.split()
            if len(fields) < 14:
                raise ValueError("incomplete disk counter record")
            major, minor = _counters(fields[:2])
            disks[fields[2]] = {"major": major, "minor": minor,
                                "counters": _counters(fields[3:])}
        result["diskstats"] = disks
    try:
        net_lines = (root / "net/dev").read_text().splitlines()
    except OSError as exc:
        result.setdefault("optional_errors", {})["net_dev"] = type(exc).__name__
    else:
        interfaces = {}
        for line in net_lines:
            name, separator, raw = line.partition(":")
            if not separator:
                continue
            values = _counters(raw.split())
            if len(values) != 16:
                raise ValueError("incomplete network counter record")
            interfaces[name.strip()] = {"rx_bytes": values[0], "tx_bytes": values[8],
                                         "counters": values}
        result["net_dev"] = interfaces
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proc-root", default="/proc")
    args = parser.parse_args(argv)
    try:
        sample = sample_proc(args.proc_root)
    except (OSError, ValueError):
        print("Host sample unavailable: required evidence could not be collected.",
              file=sys.stderr)
        return 1
    print(json.dumps(sample, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
