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
    parser.add_argument("--rules", required=True, help="versioned JSON component rule file")
    parser.add_argument("--proc-root", default="/proc")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(Path(args.rules).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "rules"} or raw["schema_version"] != 1:
            raise ValueError("rule file schema is invalid")
        rules = tuple(ProcessRule(**row) for row in raw["rules"])
        facts, limitations = collect_inventory(proc_root=args.proc_root)
        result = classify_processes(facts, rules)
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
