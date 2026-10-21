"""Measurement semantics remain stable across graph identity changes only."""
from sandbox_graph.domain.models import canonical_hash


def measurement_contract_hash(case):
    body=case.model_dump(mode='json')
    for field in ('work_id','group_id','plan_ref','contract_ref'):
        body.pop(field)
    return canonical_hash(body)
