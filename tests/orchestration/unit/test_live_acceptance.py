import pytest
from sandbox_graph.testing.live_acceptance import validate_contract,verify_delivery


def test_live_contract_requires_finite_limits_and_explicit_environment():
    with pytest.raises(ValueError):validate_contract({})
    value={'schema_version':1,'runtime':'/tmp/runtime.json','selection':{},'request_id':'acceptance-1',
        'seconds':120,'max_work_items':4,'expected_groups':['a','b'],'output':'/tmp/acceptance.json'}
    assert validate_contract(value)==value
    with pytest.raises(ValueError):validate_contract(value|{'seconds':float('inf')})
    with pytest.raises(ValueError):validate_contract(value|{'expected_groups':['a','a']})


def test_harness_rejects_archive_without_actual_group_coverage():
    status={'run':{'run_id':'r'},'archives':[{'status':'complete'}],'analyses':[], 'deliveries':[],'operations':[]}
    with pytest.raises(ValueError,match='coverage'):
        verify_delivery(None,status,{'a','b'})
