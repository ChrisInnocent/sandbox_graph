"""State changes preserve execution facts independently of later review/cleanup."""
from .models import NodeResult, canonical_hash

_ALLOWED = {
    'pending': {'running', 'blocked', 'cancelled'},
    'running': {'succeeded', 'failed', 'blocked', 'cancelled'},
    'blocked': {'pending', 'cancelled'},
    'succeeded': set(), 'failed': set(), 'cancelled': set(),
}


def transition(current: NodeResult, event: str, expected_revision: int, revision: int) -> NodeResult:
    if revision != expected_revision:
        raise ValueError('VERSION_CONFLICT')
    if event not in _ALLOWED[current.execution_status]:
        raise ValueError(f'illegal transition {current.execution_status} -> {event}; create a new attempt')
    return NodeResult.model_validate(current.model_dump() | {'execution_status': event})


def merge_revision(current: tuple[int, dict], incoming: tuple[int, dict]) -> tuple[int, dict]:
    if incoming[0] == current[0] and canonical_hash(current[1]) != canonical_hash(incoming[1]):
        raise ValueError('same revision has conflicting content')
    return incoming if incoming[0] > current[0] else current


def retry_allowed(code: str, *, attempts: int, max_attempts: int, now: float, deadline: float) -> bool:
    if max_attempts < 1 or deadline <= now:
        return False
    return code == 'TRANSIENT_READ' and attempts < max_attempts
