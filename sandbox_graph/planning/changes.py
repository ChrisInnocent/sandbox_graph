"""Conservative compatibility classification; old evidence identity never changes."""
from ..domain.models import Model

class ImpactPlan(Model):
    changed_fields: tuple[str,...]
    rerun_nodes: tuple[str,...]
    requires_measurement: bool
    reason: str
    reuse_from: tuple[str,...] = ()


def diff(old:dict,new:dict,*,available_events=frozenset()) -> ImpactPlan:
    changed=tuple(sorted(k for k in set(old)|set(new) if old.get(k)!=new.get(k)))
    keys=set(changed)
    if not keys: return ImpactPlan(changed_fields=(),rerun_nodes=(),requires_measurement=False,reason='identical')
    if keys<={'wording'}: nodes=('F1','F2','F4');measurement=False;reason='presentation only'
    elif keys<={'layout','wording'}: nodes=('A7','F1','F2','F4');measurement=False;reason='workbook layout'
    elif keys<={'threshold','rules','wording','layout'}: nodes=('A3','A4','A5','A7','F1','F2','F4');measurement=False;reason='recompute using compatible raw evidence'
    elif keys=={'completion_event'} and new.get('completion_event') in available_events:
        nodes=('A3','A4','A5','A7','F1','F2','F4');measurement=False;reason='new completion event is present; semantics/time-domain review still required'
    else: nodes=tuple(f'N{i:02}' for i in range(11));measurement=True;reason='measurement contract changed or compatibility unknown'
    return ImpactPlan(changed_fields=changed,rerun_nodes=nodes,requires_measurement=measurement,reason=reason)
