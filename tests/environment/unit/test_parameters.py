import pytest
from sandbox_graph.environment.parameters import ParameterRule, resolve_parameter


def test_preserve_keeps_discovered_value_and_exact_converts_units():
    rule=ParameterRule(path='sandbox.memory',dimension='instance_spec',unit='MiB',minimum=128,maximum=65536)
    preserved=resolve_parameter(rule,mode='preserve',observed={'value':4096,'unit':'MiB'})
    exact=resolve_parameter(rule,mode='exact',value={'value':4,'unit':'GiB'})
    assert preserved.value==4096 and preserved.unit=='MiB'
    assert exact.value==4096 and exact.unit=='MiB'


def test_relax_for_plan_requires_finite_budget_and_never_uses_unbounded_integer():
    rule=ParameterRule(path='limits.memory',dimension='platform_limit',unit='MiB',minimum=512,maximum=131072)
    result=resolve_parameter(rule,mode='relax_for_plan',observed={'value':4096,'unit':'MiB'},
        budget={'minimum':4096,'maximum':32768,'step':4096,'budget_ref':'artifact://budget/1'})
    assert result.value==32768
    with pytest.raises(ValueError,match='finite'):
        resolve_parameter(rule,mode='relax_for_plan',observed={'value':4096,'unit':'MiB'},
            budget={'minimum':0,'maximum':2**63-1,'step':1,'budget_ref':'artifact://budget/2'})


def test_dimension_separation_and_range_validation():
    rule=ParameterRule(path='sandbox.cpu',dimension='instance_spec',unit='mCPU',minimum=100,maximum=64000)
    with pytest.raises(ValueError,match='unit'):
        resolve_parameter(rule,mode='exact',value={'value':2,'unit':'GiB'})
    with pytest.raises(ValueError,match='range'):
        resolve_parameter(rule,mode='exact',value={'value':999999,'unit':'mCPU'})
    assert rule.dimension=='instance_spec'


def test_zero_is_not_implicit_disable_and_requires_explicit_rule():
    rule=ParameterRule(path='admission.rate',dimension='performance_policy',unit='requests/s',minimum=0,maximum=1000,zero_semantics='literal')
    assert resolve_parameter(rule,mode='exact',value={'value':0,'unit':'requests/s'}).value==0
    disallow=ParameterRule(path='builder.parallelism',dimension='platform_limit',unit='count',minimum=1,maximum=256)
    with pytest.raises(ValueError,match='range'):
        resolve_parameter(disallow,mode='exact',value={'value':0,'unit':'count'})
