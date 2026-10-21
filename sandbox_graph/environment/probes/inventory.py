"""Linux process inventory with stable PID identity and explicit observation gaps."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ProcessRule:
    component: str
    role: str
    executable_names: tuple[str, ...]

    def __post_init__(self):
        if not self.component or not self.role or not self.executable_names:
            raise ValueError("inventory rule requires a component, role and executable names")
        if any("/" in name or not name for name in self.executable_names):
            raise ValueError("inventory executable match must use basenames")


@dataclass(frozen=True)
class ProcessFact:
    pid: int
    start_ticks: int
    comm: str
    exe: str | None
    cgroup: str | None
    cpu_ticks: int
    pss_kib: int | None

    @property
    def identity(self) -> str:
        return f"{self.pid}:{self.start_ticks}"


@dataclass(frozen=True)
class InventoryResult:
    processes: tuple[dict, ...]
    ambiguous: tuple[dict, ...]
    pss_missing_pids: tuple[int, ...]
    shared_cgroups: tuple[str, ...]
    container_ids: tuple[str, ...]
    limitations: tuple[str, ...]


def parse_proc_stat(raw: str) -> dict[str, int | str]:
    # comm is parenthesized but may itself contain spaces and parentheses.
    opening, closing = raw.find("("), raw.rfind(")")
    if opening <= 0 or closing <= opening:
        raise ValueError("proc stat record is malformed")
    pid = int(raw[:opening].strip())
    fields = raw[closing + 1:].split()
    if len(fields) < 20:
        raise ValueError("proc stat record is truncated")
    return {"pid": pid, "comm": raw[opening + 1:closing], "state": fields[0],
            "utime": int(fields[11]), "stime": int(fields[12]), "start_ticks": int(fields[19])}


def _pss_kib(path: Path) -> int | None:
    try:
        for line in path.read_text(encoding="ascii", errors="replace").splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _container_id(cgroup: str | None) -> str | None:
    if not cgroup:
        return None
    match = re.search(r"(?:docker|cri-containerd|crio)[-/]([0-9a-f]{12,64})", cgroup)
    return match.group(1) if match else None


def collect_inventory(*, proc_root: str | Path = "/proc") -> tuple[tuple[ProcessFact, ...], tuple[str, ...]]:
    root = Path(proc_root)
    facts, limitations = [], []
    try:
        pids = sorted((entry for entry in root.iterdir() if entry.name.isdecimal()), key=lambda item: int(item.name))
    except OSError as exc:
        return (), (f"proc_root_unavailable:{type(exc).__name__}",)
    for directory in pids:
        try:
            stat_data = parse_proc_stat((directory / "stat").read_text(encoding="ascii"))
            try:
                exe = os.readlink(directory / "exe")
            except OSError:
                exe = None
                limitations.append(f"exe_unavailable:{stat_data['pid']}")
            try:
                cgroup = (directory / "cgroup").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                cgroup = None
                limitations.append(f"cgroup_unavailable:{stat_data['pid']}")
            facts.append(ProcessFact(pid=int(stat_data["pid"]), start_ticks=int(stat_data["start_ticks"]),
                comm=str(stat_data["comm"]), exe=exe, cgroup=cgroup,
                cpu_ticks=int(stat_data["utime"]) + int(stat_data["stime"]),
                pss_kib=_pss_kib(directory / "smaps_rollup")))
        except (OSError, ValueError, KeyError):
            # Process exit or permission races are recorded as coverage gaps, never zero resources.
            limitations.append(f"process_unreadable:{directory.name}")
    return tuple(facts), tuple(sorted(set(limitations)))


def classify_processes(facts: Iterable[ProcessFact], rules: Iterable[ProcessRule]) -> InventoryResult:
    rules = tuple(rules)
    facts = tuple(facts)
    output, ambiguous = [], []
    missing_pss = [fact.pid for fact in facts if fact.pss_kib is None]
    cgroup_counts: dict[str, int] = {}
    container_ids = set()
    for fact in facts:
        if fact.cgroup:
            cgroup_counts[fact.cgroup] = cgroup_counts.get(fact.cgroup, 0) + 1
            container_id = _container_id(fact.cgroup)
            if container_id:
                container_ids.add(container_id)
    for fact in facts:
        matches = [rule for rule in rules if fact.exe and Path(fact.exe).name in rule.executable_names]
        if len(matches) > 1:
            ambiguous.append({"pid": fact.pid, "start_ticks": fact.start_ticks,
                              "components": sorted(rule.component for rule in matches)})
            continue
        if not matches:
            continue
        rule = matches[0]
        output.append({"identity": fact.identity, "pid": fact.pid, "start_ticks": fact.start_ticks,
            "component": rule.component, "role": rule.role, "exe": fact.exe, "cgroup": fact.cgroup,
            "cpu_time": {"ticks": fact.cpu_ticks, "ticks_per_second": os.sysconf("SC_CLK_TCK")}, "pss_kib": fact.pss_kib,
            "observation": "complete" if fact.pss_kib is not None else "partial_pss_missing"})
    shared = tuple(sorted(group for group, count in cgroup_counts.items() if count > 1))
    return InventoryResult(tuple(output), tuple(ambiguous), tuple(sorted(missing_pss)), shared,
                           tuple(sorted(container_ids)), ())


__all__ = ["ProcessRule", "ProcessFact", "InventoryResult", "parse_proc_stat", "collect_inventory", "classify_processes"]
