#!/usr/bin/env python3
"""Deterministic local Linux host inventory helper; emits JSON only."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sandbox_graph.environment.probes.host import assess_role, collect_host_facts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True)
    args = parser.parse_args()
    facts = collect_host_facts()
    result = {"facts": asdict(facts), "role": assess_role(args.role, facts)}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
