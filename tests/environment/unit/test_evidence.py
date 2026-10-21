import json

import pytest

from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.environment.evidence import SecretRedactor, assert_public_payload_safe, commit_sanitized_json


def test_secret_redactor_scrubs_output_strings_dicts_headers_and_errors():
    redactor = SecretRedactor(["SENTINEL-secret-998877", "api-value"])
    data = {"stdout":"got SENTINEL-secret-998877", "headers":{"X-API-Key":"api-value"},
        "error":"request failed with SENTINEL-secret-998877"}
    sanitized = redactor.redact(data)
    encoded = json.dumps(sanitized)
    assert "SENTINEL-secret-998877" not in encoded
    assert "api-value" not in encoded
    assert sanitized["headers"]["X-API-Key"] == "[REDACTED]"


def test_public_payload_scanner_rejects_secret_material_and_sensitive_fields():
    assert_public_payload_safe({"auth_ref":"secret://lab/api/v1", "status":"verified"})
    with pytest.raises(ValueError, match="sensitive field"):
        assert_public_payload_safe({"Authorization":"Bearer abc"})
    with pytest.raises(ValueError, match="secret material"):
        assert_public_payload_safe({"message":"response included SENTINEL-secret-998877"},
            secret_values=("SENTINEL-secret-998877",))


def test_redactor_does_not_modify_non_sensitive_evidence():
    value={"node":"host-a", "status":"satisfied", "memory_bytes":4096}
    assert SecretRedactor(["dontleak"]).redact(value)==value


def test_json_evidence_is_redacted_before_artifact_commit(tmp_path):
    store=FileArtifactStore(tmp_path/"artifacts")
    ref=commit_sanitized_json(store,{"message":"token sentinel-secret", "X-API-Key":"key-secret"},
        scope="group-a",producer="test",redactor=SecretRedactor(["sentinel-secret","key-secret"]))
    body=store.path_for(ref).read_text()
    assert "sentinel-secret" not in body and "key-secret" not in body
    assert "[REDACTED]" in body
