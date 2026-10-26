import pytest
from sandbox_graph.environment.products.literal_env import render_literal_env,parse_literal_env


def test_generated_environment_roundtrips_literal_values_without_expansion():
    values={'API_KEY':'abc','URL':'https://example.invalid/$HOME?x=$(touch /bad)'}
    data=render_literal_env(values)
    assert parse_literal_env(data)==values


@pytest.mark.parametrize('raw',[b'API_KEY=$(cat /secret)\n',b'API_KEY="${SECRET}"\n',
    b"API_KEY='x'; touch /bad\n",b"BASH_ENV='/evil'\n",b"CUBE_SANDBOX_INSTALL_ROOT='/other'\n",
    b"ONE_CLICK_DEPLOY_ROLE='control'\n",b"KEY='a'\nKEY='b'\n"])
def test_script_sourced_configuration_only_accepts_literal_non_override_assignments(raw):
    with pytest.raises(ValueError):parse_literal_env(raw)
