import asyncio

import pytest

from sandbox_graph.environment.contracts import NodeDeclaration
from sandbox_graph.environment.probes.access import AccessProbe
from sandbox_graph.environment.transport import SSHResult


class Resolver:
    def __init__(self, values): self.values = values
    def resolve(self, ref): return self.values[ref]


class FakeTransport:
    def __init__(self, result=None, error=None): self.result=result; self.error=error; self.requests=[]
    async def request(self, request):
        self.requests.append(request)
        if self.error: raise self.error
        return self.result


def node():
    return NodeDeclaration(node_id="n1", machine_id="machine-1", roles=("compute",),
        ssh_address="host.example", ssh_user="bench", known_hosts_ref="artifact://known",
        automation_credential_ref="secret://ssh/key")


async def dns_ok(_host, _port): return ["192.0.2.1"]
async def tcp_ok(_host, _port, _timeout): return True
async def tcp_fail(_host, _port, _timeout): return False


def test_access_probe_separates_network_identity_auth_and_permissions():
    response = SSHResult(0, b'{"machine_id":"machine-1","uid":1001,"can_exec":true,"tools":["systemctl"]}', b"")
    transport = FakeTransport(result=response)
    probe = AccessProbe(transport=transport, resolver=Resolver({"artifact://known":"/tmp/known", "secret://ssh/key":"/tmp/key"}),
        dns_probe=dns_ok, tcp_probe=tcp_ok)
    result = asyncio.run(probe.check(node()))
    assert result.network_status == "reachable"
    assert result.identity_status == "confirmed"
    assert result.authentication_status == "authenticated"
    assert result.execution_status == "allowed"
    assert result.observation["tools"] == ["systemctl"]
    assert transport.requests[0].payload["action"] == "probe.access.v1"


def test_access_probe_does_not_try_ssh_or_mutation_after_tcp_failure():
    transport = FakeTransport(result=SSHResult(0, b"{}", b""))
    probe = AccessProbe(transport=transport, resolver=Resolver({}), dns_probe=dns_ok, tcp_probe=tcp_fail)
    result = asyncio.run(probe.check(node()))
    assert result.network_status == "unreachable"
    assert result.authentication_status == "not_attempted"
    assert transport.requests == []


def test_access_probe_blocks_changed_machine_identity():
    response = SSHResult(0, b'{"machine_id":"other-host","uid":1001,"can_exec":true,"tools":[]}', b"")
    probe = AccessProbe(transport=FakeTransport(response),
        resolver=Resolver({"artifact://known":"/tmp/known", "secret://ssh/key":"/tmp/key"}),
        dns_probe=dns_ok, tcp_probe=tcp_ok)
    result = asyncio.run(probe.check(node()))
    assert result.identity_status == "changed"
    assert result.execution_status == "blocked"
