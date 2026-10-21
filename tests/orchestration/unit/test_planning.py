"""Explicit finite selection oracle; never derive expected set from compiler."""
import pytest
from sandbox_graph.planning.compiler import Selection, Case, PlanCompiler
from sandbox_graph.catalog.registry import Registry, standard_registry


def catalog():
    return [Case(capability='supported',adapter_verified=True,case_id='a', group_id='G_A', sandbox_type='x', scenario_id='B04', method_id='B04.MEMORY', priority='base', conditions=({'size': 1},), dependencies=('fixture.pause',)),
            Case(capability='supported',adapter_verified=True,case_id='b', group_id='G_B', sandbox_type='x', scenario_id='B01', method_id='B01.TEMPLATE', priority='base', conditions=({'size': 2},)),
            Case(adapter_verified=False,case_id='c', group_id='G_C', sandbox_type='y', scenario_id='E04', method_id='E04.PRESSURE', priority='extension', conditions=({'size': 3},), capability='unsupported')]


def test_intersection_exclusions_and_dependency_do_not_expand_measurements():
    p = PlanCompiler(catalog()).compile(Selection(groups=('G_A', 'G_B'), scenarios=('B04',)), authorized_groups={'G_A','G_B'})
    assert [x.case_id for x in p.cases] == ['a']
    assert p.dependencies == ('fixture.pause',)
    assert p.status == 'ready'
    assert len(p.hash) == 64


def test_empty_unknown_unauthorized_and_dependency_exclusion_are_distinct():
    c = PlanCompiler(catalog())
    assert c.compile(Selection(groups=()), authorized_groups={'G_A'}).status == 'empty'
    with pytest.raises(ValueError, match='unknown'):
        c.compile(Selection(groups=('nonexistent',)), authorized_groups={'G_A'})
    with pytest.raises(PermissionError):
        c.compile(Selection(groups=('G_C',)), authorized_groups={'G_A'})
    p = c.compile(Selection(groups=('G_A',), exclude=('fixture.pause',)), authorized_groups={'G_A'})
    assert p.status == 'blocked' and p.cases[0].case_id == 'a'
    assert p.gaps[0].case_id == 'a'


def test_missing_conditions_block_and_unsupported_preserves_coverage():
    c = PlanCompiler(catalog())
    p = c.compile(Selection(groups=('G_C',)), authorized_groups={'G_C'})
    assert p.cases[0].capability == 'unsupported'
    assert p.gaps[0].code == 'CAPABILITY_UNSUPPORTED'
    assert len(p.cases) == 1
    assert len(standard_registry().nodes) == 45


def test_stage_only_does_not_dispatch_measurement_cases():
    p = PlanCompiler(catalog()).compile(Selection(stage='report'), authorized_groups=set())
    assert p.cases == () and p.stage == 'report'


def test_missing_adapter_only_blocks_its_case_in_mixed_plan():
    cases=catalog()[:2]
    cases[0]=cases[0].model_copy(update={'adapter_verified':False})
    plan=PlanCompiler(cases).compile(Selection(),authorized_groups={'G_A','G_B'})
    assert plan.status=='ready'
    assert {gap.case_id for gap in plan.gaps}=={'a'}


def test_unspecified_deployment_capability_is_unverified_and_cannot_start():
    case=Case(case_id='real-undeclared',group_id='G_A',sandbox_type='candidate',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'requests':1},))
    assert case.capability=='unverified' and case.adapter_verified is False
    plan=PlanCompiler([case]).compile(Selection(),authorized_groups={'G_A'})
    assert plan.status=='blocked'
    assert any(gap.code=='ADAPTER_UNVERIFIED' for gap in plan.gaps)
