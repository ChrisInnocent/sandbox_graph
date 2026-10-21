import pytest
from pydantic import ValidationError

from sandbox_graph.environment.contracts import (
    ArtifactLock,
    AuthBinding,
    EnvironmentDeclaration,
    NodeDeclaration,
    TargetPolicy,
)


def node(node_id="n1", roles=("compute",), machine_id="machine-1", depends_on=()):
    return NodeDeclaration(
        node_id=node_id,
        machine_id=machine_id,
        roles=roles,
        ssh_address="192.0.2.10",
        ssh_user="sandbox",
        known_hosts_ref="artifact://known-hosts",
        automation_credential_ref="secret://ssh/automation",
        depends_on=depends_on,
    )


def test_environment_declaration_allows_one_node_to_hold_multiple_roles():
    declaration = EnvironmentDeclaration(
        environment_id="bench-a",
        product="cubesandbox",
        product_version="1.2.3",
        profile="cube.oneclick.v1",
        nodes=(node(roles=("control", "compute")),),
        targets=TargetPolicy(),
        artifacts=(ArtifactLock(name="cube", version="1.2.3", digest="a" * 64),),
            service_endpoints={"control_api": "https://cube-control.example.invalid"},
    )
    assert declaration.nodes[0].roles == ("control", "compute")
    assert declaration.service_endpoints["control_api"].startswith("https://")
    assert declaration.schema_version == 2


def test_environment_declaration_rejects_duplicate_machine_identity():
    with pytest.raises(ValidationError, match="machine identity"):
        EnvironmentDeclaration(
            environment_id="bench-a",
            product="agentenv",
            product_version="1.0",
            profile="agentenv.native.v1",
            nodes=(node("n1"), node("n2", machine_id="machine-1")),
        )


def test_environment_declaration_rejects_dependency_cycles():
    with pytest.raises(ValidationError, match="dependency cycle"):
        EnvironmentDeclaration(
            environment_id="bench-a",
            product="kuasar",
            product_version="1.0",
            profile="kuasar.native.v1",
            nodes=(
                node("n1", depends_on=("n2",)),
                node("n2", machine_id="machine-2", depends_on=("n1",)),
            ),
        )


def test_auth_binding_accepts_secret_references_but_not_secret_values():
    AuthBinding(plane="control_api", mode="api_key", credential_ref="secret://cube/key")
    with pytest.raises(ValidationError, match="reference"):
        AuthBinding(plane="control_api", mode="api_key", credential_ref="plaintext-api-key")
