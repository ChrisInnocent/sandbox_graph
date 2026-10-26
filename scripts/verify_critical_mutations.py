"""Run the fixed ATG-06 mutation campaign without editing the working tree."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox_graph.testing.critical_mutations import campaign

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=campaign(Path(__file__).resolve().parents[1],args.output)
    print(result['status'],sum(r['caught'] for r in result['results']),len(result['results']))
    raise SystemExit(0 if result['status']=='pass' else 1)
