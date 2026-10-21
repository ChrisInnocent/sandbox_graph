import asyncio
import json

import pytest

from sandbox_graph.environment.probes.access import AccessResult
from sandbox_graph.environment.scripts.authorized_key import (
    ensure_authorized_key,
    merge_authorized_keys,
    validate_public_key,
)
from sandbox_graph.environment.transport import SSHResult


ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA bench"


class Resolver:
    def resolve(self, ref):
        return {"artifact://known":"/tmp/known", "secret://ssh/initial":"/tmp/initial", "secret://ssh/auto":"/tmp/auto"}[ref]


class FakeTransport:
    def __init__(self, response): self.responses=list(response) if isinstance(response,list) else [response]; self.requests=[]
    async def request(self, request): self.requests.append(request); return self.responses.pop(0)


def test_merge_is_idempotent_and_preserves_existing_authorized_keys():
    old = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB old\n"
    key, _ = validate_public_key(ED25519)
    merged, changed = merge_authorized_keys(old, key)
    assert changed is True
    assert merged.startswith(old)
    merged_again, changed_again = merge_authorized_keys(merged, key)
    assert merged_again == merged
    assert changed_again is False


def test_public_key_parser_rejects_options_or_shell_text():
    with pytest.raises(ValueError, match="public key"):
        validate_public_key("command=touch /tmp/x ssh-ed25519 AAAA comment")


def test_key_write_uses_initial_access_and_requires_fresh_automation_login():
    key, fingerprint = validate_public_key(ED25519)
    transport = FakeTransport([
        SSHResult(0,json.dumps({"machine_id":"m1","present":False,"authorized_keys_sha256":None}).encode(),b""),
        SSHResult(0, json.dumps({"fingerprint":fingerprint,"changed":True}).encode(), b"")])
    checked=[]
    async def verify(node):
        checked.append(node.node_id)
        return AccessResult(node.node_id,"reachable",authentication_status="authenticated",
            identity_status="confirmed",execution_status="allowed")
    async def grant_provider(**kwargs):
        assert kwargs["action"] == "ssh.authorized_key.ensure.v1"
        return {"grant_id":"grant-1"}
    from sandbox_graph.environment.contracts import NodeDeclaration
    node = NodeDeclaration(node_id="n1", machine_id="m1", roles=("compute",), ssh_address="host.example",
        ssh_user="bench", known_hosts_ref="artifact://known",
        initial_credential_ref="secret://ssh/initial", automation_credential_ref="secret://ssh/auto")
    result = asyncio.run(ensure_authorized_key(node, public_key=key, transport=transport,
        resolver=Resolver(), verify_new_connection=verify, grant_provider=grant_provider))
    assert result.changed
    assert result.verified
    assert checked == ["n1"]
    assert transport.requests[0].payload["action"] == "ssh.authorized_key.inspect.v1"
    assert transport.requests[1].payload["action"] == "ssh.authorized_key.ensure.v1"


def test_existing_key_is_reused_without_mutation_grant():
    key, _ = validate_public_key(ED25519)
    transport=FakeTransport(SSHResult(0,json.dumps({"machine_id":"m1","present":True,
        "authorized_keys_sha256":"a"*64}).encode(),b""))
    async def verify(node):
        return AccessResult(node.node_id,"reachable",authentication_status="authenticated",
            identity_status="confirmed",execution_status="allowed")
    from sandbox_graph.environment.contracts import NodeDeclaration
    node=NodeDeclaration(node_id="n1",machine_id="m1",roles=("compute",),ssh_address="host.example",
        ssh_user="bench",known_hosts_ref="artifact://known",initial_credential_ref="secret://ssh/initial",
        automation_credential_ref="secret://ssh/auto")
    result=asyncio.run(ensure_authorized_key(node,public_key=key,transport=transport,resolver=Resolver(),verify_new_connection=verify))
    assert result.changed is False and result.verified
    assert len(transport.requests)==1


def test_new_key_does_not_mutate_without_execution_grant_provider():
    key,_=validate_public_key(ED25519)
    transport=FakeTransport(SSHResult(0,json.dumps({"machine_id":"m1","present":False,
        "authorized_keys_sha256":None}).encode(),b""))
    from sandbox_graph.environment.contracts import NodeDeclaration
    node=NodeDeclaration(node_id="n1",machine_id="m1",roles=("compute",),ssh_address="host.example",
        ssh_user="bench",known_hosts_ref="artifact://known",initial_credential_ref="secret://ssh/initial",
        automation_credential_ref="secret://ssh/auto")
    with pytest.raises(PermissionError,match="execution grant"):
        asyncio.run(ensure_authorized_key(node,public_key=key,transport=transport,resolver=Resolver(),verify_new_connection=lambda _:None))
    assert len(transport.requests)==1


def test_key_write_is_blocked_without_initial_credential_or_on_bad_verification():
    from sandbox_graph.environment.contracts import NodeDeclaration
    base = dict(node_id="n1", machine_id="m1", roles=("compute",), ssh_address="host.example",
        ssh_user="bench", known_hosts_ref="artifact://known", automation_credential_ref="secret://ssh/auto")
    transport = FakeTransport(SSHResult(0, b"{}", b""))
    with pytest.raises(ValueError, match="initial credential"):
        asyncio.run(ensure_authorized_key(NodeDeclaration(**base), public_key=ED25519, transport=transport,
            resolver=Resolver(), verify_new_connection=lambda _: None))
