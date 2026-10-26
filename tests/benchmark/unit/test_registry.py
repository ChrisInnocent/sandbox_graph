"""The method catalogue is a runtime map, not an environment_verified claim."""
import pytest

from sandbox_bench.registry import METHODS, capability


def test_catalogue_covers_all_b_and_e_methods():
    b_series = {m for m in METHODS if m.startswith('B')}
    e_series = {m for m in METHODS if m.startswith('E')}
    assert {m for m in b_series if m.startswith('B01.')} == {
        'B01.TEMPLATE', 'B01.SNAPSHOT.DISK', 'B01.SNAPSHOT.MEMORY',
        'B01.TEMPLATE.HOLD','B01.SNAPSHOT.DISK.HOLD','B01.SNAPSHOT.MEMORY.HOLD'}
    assert {'B10.FIXED_REQUESTS.WINDOW', 'B10.FIXED_REQUESTS.BATCH'} <= b_series
    assert e_series == {'E01.WORKLOAD', 'E02.RESET', 'E03.MIXED_SOAK',
                        'E04.PRESSURE', 'E05.INTERFERENCE', 'E06.RECOVERY'}


def test_b_series_matches_the_graph_catalogue():
    from sandbox_graph.catalog.registry import METHODS as GRAPH_METHODS
    graph_b = {m for m in GRAPH_METHODS if m.startswith('B')}
    assert set(METHODS) & graph_b == graph_b


def test_capability_separates_support_from_verification():
    native = capability('kuasar', 'B05.NATIVE')
    assert native['status'] == 'unsupported' and native['environment_verified'] is False
    extension = capability('kuasar', 'E04.PRESSURE')
    assert extension['status'] == 'unverified' and extension['evidence_ref'] is None
    audited = capability('cubesandbox', 'B01.TEMPLATE')
    assert audited['status'] == 'source_audited' and audited['implementation_status'] == 'unverified'
    assert audited['environment_verified'] is False


def test_unknown_method_or_product_is_explicit():
    with pytest.raises(ValueError):
        capability('kuasar', 'B99.NOPE')
    assert capability('mystery', 'B01.TEMPLATE')['status'] == 'unverified'
