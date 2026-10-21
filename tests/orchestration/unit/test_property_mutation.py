"""P14.3 / T11: finite-seed property tests over key orchestrator invariants."""
import random

import pytest

from sandbox_graph.domain.transitions import merge_revision
from sandbox_graph.runtime.events import EventEnvelope, EventService


def test_merge_revision_is_monotonic_idempotent_and_conflict_aware():
    rng = random.Random(7)
    for _ in range(200):
        v1, v2 = rng.randint(0, 5), rng.randint(0, 5)
        c1, c2 = rng.randint(0, 9), rng.randint(0, 9)
        a, b = (v1, {'x': c1}), (v2, {'x': c2})
        if v1 == v2 and c1 != c2:
            with pytest.raises(ValueError):
                merge_revision(a, b)                    # same version, different content
            continue
        merged = merge_revision(a, b)
        assert merged in (a, b)                          # never invents a new record
        assert merged[0] >= min(v1, v2)                  # never regresses
        assert merge_revision(merged, merged) == merged  # idempotent on the winner


def test_event_consume_is_contiguous_and_gap_aware(repository):
    service = EventService(repository)
    rng = random.Random(3)
    seqs = sorted(rng.sample(range(1, 20), 5))
    for seq in seqs:
        service.append(EventEnvelope(event_id=f'e{seq}', producer_scope='s', producer_epoch=1,
                                     aggregate_id='a', aggregate_seq=seq, run_ref='r', plan_ref='p',
                                     graph_ref='g', kind='k', payload={}))
    accepted = service.consume('c', 'a', allowed_scopes={'s'})
    contiguous = 0
    for event in accepted:
        if event.aggregate_seq != contiguous + 1:
            break
        contiguous += 1
    assert contiguous == len(accepted)                    # never skips a gap
    assert len(accepted) == (seqs[0] if seqs[0] == 1 else 0)
