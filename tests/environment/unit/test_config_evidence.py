import json

import pytest

from sandbox_graph.environment.config import (
    evaluate_config_value,
    load_config,
    resolve_config,
)


def test_config_loader_parses_toml_yaml_and_json_without_executing_tags(tmp_path):
    toml = tmp_path / "config.toml"; toml.write_text("[machine]\nvcpu_count=2\n")
    yaml = tmp_path / "config.yaml"; yaml.write_text("machine:\n  vcpu_count: 2\n")
    js = tmp_path / "config.json"; js.write_text('{"machine":{"vcpu_count":2}}')
    assert load_config(toml)["machine"]["vcpu_count"] == 2
    assert load_config(yaml)["machine"]["vcpu_count"] == 2
    assert load_config(js)["machine"]["vcpu_count"] == 2
    bad = tmp_path / "bad.yaml"; bad.write_text("value: !!python/object/apply:os.system ['id']")
    with pytest.raises(ValueError): load_config(bad)


def test_resolve_config_retains_source_and_applies_declared_precedence():
    file_values = {"auth":{"api_key":"file"}, "limits":{"rate":100}}
    resolved = resolve_config(file_values, env={"AUTH__API_KEY":"environment"},
        cli={"auth.api_key":"cli"})
    assert resolved.values["auth"]["api_key"] == "cli"
    assert resolved.sources["auth.api_key"] == "cli"
    assert resolved.values["limits"]["rate"] == 100
    assert resolved.sources["limits.rate"] == "file"


def test_effective_value_cannot_pass_on_file_or_resolved_value_alone():
    result = evaluate_config_value("quota.cpu", expected=8, file_value=8,
        resolved_value=8, effective_value=None)
    assert result.status == "unknown"
    assert result.evidence_level == "resolved_only"
    mismatch = evaluate_config_value("quota.cpu", expected=8, file_value=8,
        resolved_value=8, effective_value=4)
    assert mismatch.status == "mismatch"
    assert mismatch.source == "runtime"
    passed = evaluate_config_value("quota.cpu", expected=8, file_value=8,
        resolved_value=8, effective_value=8)
    assert passed.status == "satisfied"


def test_conflicting_environment_mapping_is_rejected():
    with pytest.raises(ValueError, match="duplicate override"):
        resolve_config({}, env={"A__B":"1", "a__b":"2"})
