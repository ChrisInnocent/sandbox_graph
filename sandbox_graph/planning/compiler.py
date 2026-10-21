"""Finite plan compiler: preview is pure and never dispatches work."""
from typing import Any, Literal
from pydantic import Field
from ..domain.models import Model, canonical_hash


class Selection(Model):
    groups: tuple[str, ...] | None = None
    sandbox_types: tuple[str, ...] | None = None
    scenarios: tuple[str, ...] | None = None
    methods: tuple[str, ...] | None = None
    priorities: tuple[str, ...] | None = None
    exclude: tuple[str, ...] = ()
    stage: Literal['all', 'check', 'analysis', 'report'] = 'all'


class Case(Model):
    case_id: str
    group_id: str
    sandbox_type: str
    scenario_id: str
    method_id: str
    method_version: str = Field(default="1",min_length=1)
    repeat_id: int = Field(default=1,ge=1,strict=True)
    priority: str
    conditions: tuple[dict[str, Any], ...] = ()
    dependencies: tuple[str, ...] = ()
    capability: Literal['supported', 'unsupported', 'unverified'] = 'unverified'
    adapter_verified: bool = False
    conflict_domains: tuple[str, ...] = ()
    contract_ref: str = ''
    environment_ref: str = ''


class Gap(Model):
    case_id: str
    code: str
    reason: str


class Plan(Model):
    cases: tuple[Case, ...]
    dependencies: tuple[str, ...]
    gaps: tuple[Gap, ...]
    selection: Selection
    status: Literal['ready', 'blocked', 'empty', 'no_match']
    stage: str
    catalog_hash: str
    auth_hash: str
    evidence_hash: str
    graph_ref: str = 'default:1'
    contract_ref: str = ''

    @property
    def hash(self):
        return canonical_hash(self)


class PlanCompiler:
    def __init__(self, catalog: list[Case]):
        self.catalog = tuple(Case.model_validate(c.model_dump()) for c in catalog)
        if len({c.case_id for c in self.catalog}) != len(self.catalog):
            raise ValueError('duplicate case ID')

    def compile(self, selection: Selection, *, authorized_groups: set[str], evidence: dict | None = None, graph_ref: str = 'default:1', contract_ref: str = '') -> Plan:
        axes = {'groups':'group_id','sandbox_types':'sandbox_type','scenarios':'scenario_id','methods':'method_id','priorities':'priority'}
        for axis, field in axes.items():
            values = getattr(selection, axis)
            unknown = set(values or ()) - {getattr(c, field) for c in self.catalog}
            if unknown:
                raise ValueError(f'unknown {axis}: {sorted(unknown)}')
        known = {c.case_id for c in self.catalog} | {d for c in self.catalog for d in c.dependencies}
        if set(selection.exclude) - known:
            raise ValueError('unknown excluded ID')
        explicit_empty = any(getattr(selection, axis) == () for axis in axes)
        cases = tuple(c for c in self.catalog if all(getattr(selection, axis) is None or getattr(c, field) in getattr(selection, axis) for axis, field in axes.items()) and c.case_id not in selection.exclude)
        if selection.stage in {'analysis', 'report'}:
            cases = ()
        if {c.group_id for c in cases} - authorized_groups:
            raise PermissionError('selected groups exceed authorization')
        for group in {case.group_id for case in cases}:
            refs={case.environment_ref for case in cases if case.group_id==group and case.environment_ref}
            if len(refs)>1: raise ValueError(f'conflicting environment references in group {group}')
        dependencies, gaps = set(), []
        for c in cases:
            if c.capability == 'unsupported':
                gaps.append(Gap(case_id=c.case_id, code='CAPABILITY_UNSUPPORTED', reason='coverage retained; no measurement sample'))
                continue
            if c.capability == 'unverified' or not c.adapter_verified:
                gaps.append(Gap(case_id=c.case_id, code='ADAPTER_UNVERIFIED', reason='verified adapter required'))
            if not c.conditions and selection.stage == 'all':
                gaps.append(Gap(case_id=c.case_id, code='INPUT_MISSING', reason='finite condition tuples required'))
            for dependency in c.dependencies:
                if dependency in selection.exclude:
                    gaps.append(Gap(case_id=c.case_id, code='DEPENDENCY_EXCLUDED', reason=dependency))
                else:
                    dependencies.add(dependency)
        blocked_cases={g.case_id for g in gaps if g.code!='CAPABILITY_UNSUPPORTED'}
        status = 'empty' if explicit_empty else 'no_match' if not cases and selection.stage not in {'analysis', 'report'} else 'blocked' if cases and all(c.case_id in blocked_cases for c in cases) else 'ready'
        return Plan(cases=cases, dependencies=tuple(sorted(dependencies)), gaps=tuple(gaps), selection=selection, status=status, stage=selection.stage, catalog_hash=canonical_hash([c.model_dump() for c in self.catalog]), auth_hash=canonical_hash(sorted(authorized_groups)), evidence_hash=canonical_hash(evidence or {}), graph_ref=graph_ref, contract_ref=contract_ref)
