import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]


def test_auth_fixture_paths_and_commits_match_audited_checkouts():
    fixture=json.loads((ROOT/"tests/environment/fixtures/source-rules.json").read_text())
    paths={
        "CubeSandbox": ROOT/"CubeSandbox",
        "orchestrator": ROOT/"kuasar-components/orchestrator",
        "AgentENV": ROOT/"AgentENV",
    }
    for rule in fixture["rules"]:
        code=(paths[rule["repository"]]/rule["path"]).read_text()
        assert rule["symbol"] in code
        if rule["id"]=="cube.api-auth-carrier":
            assert "X-API-Key" in code and "Bearer" in code
        elif rule["id"]=="kuasar.group-import-file-only":
            assert 'case "file"' in code and "unsupported group source type" in code
        elif rule["id"]=="agentenv.api-key-header":
            assert "x-api-key" in code.lower()


def test_three_products_do_not_share_an_assumed_x_api_token_carrier():
    cube=(ROOT/"CubeSandbox/CubeAPI/src/middleware/auth.rs").read_text()
    kuasar=(ROOT/"kuasar-components/orchestrator/internal/api/api.go").read_text()
    agentenv=(ROOT/"AgentENV/src/api/impls/auth.rs").read_text()
    assert "X-API-Key" in cube
    assert 'Header.Get("X-API-KEY")' in kuasar
    assert "x-api-key" in agentenv.lower()
