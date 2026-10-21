"""P6.5 / ORC-026,059-064: only a registered, structured, hashed operation may execute."""
import pytest

from sandbox_graph.domain.models import Operation, ScriptRef, canonical_hash
from sandbox_graph.runtime.runner import OperationService

SCRIPT = ScriptRef(script_id='s', version='1', digest='a' * 64)
BODY = {'step_id': 'N05'}


def operation(**extra):
    data = dict(operation_id='o', input_hash=canonical_hash(BODY), script_ref=SCRIPT, scope='g',
                input=BODY, deadline=10 ** 9, run_id='r')
    data.update(extra)
    return Operation(**data)


def service():
    return OperationService(None, None, {('s', '1'): SCRIPT})


def test_registered_operation_contract_is_accepted():
    service()._validate(operation())


def test_unregistered_script_or_missing_run_is_rejected():
    with pytest.raises(ValueError, match='registered'):
        service()._validate(operation(script_ref=ScriptRef(script_id='x', version='1', digest='b' * 64)))
    with pytest.raises(ValueError, match='run_id'):
        service()._validate(operation(run_id=''))


def test_input_hash_must_match_the_structured_input():
    with pytest.raises(ValueError):
        operation(input_hash='b' * 64)
