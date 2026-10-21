"""Linux procfs readers and attribution. Frozen trees are supported for tests.

No RSS fallback, guest-size estimate, or implicit missing-resource zero is used.
"""
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import os
import re
import time


@dataclass(frozen=True)
class ProcessIdentity:
    environment: str
    node: str
    boot_id: str
    pid: int
    start_ticks: int


def parse_proc_stat(text):
    """Parse through field 22; comm may contain whitespace and parentheses."""
    match = re.fullmatch(r'(\d+) \(.*\) (\S) (.*)\s*', text.strip(), re.DOTALL)
    if not match:
        raise ValueError('invalid proc stat')
    fields = match.group(3).split()  # field 4 onward
    if len(fields) < 19:
        raise ValueError('incomplete proc stat')
    try:
        values = [int(match.group(1)), int(fields[0]), int(fields[10]), int(fields[11]), int(fields[18])]
    except ValueError as exc:
        raise ValueError('invalid proc stat counters') from exc
    if any(v < 0 for v in values) or values[0] == 0:
        raise ValueError('negative proc stat counter or invalid pid')
    return dict(zip(('pid','ppid','utime_ticks','stime_ticks','start_ticks'), values))


def parse_pss(text):
    values = []
    for line in text.splitlines():
        key, sep, raw = line.partition(':')
        if sep and key.strip() == 'Pss':
            fields = raw.split()
            if len(fields) != 2 or fields[1] != 'kB' or not fields[0].isdigit():
                raise ValueError('invalid Pss metric')
            values.append(int(fields[0]) * 1024)
    if len(values) != 1:
        raise ValueError('missing or duplicate Pss metric')
    return values[0]


def parse_cgroup_cpu(text):
    found = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in {'usage_usec','user_usec','system_usec'}:
            if len(fields) != 2 or not fields[1].isdigit() or fields[0] in found:
                raise ValueError('invalid or duplicate cgroup counter')
            found[fields[0]] = int(fields[1]) / 1_000_000
    if 'usage_usec' not in found:
        raise ValueError('missing usage_usec')
    return {key.replace('_usec','_seconds'): value for key,value in found.items()}


def sample_process(proc_root, pid, *, environment, node, clock=time.monotonic,
                   clock_ticks=None, read_text=None):
    """Read identity before/after PSS and CPU. Failure carries its interval/reason.

    read_text is an injectable filesystem reader, useful for deterministic races.
    CLK_TCK defaults to the host value; remote/frozen trees must supply their value.
    """
    if type(pid) is not int or pid <= 0 or not environment or not node:
        raise ValueError('positive pid and explicit environment/node required')
    ticks = os.sysconf('SC_CLK_TCK') if clock_ticks is None else clock_ticks
    if type(ticks) is not int or ticks <= 0:
        raise ValueError('invalid CLK_TCK')
    root, reader = Path(proc_root), read_text or (lambda p: p.read_text())
    result = {'status':'unavailable','pss_bytes':None,'cpu_seconds':None,
              'started_s':clock(),'clock_ticks':ticks,'identity':None}
    try:
        boot_before = reader(root / 'sys/kernel/random/boot_id').strip()
        before = parse_proc_stat(reader(root / str(pid) / 'stat'))
        if not boot_before or before['pid'] != pid:
            raise ValueError('invalid identity')
        identity = ProcessIdentity(environment,node,boot_before,pid,before['start_ticks'])
        result['identity'] = asdict(identity)
        pss = parse_pss(reader(root / str(pid) / 'smaps_rollup'))
        after = parse_proc_stat(reader(root / str(pid) / 'stat'))
        boot_after = reader(root / 'sys/kernel/random/boot_id').strip()
        if (boot_after != boot_before or after['pid'] != pid or
                after['start_ticks'] != before['start_ticks']):
            result['reason'] = 'identity_changed'
        else:
            result.update(status='valid',pss_bytes=pss,
                          cpu_seconds=(after['utime_ticks']+after['stime_ticks'])/ticks,
                          cpu_ticks=after['utime_ticks']+after['stime_ticks'])
    except (OSError, ValueError) as exc:
        result['reason'] = {PermissionError:'permission_denied', FileNotFoundError:'process_or_metric_missing'}.get(type(exc),type(exc).__name__)
    result['ended_s'] = clock()
    return result


