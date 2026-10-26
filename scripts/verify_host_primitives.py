"""Run explicit read-only EAT-30 host observations over SSH."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox_graph.environment.cli import read_json
from sandbox_graph.testing.host_acceptance import run_host_acceptance

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--input',type=Path,required=True)
    args=parser.parse_args();result=asyncio.run(run_host_acceptance(read_json(args.input)))
    print(json.dumps(result,sort_keys=True));raise SystemExit(0 if result['status']=='passed' else 2)
