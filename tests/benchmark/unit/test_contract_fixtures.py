"""BCT-02 fixture provenance/hash binding, and the injectable scripted service.

These fixtures exist so tester-contract tests never call a production algorithm to
produce their own expected values (BCT-02), and can inject late/partial/lost outcomes
without a real product.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from sandbox_bench.contract_fixtures import validate_fixture_metadata

FIXTURES = Path(__file__).parents[2] / 'fixtures' / 'benchmark'


def _load(name):
    module_name = name.replace('.py', '')
    spec = importlib.util.spec_from_file_location(module_name, FIXTURES / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclass annotation resolution needs this
    spec.loader.exec_module(module)
    return module


def test_metadata_binds_hash_provenance_and_classification():
    record = validate_fixture_metadata(FIXTURES / 'load_metadata.json')
    assert record['classification'] == 'synthetic'
    assert record['sha256_verified'] is True
    assert record['input_ref'].endswith('load_golden.json')
    assert record['oracle_independence']
    assert record['requirements'] == ['BTC-016', 'BTC-017']


def test_tampered_golden_is_rejected(tmp_path):
    golden = json.loads((FIXTURES / 'load_golden.json').read_text())
    golden['window_complete'] = [9, 9, 9, 9, 9]  # hand-edit, hash no longer matches
    tampered = tmp_path / 'load_golden.json'
    tampered.write_text(json.dumps(golden))
    metadata = json.loads((FIXTURES / 'load_metadata.json').read_text())
    metadata['input_ref'] = str(tampered)
    path = tmp_path / 'load_metadata.json'
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match='sha256'):
        validate_fixture_metadata(path)


def test_unsupported_classification_is_rejected(tmp_path):
    metadata = json.loads((FIXTURES / 'load_metadata.json').read_text())
    metadata['classification'] = 'guess'
    path = tmp_path / 'load_metadata.json'
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match='classification'):
        validate_fixture_metadata(path)


def test_scripted_service_outcomes_flow_through_the_load_engine():
    from sandbox_bench.load import LoadEngine, LoadSchedule
    module = _load('scripted_service.py')
    clock_mod = _load('load_clock.py')
    clock = clock_mod.ManualClock()
    service = module.ScriptedSandboxService({
        0: module.ScriptedOutcome('success', delay_s=1),
        1: module.ScriptedOutcome('failed', delay_s=1),
        2: module.ScriptedOutcome('success', delay_s=1),
        3: module.ScriptedOutcome('success', delay_s=1),
    }, clock=clock, sleep=clock.sleep)

    async def scenario():
        return await clock.run(LoadEngine(clock=clock, sleep=clock.sleep).run(
            LoadSchedule('WINDOW', 4, 1, 10), service.operation, journal=clock_mod.ScriptedJournal()))

    report = asyncio.new_event_loop().run_until_complete(scenario())
    assert report.successes == 3
    assert report.results[1].status == 'failed'   # failure consumes its budget
    assert service.calls == [0, 1, 2, 3]


def test_scripted_service_reports_a_lost_reply_as_unknown_within_the_bound():
    from sandbox_bench.load import LoadEngine, LoadSchedule
    module = _load('scripted_service.py')
    clock_mod = _load('load_clock.py')
    clock = clock_mod.ManualClock()
    service = module.ScriptedSandboxService({
        0: module.ScriptedOutcome('success', delay_s=1),
        1: module.ScriptedOutcome('unknown', unresolved_external=1),
    }, clock=clock, sleep=clock.sleep)

    async def scenario():
        return await clock.run(LoadEngine(clock=clock, sleep=clock.sleep).run(
            LoadSchedule('WINDOW', 2, 2, 10, unknown_limit=1), service.operation,
            journal=clock_mod.ScriptedJournal()))

    report = asyncio.new_event_loop().run_until_complete(scenario())
    assert report.results[1].status == 'unknown'
    assert report.unresolved_external == 1
    assert report.stop_reason == 'unknown_limit'
