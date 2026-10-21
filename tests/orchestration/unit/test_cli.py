import json
import subprocess
import sys


def test_cli_catalog_does_not_claim_runtime_adapters_verified():
    result=subprocess.run([sys.executable,'-m','sandbox_graph','catalog'],text=True,capture_output=True)
    assert result.returncode==0, result.stderr
    data=json.loads(result.stdout)
    assert len(data['nodes'])==45
    assert all(m['adapter_status']=='unverified' for m in data['methods'])
