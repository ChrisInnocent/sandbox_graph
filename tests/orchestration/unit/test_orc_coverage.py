"""P14.3 / T11: the ORC coverage ledger enumerates ORC-001-073 with an explicit status."""
import re
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[3] / 'docs' / 'orchestration' / 'orc-coverage.md'


def test_ledger_enumerates_every_orc_with_an_explicit_status():
    text = LEDGER.read_text()
    rows = dict(re.findall(r'\|\s*(ORC-\d{3})\s*\|[^|]*\|[^|]*\|\s*(\w+)\s*\|', text))
    expected = {f'ORC-{n:03d}' for n in range(1, 74)}
    assert expected <= set(rows), sorted(expected - set(rows))
    assert set(rows.values()) <= {'tagged', 'functional', 'blocked'}
    assert 'blocked' in rows.values()   # the ledger must not claim everything is done
