#!/usr/bin/env python3
"""Collect Linux process PSS/CPU inventory using a fixed component match rules file."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sandbox_graph.environment.probes.inventory import ProcessRule, classify_processes, collect_inventory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rules", help="versioned JSON component rule file")
    mode.add_argument("--discovery", help="Audited product/roles/runtime_root JSON")
    parser.add_argument("--proc-root", default="/proc")
    parser.add_argument("--machine-id-file",default="/etc/machine-id")
    parser.add_argument('--snapshot-context',help='Explicit benchmark scope/cohort JSON for collector_procfs')
    parser.add_argument('--output',type=Path,help='Atomically publish the snapshot consumed by collector_procfs')
    args = parser.parse_args(argv)
    try:
        if args.discovery:
            from sandbox_graph.environment.cli import read_json
            from sandbox_graph.environment.probes.discovery import refresh_snapshot
            if not args.snapshot_context or args.output is None:raise ValueError('discovery requires context and snapshot output')
            profile=read_json(args.discovery);context=read_json(args.snapshot_context)
            if set(profile)!={'product','roles','runtime_root'} or set(context)!={'environment_ref','node','inventory_ref','work_id','cohort_ids'}:
                raise ValueError('exact discovery profile and cohort context required')
            result=refresh_snapshot(**profile,context=context,output=args.output,proc_root=args.proc_root,
                machine_id=Path(args.machine_id_file).read_text().strip(),
                boot_id=(Path(args.proc_root)/'sys/kernel/random/boot_id').read_text().strip())
            print(json.dumps({'complete':result['complete'],'snapshot_version':result['snapshot_version'],
                'missing_cohort_ids':result['missing_cohort_ids'],'process_count':len(result['processes'])}))
            return 0 if result['complete'] else 3
        raw = json.loads(Path(args.rules).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "rules"} or raw["schema_version"] != 1:
            raise ValueError("rule file schema is invalid")
        rules = tuple(ProcessRule(**row) for row in raw["rules"])
        machine_id=Path(args.machine_id_file).read_text().strip()
        boot_id=(Path(args.proc_root)/'sys/kernel/random/boot_id').read_text().strip()
        if not machine_id or not boot_id:raise ValueError('machine and boot identity required')
        facts, limitations = collect_inventory(proc_root=args.proc_root,machine_id=machine_id,boot_id=boot_id)
        result = classify_processes(facts, rules)
        if args.snapshot_context:
            import time
            from dataclasses import replace
            from sandbox_graph.environment.cli import read_json
            from sandbox_graph.environment.probes.inventory import benchmark_snapshot
            from sandbox_bench.runner import atomic_json
            if args.output is None:raise ValueError('snapshot output path required')
            context=read_json(args.snapshot_context)
            if set(context)!={'environment_ref','node','inventory_ref','work_id','cohort_ids','version'}:
                raise ValueError('exact benchmark snapshot context required')
            snapshot=benchmark_snapshot(replace(result,limitations=limitations),boot_id=boot_id,
                observed_monotonic_s=time.monotonic(),**context)
            atomic_json(args.output,snapshot)
            print(json.dumps({'status':'complete' if snapshot['complete'] else 'incomplete','output':str(args.output),
                'snapshot_version':context['version'],'process_count':len(snapshot['processes'])}))
            return 0 if snapshot['complete'] else 3
        output = {"schema_version": 1, "processes": result.processes,
            "ambiguous": result.ambiguous, "pss_missing_pids": result.pss_missing_pids,
            "shared_cgroups": result.shared_cgroups, "container_ids": result.container_ids,
            "limitations": tuple(sorted(set(limitations + result.limitations)))}
        print(json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0 if not output["limitations"] and not output["ambiguous"] and not output["pss_missing_pids"] else 3
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:200]}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
