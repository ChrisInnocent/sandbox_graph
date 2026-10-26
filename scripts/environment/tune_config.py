#!/usr/bin/env python3
"""Render source-audited configuration assets for the normal grant/CAS repair path."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sandbox_graph.environment.cli import read_json
from sandbox_graph.environment.tuning import prepare_asset


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True,help='Version 1 tuning contract JSON')
    p.add_argument('--manifest',type=Path,required=True)
    args=p.parse_args(argv);contract=read_json(args.input)
    fields={'schema_version','product','component','source','output','remote_path','targets','budgets'}
    if set(contract)!=fields or contract['schema_version']!=1:raise ValueError('explicit versioned tuning contract required')
    result=prepare_asset(**{key:value for key,value in contract.items() if key!='schema_version'})
    with args.manifest.open('x') as stream:json.dump(result,stream,sort_keys=True,indent=2);stream.write('\n')
    print(json.dumps({'manifest':str(args.manifest),'evidence_level':'rendered_only'}));return 0

if __name__=='__main__':raise SystemExit(main())
