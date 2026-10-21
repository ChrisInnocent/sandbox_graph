import json

import pytest

from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.environment.declarations import (
    load_declaration,
    resolve_declaration,
    verify_artifact_reference,
)
from sandbox_graph.environment.contracts import EnvironmentDeclaration


def sample():
    return {
        "schema_version": 2,
        "environment_id": "test-env",
        "product": "cubesandbox",
        "product_version": "1.2.3",
        "profile": "cube.oneclick.v1",
        "nodes": [{
            "node_id": "node-a", "machine_id": "host-a", "roles": ["compute"],
            "ssh_address": "192.0.2.4", "ssh_user": "bench",
            "known_hosts_ref": "artifact://known-hosts",
            "automation_credential_ref": "secret://ssh/key",
        }],
    }


def test_load_declaration_parses_json_and_resolves_only_missing_discoverable_facts(tmp_path):
    path = tmp_path / "env.json"
    path.write_text(json.dumps(sample()))
    declaration = load_declaration(path)
    resolved = resolve_declaration(declaration, {"node-a": {"ssh_port": 2202}})
    assert resolved.declaration.nodes[0].ssh_port == 2202
    assert resolved.discovered_fields == ("node-a.ssh_port",)


def test_explicit_declaration_values_take_precedence_over_discovery(tmp_path):
    raw = sample()
    raw["nodes"][0]["ssh_port"] = 2022
    path = tmp_path / "env.json"
    path.write_text(json.dumps(raw))
    resolved = resolve_declaration(load_declaration(path), {"node-a": {"ssh_port": 2202}})
    assert resolved.declaration.nodes[0].ssh_port == 2022
    assert resolved.discovered_fields == ()


def test_yaml_duplicate_keys_are_rejected(tmp_path):
    path = tmp_path / "env.yaml"
    path.write_text("environment_id: a\nenvironment_id: b\n")
    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_declaration(path)


def test_artifact_reference_must_match_content_store_scope_and_digest(tmp_path):
    store = FileArtifactStore(tmp_path / "artifacts")
    ref = store.commit([b"known hosts"], scope="test-env", media_type="text/plain")
    assert verify_artifact_reference(ref, store, expected_scope="test-env") == ref
    with pytest.raises((ValueError, PermissionError)):
        verify_artifact_reference(ref, store, expected_scope="other-env")
