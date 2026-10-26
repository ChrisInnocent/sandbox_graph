#!/usr/bin/env python3
"""Prepare a finite ublk target for the existing authorized recipe/CAS pipeline."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sandbox_graph.environment.tuning import prepare_ublk_asset


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',required=True,type=int);parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--before-sha256',help='Omit only when the target modprobe configuration is confirmed absent')
    args=parser.parse_args(argv)
    value=prepare_ublk_asset(args.output,target=args.target,expected_config_sha256=args.before_sha256)
    with args.manifest.open('x') as stream:json.dump(value,stream,indent=2);stream.write('\n')
    print(json.dumps({'manifest':str(args.manifest),'evidence_level':'rendered_only'}));return 0

if __name__=='__main__':raise SystemExit(main())
