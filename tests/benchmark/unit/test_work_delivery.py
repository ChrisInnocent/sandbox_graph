"""P8.4 / ORC-024,028,041,046: N08-N10 self-check, cleanup and sealed delivery."""
from sandbox_bench.session import BenchSession
from tests.benchmark.scenarios.test_scenarios import case
from tests.benchmark.unit.test_session import Scenario

CONFIG = {'product': 'agentenv', 'api_url': 'http://localhost:1', 'data_url': 'http://localhost:1'}


async def test_full_measure_deliver_sequence_seals_evidence(tmp_path):
    session = BenchSession(case('B01.TEMPLATE'), CONFIG, tmp_path, scenario=Scenario())
    assert (await session._N05())['status'] == 'completed'
    analysis = await session._N08()
    assert analysis['status'] == 'completed' and analysis['analysis']['throughput']['count'] == 4
    assert (await session._N09())['status'] == 'completed'
    assert (await session._N10())['status'] == 'completed'
    assert (tmp_path / 'evidence' / 'case_manifest.json').exists()
    assert (tmp_path / 'analysis.json').exists()


async def test_every_failure_exit_still_has_a_cleanup_and_delivery_step(tmp_path):
    session = BenchSession(case('B01.TEMPLATE'), CONFIG, tmp_path, scenario=Scenario())
    # A node that never ran measurement must not fabricate a delivery, but cleanup/delivery
    # are still reachable and produce explicit gaps rather than silence.
    assert (await session._N09())['status'] == 'completed'
    delivered = await session._N10()
    assert delivered['status'] == 'completed'