def _identity_key(identity):
    if isinstance(identity, ProcessIdentity):
        identity = asdict(identity)
    keys = ('environment','node','boot_id','pid','start_ticks')
    if not isinstance(identity,dict) or any(k not in identity for k in keys):
        raise ValueError('incomplete process identity')
    return tuple(identity[k] for k in keys)


def deduplicate_inventory(rows):
    """One address space per identity; conflicting sandbox generation is invalid."""
    grouped = {}
    for row in rows:
        key = _identity_key(row['identity'])
        if row.get('tgid',key[3]) != key[3]:
            leader=row.get('thread_group_identity')
            if leader is None:
                raise ValueError('thread requires verified leader identity')
            leader_key=_identity_key(leader)
            if leader_key[:3]!=key[:3] or leader_key[3]!=row['tgid']:
                raise ValueError('thread leader domain mismatch')
            key=leader_key
        ownership = (row.get('sandbox_id'),row.get('generation'))
        if key not in grouped:
            grouped[key] = {**row,'identity':row.get('thread_group_identity',row['identity']),
                            'roles':[],'components':[]}
        entry = grouped[key]
        if (entry.get('sandbox_id'),entry.get('generation')) != ownership:
            raise ValueError('conflicting sandbox ownership')
        for field, plural in [('role','roles'),('component','components')]:
            values=list(row.get(plural,[]))
            if row.get(field) is not None:values.append(row[field])
            for value in values:
                if not isinstance(value,str) or not value:raise ValueError('inventory role/component must be nonempty strings')
                if value not in entry[plural]:entry[plural].append(value)
    return list(grouped.values())


def validate_counter_scopes(rows):
    """Reject process/cgroup and parent/child overlap before any summation."""
    def inside(child, parent):
        return child == parent or parent in PurePosixPath(child).parents
    def path(value):
        p = PurePosixPath(value)
        if not p.is_absolute() or '..' in p.parts:
            raise ValueError('absolute canonical cgroup path required')
        return p
    for index, row in enumerate(rows):
        if row.get('kind') not in {'process','cgroup'} or not row.get('domain'):
            raise ValueError('counter scope needs kind/domain')
        if row['kind'] == 'cgroup':
            path(row['path'])
        elif not row.get('identity'):
            raise ValueError('process counter needs identity')
        for other in rows[:index]:
            if row['domain'] != other['domain']:
                continue
            if row['kind'] == other['kind'] == 'process':
                if not row.get('identity') or not other.get('identity'):
                    raise ValueError('process counter needs identity')
                if row['identity'] == other['identity']:
                    raise ValueError('duplicate process counter')
                continue
            if row['kind'] == other['kind'] == 'cgroup':
                a,b=path(row['path']),path(other['path'])
                if inside(a,b) or inside(b,a):
                    raise ValueError('overlapping cgroup counters')
            else:
                proc,group=(row,other) if row['kind']=='process' else (other,row)
                if not proc.get('cgroup'):
                    raise ValueError('cannot prove process/cgroup disjointness')
                if inside(path(proc['cgroup']),path(group['path'])):
                    raise ValueError('process and cgroup overlap')
    return list(rows)


def guest_net_pss(raw_bytes, proof, *, domain, window, coverage=1.0):
    result={'formula_version':'guest_pss.v1','raw_pss_bytes':raw_bytes,
            'net_pss_bytes':None,'deduction':proof,'status':'unavailable'}
    required=('definition','domain','window','coverage','method','version','source','deduction_bytes')
    if type(raw_bytes) is not int or raw_bytes < 0:
        return dict(result,status='invalid',reason='invalid_raw_pss')
    if not isinstance(proof,dict) or any(k not in proof for k in required):
        return dict(result,reason='missing_guest_definition')
    if any(not proof[k] for k in ('definition','method','version','source')):
        return dict(result,reason='missing_guest_definition')
    if proof['domain'] != domain or proof['window'] != window:
        return dict(result,reason='domain_or_window_mismatch')
    if coverage != 1 or proof['coverage'] != 1:
        return dict(result,reason='incomplete_coverage')
    value=proof['deduction_bytes']
    if type(value) is not int or not 0 <= value <= raw_bytes:
        return dict(result,status='invalid',reason='deduction_out_of_range')
    return dict(result,status='valid',net_pss_bytes=raw_bytes-value)
