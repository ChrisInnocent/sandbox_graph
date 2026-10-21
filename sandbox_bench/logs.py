"""Kuasar slog terminal evidence parser, versioned against local source.

Source: orchestrator/internal/orch/orch.go::runLaunch. The source substitutes now
when AcceptedAt is absent, so duration alone cannot prove a valid start boundary.
Only successful, unambiguous records with independently evidenced AcceptedAt
contribute an internal duration. No other product's internal fields are synthesized.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import shlex
from typing import Any

PARSER_VERSION = 'kuasar-launch-terminal-v1'


@dataclass(frozen=True)
class LogIdentity:
    sid: str
    run_id: str
    kind: str
    profile: str

    def __post_init__(self) -> None:
        if any(not isinstance(x, str) or not x for x in (self.sid, self.run_id, self.kind, self.profile)):
            raise ValueError('complete operation association is required')
        if self.kind not in {'create', 'resume'}:
            raise ValueError('launch kind must be create or resume')


@dataclass(frozen=True)
class AcceptedAtEvidence:
    identity: LogIdentity
    accepted_at: str
    evidence_ref: str

    @property
    def valid(self) -> bool:
        try:
            stamp = datetime.fromisoformat(self.accepted_at.replace('Z', '+00:00'))
            return bool(self.evidence_ref) and stamp.year > 1 and stamp.utcoffset() is not None
        except (ValueError, TypeError, AttributeError):
            return False


@dataclass(frozen=True)
class ParsedTerminal:
    status: str
    reason: str | None
    identity: LogIdentity
    internal_duration_ns: int | None
    source_event_time: Any
    observed_at: float
    raw_record: str
    raw_sha256: str
    accepted_at_ref: str | None
    parser_version: str = PARSER_VERSION


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def _duration_ns(value: Any) -> int:
    # slog JSON represents time.Duration as integer nanoseconds; text uses Go units.
    if type(value) is int:
        if value < 0:
            raise ValueError('negative duration')
        return value
    if not isinstance(value, str) or not value:
        raise ValueError('duration must have verified units')
    if value == '0s':
        return 0
    units = {'h': 3600000000000, 'm': 60000000000, 's': 1000000000,
             'ms': 1000000, 'us': 1000, 'µs': 1000, 'μs': 1000, 'ns': 1}
    pattern = re.compile(r'(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)')
    cursor, total = 0, Decimal(0)
    for match in pattern.finditer(value):
        if match.start() != cursor:
            raise ValueError('invalid duration')
        total += Decimal(match[1]) * units[match[2]]
        cursor = match.end()
    if cursor != len(value) or not cursor or total != total.to_integral_value():
        raise ValueError('invalid or sub-nanosecond duration')
    return int(total)


class TerminalLogParser:
    def __init__(self, *, max_record_bytes: int = 65536):
        if type(max_record_bytes) is not int or max_record_bytes <= 0:
            raise ValueError('max_record_bytes must be positive')
        self.max_record_bytes = max_record_bytes
        self.records: dict[LogIdentity, ParsedTerminal] = {}
        self._semantic: dict[LogIdentity, str] = {}
        self._conflicts: set[LogIdentity] = set()

    def parse(self, raw: str, *, expected: LogIdentity,
              accepted_at: AcceptedAtEvidence | None, observed_at: float) -> ParsedTerminal:
        if not isinstance(raw, str):
            raise ValueError('raw record must be text')
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at):
            raise ValueError('observed_at must be a finite collector timestamp')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        def result(status: str, reason: str | None, fields: dict | None = None,
                   duration: int | None = None) -> ParsedTerminal:
            return ParsedTerminal(status, reason, expected, duration,
                (fields or {}).get('time'), observed_at, raw, digest,
                accepted_at.evidence_ref if accepted_at else None)
        if len(raw.encode()) > self.max_record_bytes:
            return result('invalid', 'record_too_large')
        try:
            if raw.lstrip().startswith('{'):
                fields = json.loads(raw, object_pairs_hook=_unique_object,
                                    parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            else:
                pairs = []
                for part in shlex.split(raw):
                    if '=' not in part:
                        raise ValueError('not a slog key-value record')
                    pairs.append(tuple(part.split('=', 1)))
                fields = _unique_object(pairs)
            if not isinstance(fields, dict):
                raise ValueError('record must be an object')
        except (ValueError, TypeError):
            return result('invalid', 'malformed_record')
        if fields.get('msg') != 'sandbox launch terminal':
            return result('invalid', 'not_terminal_record', fields)
        if any(fields.get(name) != getattr(expected, name) for name in ('sid', 'run_id', 'kind', 'profile')):
            return result('invalid', 'identity_mismatch', fields)
        # Deduplication is semantic, unaffected by whitespace or collector arrival.
        semantic = json.dumps(fields, sort_keys=True, separators=(',', ':'))
        if expected in self._conflicts:
            return result('invalid', 'conflicting_duplicate', fields)
        if expected in self._semantic:
            if self._semantic[expected] != semantic:
                self._conflicts.add(expected)
                conflict = result('invalid', 'conflicting_duplicate', fields)
                self.records[expected] = conflict
                return conflict
            return result('duplicate', 'duplicate_terminal', fields)
        self._semantic[expected] = semantic
        if fields.get('result') != 'success':
            parsed = result('invalid', 'terminal_failure', fields)
        elif not accepted_at or not accepted_at.valid or accepted_at.identity != expected:
            parsed = result('unavailable', 'missing_start_evidence', fields)
        else:
            try:
                duration = _duration_ns(fields.get('starting_total_duration'))
                parsed = result('valid', None, fields, duration)
            except (ValueError, InvalidOperation):
                parsed = result('invalid', 'invalid_duration', fields)
        self.records[expected] = parsed
        return parsed
