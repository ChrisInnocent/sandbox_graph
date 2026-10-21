import pytest
from sandbox_graph.environment.contracts import (
    CheckObservation, EnvironmentDeclaration, EnvironmentReport, NodeDeclaration,
)
from sandbox_graph.environment.planner import ActionCatalog, ActionDefinition, RepairPlanner


def declaration():
    return EnvironmentDeclaration(environment_id="env",product="cubesandbox",product_version="1",
        profile="cube.oneclick.v1",nodes=(NodeDeclaration(node_id="n1",machine_id="h1",roles=("compute",),
            ssh_address="host.example",ssh_user="bench",known_hosts_ref="artifact://known",
            automation_credential_ref="secret://ssh/key"),))


def report(status="missing", code="CUBELET_SERVICE_STOPPED"):
    obs=CheckObservation(check_id="service",rule_ref="C-I:service",node_id="n1",component="cubelet",
        status=status,expected="active",observed="inactive",observed_at="2026-10-21T00:00:00Z",repair_code=code)
    return EnvironmentReport(declaration_hash=declaration().declaration_hash,source_set=("CubeSandbox=abc",),
        profile="cube.oneclick.v1",observations=(obs,),readiness={"service":"blocked"})


def catalog():
    return ActionCatalog((ActionDefinition(product="cubesandbox",profile="cube.oneclick.v1",
        repair_codes=("CUBELET_SERVICE_STOPPED",),action_ref="cube.service.start.v1",roles=("compute",),
        timeout_seconds=30,reconcile_rule="check-systemd-mainpid",retry_class="reconcile"),))


def test_repair_planner_maps_confirmed_drift_to_registered_action_deterministically():
    planner=RepairPlanner(catalog())
    first=planner.plan(declaration(),report(),report_ref="artifact://env/report",deadline="2026-10-21T01:00:00Z",
        authorized_actions={"cube.service.start.v1"})
    second=planner.plan(declaration(),report(),report_ref="artifact://env/report",deadline="2026-10-21T01:00:00Z",
        authorized_actions={"cube.service.start.v1"})
    assert first.status=="ready"
    assert first.plan.steps[0].action_ref=="cube.service.start.v1"
    assert first.plan.plan_hash==second.plan.plan_hash


def test_unknown_state_never_becomes_install_or_start_action():
    result=RepairPlanner(catalog()).plan(declaration(),report(status="unknown"),
        report_ref="artifact://env/report",deadline="2026-10-21T01:00:00Z",
        authorized_actions={"cube.service.start.v1"})
    assert result.status=="blocked"
    assert result.plan.steps==()
    assert result.blockers[0].code=="OBSERVATION_UNKNOWN"


def test_unregistered_or_unauthorized_action_blocks_without_fallback():
    result=RepairPlanner(catalog()).plan(declaration(),report(),report_ref="artifact://env/report",
        deadline="2026-10-21T01:00:00Z",authorized_actions=set())
    assert result.status=="blocked"
    assert result.plan.steps==()
    assert result.blockers[0].code=="ACTION_NOT_AUTHORIZED"


def test_all_satisfied_report_produces_empty_explainable_plan():
    result=RepairPlanner(catalog()).plan(declaration(),report(status="satisfied"),
        report_ref="artifact://env/report",deadline="2026-10-21T01:00:00Z",authorized_actions=set())
    assert result.status=="already_satisfied"
    assert result.plan.steps==()


def test_action_dependency_dag_is_topologically_ordered_and_cycle_rejected():
    from sandbox_graph.environment.planner import ActionCatalog, ActionDefinition
    base=report()
    first=base.observations[0].model_copy(update={'check_id':'a-child','repair_code':'CHILD'})
    second=base.observations[0].model_copy(update={'check_id':'z-parent','repair_code':'PARENT'})
    observed=base.model_copy(update={'observations':(first,second)})
    actions=ActionCatalog((
        ActionDefinition(product='cubesandbox',profile='cube.oneclick.v1',repair_codes=('CHILD',),
            action_ref='cube.child.action.v1',roles=('compute',),timeout_seconds=30,reconcile_rule='child',dependencies=('PARENT',)),
        ActionDefinition(product='cubesandbox',profile='cube.oneclick.v1',repair_codes=('PARENT',),
            action_ref='cube.parent.action.v1',roles=('compute',),timeout_seconds=30,reconcile_rule='parent'),
    ))
    result=RepairPlanner(actions).plan(declaration(),observed,report_ref='artifact://env/report',
        deadline='2026-10-21T01:00:00Z',authorized_actions={'cube.child.action.v1','cube.parent.action.v1'})
    assert [step.action_ref for step in result.plan.steps]==['cube.parent.action.v1','cube.child.action.v1']
    assert result.plan.steps[1].depends_on==(result.plan.steps[0].step_id,)
    with pytest.raises(ValueError,match='cycle'):
        ActionCatalog((
            ActionDefinition(product='p',profile='x',repair_codes=('A',),action_ref='a.action.v1',roles=('compute',),timeout_seconds=1,reconcile_rule='r',dependencies=('B',)),
            ActionDefinition(product='p',profile='x',repair_codes=('B',),action_ref='b.action.v1',roles=('compute',),timeout_seconds=1,reconcile_rule='r',dependencies=('A',)),
        ))
