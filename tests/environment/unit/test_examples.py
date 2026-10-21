from pathlib import Path

import pytest

from sandbox_graph.environment.catalog import ProfileCatalog, audited_sources
from sandbox_graph.environment.contracts import EnvironmentDeclaration
from sandbox_graph.environment.declarations import load_declaration


ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "docs" / "examples" / "environment"


@pytest.mark.parametrize(
    ("filename", "architecture"),
    [
        ("cube-multinode-check.yaml", {"cube-compute-01": "x86_64"}),
        ("kuasar-single-node-check.yaml", {"kuasar-host-01": "x86_64"}),
        ("agentenv-multinode-check.yaml", {
            "agentenv-control-01": "x86_64", "agentenv-runtime-01": "x86_64"
        }),
    ],
)
def test_documented_check_examples_match_audited_profiles(filename, architecture):
    declaration = load_declaration(EXAMPLES / filename)
    resolved = ProfileCatalog.standard().verify_declaration(
        EnvironmentDeclaration.model_validate(declaration),
        architecture,
        mutation=False,
    )
    assert resolved.product == declaration["product"]
    assert declaration["policy"]["operation"] == "check_only"


def test_source_rule_fixture_references_audited_source_files():
    import json

    fixture = json.loads((ROOT / "tests/environment/fixtures/source-rules.json").read_text())
    sources = audited_sources()
    upstream = {
        "CubeSandbox": ROOT / "CubeSandbox",
        "orchestrator": ROOT / "kuasar-components/orchestrator",
        "AgentENV": ROOT / "AgentENV",
    }
    for rule in fixture["rules"]:
        assert sources[rule["repository"]] == rule["commit"]
        assert (upstream[rule["repository"]] / rule["path"]).is_file()
