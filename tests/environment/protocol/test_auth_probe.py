import asyncio
from dataclasses import dataclass

from sandbox_graph.environment.probes.auth import AuthProbe


@dataclass
class Response:
    status_code: int
    body: dict


class Endpoint:
    def __init__(self, expected="right", mode="protected"):
        self.expected=expected; self.mode=mode; self.calls=[]
    async def call(self, credential):
        self.calls.append(credential)
        if self.mode=="public": return Response(200,{"ok":True})
        if credential is None: return Response(401,{})
        if credential != self.expected: return Response(403,{})
        return Response(200,{"owner":"group-a"})


def test_protected_endpoint_requires_good_key_and_rejects_missing_and_wrong():
    endpoint=Endpoint()
    result=asyncio.run(AuthProbe(endpoint.call).verify(correct_credential="right",
        rejected_statuses={401,403},accepted_statuses={200},
        ownership_check=lambda response: response.body.get("owner")=="group-a"))
    assert result.status=="verified"
    assert endpoint.calls[0] is None and endpoint.calls[1] != "right" and endpoint.calls[2] == "right"
    assert "right" not in repr(result)


def test_public_health_like_endpoint_does_not_prove_authentication():
    endpoint=Endpoint(mode="public")
    result=asyncio.run(AuthProbe(endpoint.call).verify(correct_credential="right",
        rejected_statuses={401,403},accepted_statuses={200},health_endpoint=True))
    assert result.status=="health_only"
    assert endpoint.calls==[]


def test_public_protected_route_is_rejected_as_unprotected():
    endpoint=Endpoint(mode="public")
    result=asyncio.run(AuthProbe(endpoint.call).verify(correct_credential="right",
        rejected_statuses={401,403},accepted_statuses={200}))
    assert result.status=="unprotected"


def test_kv_valid_format_without_owner_evidence_is_not_verified():
    endpoint=Endpoint()
    result=asyncio.run(AuthProbe(endpoint.call).verify(correct_credential="right",
        rejected_statuses={401,403},accepted_statuses={200},ownership_check=lambda _:False))
    assert result.status=="ownership_unverified"


def test_wrong_credential_cannot_equal_the_configured_credential():
    endpoint=Endpoint()
    result=asyncio.run(AuthProbe(endpoint.call).verify(correct_credential="same",
        wrong_credential="same",rejected_statuses={401,403},accepted_statuses={200}))
    assert result.status=="invalid_probe_configuration"
    assert endpoint.calls==[]
