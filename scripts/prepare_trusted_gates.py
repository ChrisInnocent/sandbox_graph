"""Prepare fixed external controller suites; never issue a GateManifest."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox_graph.environment.cli import read_json
from sandbox_graph.testing.suite_catalog import compile_catalog

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(compile_catalog(read_json(args.input),args.output)))
