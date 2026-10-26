"""Explicit opt-in deployment; never invent a target or reuse a default host."""
import json
import os
from pathlib import Path
import pytest
from sandbox_graph.environment.bootstrap import deploy
from sandbox_graph.environment.bootstrap_remote import checksum,encode
from sandbox_graph.environment.transport import SSHRequest,SSHTransport


@pytest.mark.live
@pytest.mark.asyncio
async def test_bootstrap_real_ssh_reentry_and_fresh_helper_probe():
    contract=os.environ.get('SANDBOX_GRAPH_BOOTSTRAP_CONTRACT')
    if not contract:pytest.skip('BLOCKED: SANDBOX_GRAPH_BOOTSTRAP_CONTRACT requires an authorized SSH target and offline bundle')
    value=json.loads(Path(contract).read_text())
    assert set(value)=={'bundle','deployment'}
    deployment=value['deployment'];ssh=deployment['ssh'];bundle=Path(value['bundle'])
    manifest=json.loads((bundle/'manifest.json').read_text())
    request={key:val for key,val in deployment.items() if key!='ssh'}
    request.update(schema_version=1,manifest=manifest,bundle_sha256=checksum(encode(manifest)))
    first=await deploy(bundle=bundle,request=request,**ssh)
    assert await deploy(bundle=bundle,request=request,**ssh)==first
    transport=SSHTransport(helper_paths={ssh['address']:first['helper_path']})
    observed=await transport.request(SSHRequest(**ssh,payload={'action':'probe.access.v1','input':{'read_only':True}}))
    assert observed.returncode==0
    proof=json.loads(observed.stdout)
    assert proof['machine_id']==request['machine_id'] and proof['boot_id'] and proof['can_exec'] is True
