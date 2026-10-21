"""D18.6 / ORC-054,055,056f: change impact classes distinct and identity change forces re-measure."""
from sandbox_graph.planning.changes import diff


def test_identical_change_does_nothing():
    plan = diff({'a': 1}, {'a': 1})
    assert plan.requires_measurement is False and plan.rerun_nodes == ()


def test_wording_layout_threshold_and_completion_event_are_distinct_classes():
    wording = diff({'wording': 'a'}, {'wording': 'b'})
    assert wording.requires_measurement is False and wording.rerun_nodes == ('F1', 'F2', 'F4')
    layout = diff({'layout': 'a'}, {'layout': 'b'})
    assert layout.requires_measurement is False and layout.rerun_nodes[0] == 'A7'
    threshold = diff({'threshold': 1}, {'threshold': 2})
    assert threshold.requires_measurement is False and 'A3' in threshold.rerun_nodes
    present = diff({'completion_event': 'old'}, {'completion_event': 'new'},
                   available_events=frozenset({'new'}))
    assert present.requires_measurement is False and 'A3' in present.rerun_nodes
    absent = diff({'completion_event': 'old'}, {'completion_event': 'new'})
    assert absent.requires_measurement is True


def test_spec_change_forces_full_re_measurement():
    plan = diff({'specification': '2v2G'}, {'specification': '2v4G'})
    assert plan.requires_measurement is True
    assert plan.rerun_nodes == tuple(f'N{i:02}' for i in range(11))


def test_identity_change_forces_new_measurement_and_is_not_reusable():
    for field in ('artifact_kind', 'launch_path', 'producer_step', 'cost_scope',
                  'topology', 'endpoint_kind', 'readiness_signal', 'endpoint_ref'):
        plan = diff({field: 'old'}, {field: 'new'})
        assert plan.requires_measurement is True, field
        assert 'identity' in plan.reason, field
