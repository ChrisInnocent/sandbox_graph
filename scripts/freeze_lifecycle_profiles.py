"""Maintainer-only collection snapshot; execution never refreshes its required IDs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def main():
    old=json.loads((ROOT/'config/gates/stop-local.json').read_text())
    profiles={
        'development':(sorted(set(old['paths'])|{str(path.relative_to(ROOT)) for folder in
            ('tests/orchestration/unit','tests/orchestration/graphs','tests/environment/unit','tests/environment/protocol')
            for path in (ROOT/folder).glob('test_*.py')}),[]),
        'persistent':(sorted(str(path.relative_to(ROOT)) for folder in ('tests/orchestration/persistence','tests/orchestration/recovery')
            for path in (ROOT/folder).glob('test_*.py')),['SANDBOX_GRAPH_TEST_POSTGRES_DSN']),
        'isolation':(['tests/orchestration/isolation/test_live_isolation.py'],['SANDBOX_GRAPH_TEST_ISOLATION_CONTRACT']),
        'release':(['tests/orchestration/e2e/test_end_to_end.py'],['SANDBOX_GRAPH_TEST_POSTGRES_DSN','SANDBOX_GRAPH_TEST_PRODUCT_ENV'])}
    for name,(paths,requires) in profiles.items():
        with tempfile.TemporaryDirectory(prefix='lifecycle-collection-') as folder:
            report=Path(folder)/'collection.json';env=dict(os.environ,SANDBOX_GATE_REPORT=str(report),PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
            result=subprocess.run([sys.executable,'-m','pytest','--collect-only','-q','-p','pytest_asyncio.plugin',
                '-p','sandbox_graph.testing.pytest_receipts',*paths],cwd=ROOT,env=env,capture_output=True,text=True,timeout=120)
            if result.returncode:raise RuntimeError(result.stdout+result.stderr)
            collection=json.loads(report.read_text())
            if collection['collection_errors'] or not collection['collected']:raise ValueError('incomplete gate collection')
            value={'schema_version':1,'profile':'lifecycle-'+name,'paths':paths,'required_ids':collection['collected'],'requires':requires}
            (ROOT/'config/gates'/f'lifecycle-{name}.json').write_text(json.dumps(value,indent=2)+'\n')
            print(name,len(collection['collected']))

if __name__=='__main__':main()
