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
    argv_prefix: tuple[str, ...] = ()
    cgroup_path: str | None = None
    sandbox_id: str | None = None
    generation: str | None = None

    def __post_init__(self):
        if not self.component or not self.role or not self.executable_names:
            raise ValueError("inventory rule requires a component, role and executable names")
        if any("/" in name or not name for name in self.executable_names):
            raise ValueError("inventory executable match must use basenames")
        if self.cgroup_path is not None and (not self.cgroup_path.startswith('/') or self.cgroup_path=='/'
                or any(p in {'.','..',''} for p in self.cgroup_path[1:].split('/'))):
            raise ValueError('explicit owned cgroup subtree required')
        if bool(self.sandbox_id)!=bool(self.generation) or self.sandbox_id and not self.cgroup_path:
            raise ValueError('sandbox generation requires owned cgroup identity')


@dataclass(frozen=True)
class ProcessFact:
    pid: int
    start_ticks: int
    comm: str
    exe: str | None
    cgroup: str | None
    cpu_ticks: int
    pss_kib: int | None
    argv: tuple[str, ...] = ()
    machine_id: str | None = None
    boot_id: str | None = None
    ppid: int = 0

    @property
    def identity(self) -> str:
        return f"{self.machine_id}:{self.boot_id}:{self.pid}:{self.start_ticks}" if self.machine_id and self.boot_id else f"{self.pid}:{self.start_ticks}"


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
            "ppid": int(fields[1]), "utime": int(fields[11]), "stime": int(fields[12]), "start_ticks": int(fields[19])}


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


def collect_inventory(*, proc_root: str | Path = "/proc", machine_id: str | None = None, boot_id: str | None = None) -> tuple[tuple[ProcessFact, ...], tuple[str, ...]]:
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
            try:
                with (directory/'cmdline').open('rb') as stream:cmdline=stream.read(65537)
                if len(cmdline)>65536:raise ValueError('cmdline too large')
                argv=tuple(part.decode('utf-8') for part in cmdline.split(b'\0') if part)
            except (OSError,ValueError,UnicodeError):
                argv=();limitations.append('cmdline_unavailable:'+directory.name)
            pss=_pss_kib(directory / 'smaps_rollup')
            after=parse_proc_stat((directory / 'stat').read_text(encoding='ascii'))
            if (stat_data['pid'],stat_data['start_ticks'])!=(after['pid'],after['start_ticks']):
                limitations.append('pid_reused:'+directory.name)
                continue
            facts.append(ProcessFact(pid=int(stat_data["pid"]), start_ticks=int(stat_data["start_ticks"]),
                comm=str(stat_data["comm"]), exe=exe, cgroup=cgroup,
                cpu_ticks=int(stat_data["utime"]) + int(stat_data["stime"]),
                pss_kib=pss,argv=argv,machine_id=machine_id,boot_id=boot_id,ppid=int(stat_data["ppid"])))
        except (OSError, ValueError, KeyError):
            # Process exit or permission races are recorded as coverage gaps, never zero resources.
            limitations.append(f"process_unreadable:{directory.name}")
    return tuple(facts), tuple(sorted(set(limitations)))


def classify_processes(facts: Iterable[ProcessFact], rules: Iterable[ProcessRule]) -> InventoryResult:
    rules = tuple(rules)
    facts = tuple(facts)
    output, ambiguous = [], []
    missing_pss = []
    cgroup_counts: dict[str, int] = {}
    container_ids = set()
    for fact in facts:
        if fact.cgroup:
            cgroup_counts[fact.cgroup] = cgroup_counts.get(fact.cgroup, 0) + 1
            container_id = _container_id(fact.cgroup)
            if container_id:
                container_ids.add(container_id)
    for fact in facts:
        matches = [rule for rule in rules if fact.exe and Path(fact.exe).name in rule.executable_names
            and (not rule.argv_prefix or tuple(fact.argv[1:1+len(rule.argv_prefix)])==tuple(rule.argv_prefix))
            and (not rule.cgroup_path or any(path==rule.cgroup_path or path.startswith(rule.cgroup_path+'/')
                for path in (line.split(':',2)[-1] for line in (fact.cgroup or '').splitlines())))]
        if matches and fact.pss_kib is None:missing_pss.append(fact.pid)
        if len(matches) > 1:
            ambiguous.append({"pid": fact.pid, "start_ticks": fact.start_ticks,
                              "components": sorted(rule.component for rule in matches)})
            continue
        if not matches:
            continue
        rule = matches[0]
        output.append({"identity": fact.identity, "pid": fact.pid, "start_ticks": fact.start_ticks,
            "machine_id":fact.machine_id,"boot_id":fact.boot_id,"component": rule.component, "role": rule.role, "exe": fact.exe, "cgroup": fact.cgroup,
            "cpu_time": {"ticks": fact.cpu_ticks, "ticks_per_second": os.sysconf("SC_CLK_TCK")}, "pss_kib": fact.pss_kib,
            'sandbox_id':rule.sandbox_id,'generation':rule.generation,
            "observation": "complete" if fact.pss_kib is not None else "partial_pss_missing"})
    shared = tuple(sorted(group for group, count in cgroup_counts.items() if count > 1))
    return InventoryResult(tuple(output), tuple(ambiguous), tuple(sorted(missing_pss)), shared,
                           tuple(sorted(container_ids)), ())


