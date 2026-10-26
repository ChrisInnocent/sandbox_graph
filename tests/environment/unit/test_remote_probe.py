import json
import pytest
from sandbox_graph.environment.probes.environment import RemoteEnvironmentProbe
from sandbox_graph.environment.probes.access import AccessResult
from sandbox_graph.environment.transport import SSHResult
from sandbox_graph.artifacts.store import FileArtifactStore
from tests.environment.unit.test_planner import declaration

class Resolver:
    def resolve(self,ref):return '/private/path'
class Access:
    async def check(self,node):return AccessResult(node.node_id,'reachable',authentication_status='authenticated',
        identity_status='confirmed',execution_status='allowed',observation={'boot_id':'boot','machine_id':node.machine_id})
class Transport:
    def __init__(self):self.calls=[]
    async def request(self,request):
        self.calls.append(request);action=request.payload['action']
        if action=='probe.host.v1':body={'facts':{'architecture':'x86_64'},'role':{'checks':{'kvm':{'status':'satisfied'}}}}
        elif action=='probe.product.v1':body={'installation':'absent','files':{},'services':[],'containers':[]}
        else:raise AssertionError(action)
        return SSHResult(0,json.dumps(body).encode(),b'')

@pytest.mark.asyncio
async def test_remote_check_retains_raw_evidence_and_does_not_mutate_or_claim_ready(tmp_path):
    transport=Transport();store=FileArtifactStore(tmp_path/'artifacts')
    probe=RemoteEnvironmentProbe(transport=transport,resolver=Resolver(),store=store,scope='check',access=Access())
    report=await probe.check(declaration(),('n1',),fresh=False)
    assert report.readiness=={'n1':'blocked'}
    assert report.observations[-1].status=='missing'
    assert report.observations[-1].repair_code=='PRODUCT_ABSENT'
    assert all(call.payload['action'].startswith('probe.') for call in transport.calls)
    assert report.artifact_manifest and all(probe.resolve(ref) for ref in report.artifact_manifest)
    assert all(row.evidence_refs for row in report.observations)

@pytest.mark.asyncio
async def test_unreachable_node_does_not_probe_or_disappear_from_report(tmp_path):
    class Denied:
        async def check(self,node):return AccessResult(node.node_id,'unreachable',failure_code='SSH_PORT_UNREACHABLE')
    transport=Transport();probe=RemoteEnvironmentProbe(transport=transport,resolver=Resolver(),
        store=FileArtifactStore(tmp_path),scope='check',access=Denied())
    report=await probe.check(declaration(),('n1',),fresh=True)
    assert not transport.calls and report.observations[0].status=='unknown' and report.readiness['n1']=='blocked'
