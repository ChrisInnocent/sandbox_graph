"""D18.4 / ORC-025,042,062: observation identity, cross-restart and truncation."""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.observation import ObservationService


def test_observation_identity_is_immutable(repository):
    service = ObservationService(repository)
    service.start('s1', 'scope', operation_id='op', clock_epoch='e1', deadline=10 ** 9)
    with pytest.raises(Conflict):
        service.start('s1', 'other', operation_id='op', clock_epoch='e1', deadline=10 ** 9)


def test_cross_restart_epoch_cannot_be_appended_and_restart_needs_a_new_epoch(repository):
    service = ObservationService(repository)
    service.start('s1', 'scope', operation_id='op', clock_epoch='e1', deadline=10 ** 9)
    service.append('s1', seq=1, clock_epoch='e1', value=1)
    with pytest.raises(Conflict):
        service.append('s1', seq=2, clock_epoch='e2', value=2)   # cross-restart monotonic sample
    with pytest.raises(Conflict):
        service.restart('s1', clock_epoch='e1')                  # restart requires a new epoch
    service.restart('s1', clock_epoch='e2')
    service.append('s1', seq=2, clock_epoch='e2', value=2)


def test_missing_sequence_and_interruption_never_become_valid(repository):
    service = ObservationService(repository)
    service.start('s1', 'scope', operation_id='op', clock_epoch='e1', deadline=10 ** 9)
    service.append('s1', seq=1, clock_epoch='e1', value=1)
    service.restart('s1', clock_epoch='e2')
    service.append('s1', seq=2, clock_epoch='e2', value=2)
    result = service.stop('s1', last_seq=3)                      # seq 3 missing
    assert result['missing_sequences'] == [3]
    assert result['data_status'] == 'insufficient'              # never filled with zero
    assert service.stop('s1', last_seq=3)['last_seq'] == 3      # idempotent
    with pytest.raises(Conflict):
        service.stop('s1', last_seq=1)                          # would truncate recorded samples
