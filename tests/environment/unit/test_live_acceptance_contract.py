import pytest
from sandbox_graph.testing.environment_acceptance import validate_contract


def test_environment_acceptance_requires_finite_explicit_contract():
    valid={'schema_version':1,'environment':'/private/env.json','runtime':'/private/runtime.json',
        'authority':'/private/authority.json','artifacts':'/private/artifacts','output':'/private/result',
        'request_id':'acceptance/1','seconds':60,'max_steps':16}
    assert validate_contract(valid,product='kuasar',topology='single')==valid
    for change in ({'seconds':float('inf')},{'seconds':True},{'max_steps':0},{'runtime':'relative'}):
        with pytest.raises(ValueError):validate_contract(valid|change,product='kuasar',topology='single')
