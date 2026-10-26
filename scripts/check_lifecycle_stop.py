"""Fixed local/persistent stop gate. Missing required backend exits 2 (blocked)."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sandbox_graph.testing.local_gate import run_gate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=('stop-local','stop-persistent'),default='stop-local')
    args=parser.parse_args()
    profile=json.loads((ROOT/'config/gates'/f'{args.profile}.json').read_text())
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    output=ROOT/'artifacts/gates'/args.profile/stamp
    print(f'Gate evidence: {output}',flush=True)
    result=run_gate(ROOT,profile,output)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return {'pass':0,'fail':1,'blocked':2}[result['status']]


if __name__=='__main__':
    raise SystemExit(main())