__all__ = ["ProcessRule", "ProcessFact", "InventoryResult", "parse_proc_stat", "collect_inventory", "classify_processes"]


def product_rules(product,roles):
    """Audited process names; subcommand prefix disambiguates shared binaries."""
    from ..products import cubesandbox,kuasar,agentenv
    module={'cubesandbox':cubesandbox,'kuasar':kuasar,'agentenv':agentenv}.get(product)
    if module is None or not roles or not set(roles)<=set(module.ROLE_COMPONENTS):
        raise ValueError('registered product roles required')
    rules=[]
    for role in roles:
        if product=='kuasar' and role in {'conductor','proxy','registry','router','placer'}:
            binary='node-ctl' if role in {'conductor','proxy'} else 'cluster-ctl'
            rules.append(ProcessRule(role,role,(binary,),argv_prefix=(role,)))
        elif product=='cubesandbox':
            names={'CubeOps':('cubeops',),'CubeMaster':('cubemaster',),'CubeAPI':('cube-api',),
                'CubeTemplateCenter':('templatecenter',),'cubeproxy':('nginx','openresty')}
            rules.extend(ProcessRule(component,role,names.get(component,(component,))) for component in module.ROLE_COMPONENTS[role])
        else:
            rules.extend(ProcessRule(component,role,(component,)) for component in module.ROLE_COMPONENTS[role])
    return tuple(rules)


def product_inventory(product,roles,*,proc_root,machine_id,boot_id):
    from dataclasses import asdict
    if not machine_id or not boot_id:raise ValueError('current machine and boot identity required')
    facts,limitations=collect_inventory(proc_root=proc_root,machine_id=machine_id,boot_id=boot_id)
    result=classify_processes(facts,product_rules(product,roles))
    body=asdict(result);body['limitations']=limitations
    body.update(schema_version=2,machine_id=machine_id,boot_id=boot_id,product=product)
    return body


def benchmark_snapshot(result,*,environment_ref,node,boot_id,inventory_ref,work_id,cohort_ids,version,observed_monotonic_s):
    """Convert observed owned process facts to the existing collector_procfs wire contract."""
    import math
    from sandbox_bench.runner import digest
    if (not all(isinstance(v,str) and v for v in (environment_ref,node,boot_id,inventory_ref,work_id))
            or type(version) is not int or version<1
            or type(observed_monotonic_s) not in (int,float) or not math.isfinite(observed_monotonic_s) or observed_monotonic_s<0
            or not isinstance(cohort_ids,list) or any(not isinstance(v,str) or not v for v in cohort_ids)
            or len(set(cohort_ids))!=len(cohort_ids)):
        raise ValueError('explicit bounded benchmark inventory identity required')
    processes=[]
    for row in result.processes:
        if row['boot_id']!=boot_id:raise ValueError('inventory boot differs')
        sandbox=row.get('sandbox_id')
        if sandbox and sandbox not in cohort_ids:raise PermissionError('process belongs to another sandbox cohort')
        processes.append({'identity':{'environment':environment_ref,'node':node,'boot_id':boot_id,
            'pid':row['pid'],'start_ticks':row['start_ticks']},'roles':[row['role']],
            'components':[row['component']],'sandbox_id':sandbox,'generation':row.get('generation'),'cgroup':row['cgroup'],
            **{key:row[key] for key in ('generation_kind','attribution_source') if key in row}})
    missing=sorted(set(cohort_ids)-{p['sandbox_id'] for p in processes if p['sandbox_id']})
    return {'schema_version':1,'environment_ref':environment_ref,'node':node,'boot_id':boot_id,
        'inventory_ref':inventory_ref,'work_id':work_id,'clock_domain':digest([environment_ref,node,boot_id]),
        'snapshot_version':version,'observed_monotonic_s':observed_monotonic_s,'cohort_ids':cohort_ids,
        'processes':processes,'complete':bool(processes) and not (missing or result.ambiguous or result.pss_missing_pids or result.limitations),
        'missing_cohort_ids':missing,'limitations':list(result.limitations),'ambiguous':list(result.ambiguous)}
