"""BTC-008: source-shaped Kuasar slog fixtures, not adapter round trips."""
import json
import unittest
from sandbox_bench.logs import AcceptedAtEvidence, LogIdentity, TerminalLogParser

IDENTITY = LogIdentity('sid-1', 'run-1', 'create', 'memory')
RAW = dict(time='2026-01-01T00:00:02Z', level='INFO', msg='sandbox launch terminal',
           sid='sid-1', run_id='run-1', kind='create', profile='memory', result='success',
           starting_total_duration=1500000000)
START = AcceptedAtEvidence(IDENTITY, '2026-01-01T00:00:00.5Z', 'raw:accepted-at')


class LogsTests(unittest.TestCase):
    def test_btc008_json_duration_and_arrival_are_separate(self):
        parser = TerminalLogParser()
        record = parser.parse(json.dumps(RAW), expected=IDENTITY, accepted_at=START, observed_at=50)
        self.assertEqual(record.status, 'valid')
        self.assertEqual(record.internal_duration_ns, 1500000000)
        self.assertEqual(record.source_event_time, '2026-01-01T00:00:02Z')
        self.assertEqual(record.observed_at, 50)
        self.assertEqual(record.raw_record, json.dumps(RAW))
        self.assertEqual(len(record.raw_sha256), 64)

    def test_btc008_text_slog_parses_exact_go_duration(self):
        raw = 'time=2026-01-01T00:00:02Z level=INFO msg="sandbox launch terminal" sid=sid-1 run_id=run-1 kind=create profile=memory result=success starting_total_duration=1.500000001s err=<nil>'
        record = TerminalLogParser().parse(raw, expected=IDENTITY, accepted_at=START, observed_at=50)
        self.assertEqual(record.internal_duration_ns, 1500000001)
        self.assertEqual(record.status, 'valid')

    def test_btc008_each_identity_field_must_match(self):
        for field in ('sid', 'run_id', 'kind', 'profile'):
            with self.subTest(field=field):
                raw = {**RAW, field: 'wrong'}
                record = TerminalLogParser().parse(json.dumps(raw), expected=IDENTITY, accepted_at=START, observed_at=50)
                self.assertEqual(record.status, 'invalid')
                self.assertEqual(record.reason, 'identity_mismatch')
                self.assertIsNone(record.internal_duration_ns)

    def test_btc008_failure_and_missing_or_zero_start_are_not_internal_success(self):
        variants = [({**RAW, 'result': 'failure'}, START), (RAW, None),
                    (RAW, AcceptedAtEvidence(IDENTITY, '0001-01-01T00:00:00Z', 'raw:zero')),
                    (RAW, AcceptedAtEvidence(LogIdentity('other', 'run-1', 'create', 'memory'), '2026-01-01T00:00:00Z', 'raw:other'))]
        for raw, start in variants:
            with self.subTest(raw=raw, start=start):
                record = TerminalLogParser().parse(json.dumps(raw), expected=IDENTITY, accepted_at=start, observed_at=50)
                self.assertNotEqual(record.status, 'valid')
                self.assertIsNone(record.internal_duration_ns)

    def test_btc008_duplicates_not_counted_conflicts_invalidate_semantic_record(self):
        parser = TerminalLogParser()
        first = parser.parse(json.dumps(RAW), expected=IDENTITY, accepted_at=START, observed_at=50)
        duplicate = parser.parse(json.dumps(RAW, sort_keys=True), expected=IDENTITY, accepted_at=START, observed_at=60)
        self.assertEqual(first.status, 'valid')
        self.assertEqual(duplicate.status, 'duplicate')
        self.assertIsNone(duplicate.internal_duration_ns)
        conflict = parser.parse(json.dumps({**RAW, 'starting_total_duration': 1}), expected=IDENTITY, accepted_at=START, observed_at=70)
        self.assertEqual(conflict.reason, 'conflicting_duplicate')
        self.assertIsNone(parser.records[IDENTITY].internal_duration_ns)
        replay = parser.parse(json.dumps(RAW), expected=IDENTITY, accepted_at=START, observed_at=80)
        self.assertEqual(replay.reason, 'conflicting_duplicate')

    def test_btc008_malformed_duplicate_fields_and_ambiguous_units_rejected(self):
        variants = [json.dumps({**RAW, 'starting_total_duration': True}),
                    json.dumps({**RAW, 'starting_total_duration': -1}),
                    json.dumps({**RAW, 'starting_total_duration': 1.5}),
                    json.dumps({**RAW, 'starting_total_duration': '1'}),
                    json.dumps(RAW)[:-1] + ',"sid":"sid-2"}',
                    'msg="sandbox launch terminal" sid=sid-1 sid=sid-2',
                    json.dumps({**RAW, 'msg': 'other sandbox launch terminal event'})]
        for raw in variants:
            with self.subTest(raw=raw):
                record = TerminalLogParser().parse(raw, expected=IDENTITY, accepted_at=START, observed_at=50)
                self.assertNotEqual(record.status, 'valid')
                self.assertIsNone(record.internal_duration_ns)
