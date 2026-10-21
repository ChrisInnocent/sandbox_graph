"""Topology-aware host component catalog for resource attribution (RT-21).

The components to sample are a function of the product *and* the cluster topology:
which node roles exist, and where management/data roles are co-located. The catalog
declares, per product, each host component's class, whether it is per-sandbox-instance,
whether it is shared/auxiliary, and read-only process-match hints for discovery.

In-guest processes (e.g. ``envd``) are never host sandbox processes and are excluded.

Discovery is advisory: a process that matches zero or several catalog entries without a
disambiguating argv marker is reported as unresolved, never silently assigned.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

ComponentClass = Literal['management_plane', 'data_plane_management', 'sandbox_runtime', 'shared_auxiliary']
NodeRole = Literal['management', 'data']
Confidence = Literal['daemon', 'cli', 'library', 'unknown']


@dataclass(frozen=True)
class Component:
    component_id: str
    component_class: ComponentClass
    node_roles: tuple[NodeRole, ...]
    per_instance: bool
    shared: bool
    process_names: tuple[str, ...]
    argv_markers: tuple[str, ...] = ()
    confidence: Confidence = 'daemon'
    guest_side: bool = False


_CATALOG: dict[str, tuple[Component, ...]] = {
    'kuasar': (
        Component('kuasar.registry', 'management_plane', ('management',), False, False, ('cluster-ctl',), ('registry',)),
        Component('kuasar.router', 'management_plane', ('management',), False, False, ('cluster-ctl',), ('router',)),
        Component('kuasar.placer', 'management_plane', ('management',), False, False, ('cluster-ctl',), ('placer',)),
        Component('kuasar.node-conductor', 'data_plane_management', ('data',), False, False, ('node-ctl',), ('conductor',)),
        Component('kuasar.node-proxy', 'data_plane_management', ('data',), False, False, ('node-ctl',), ('proxy',)),
        Component('kuasar.node-telemetry', 'data_plane_management', ('data',), False, False, ('node-ctl',), ('telemetry',)),
        Component('kuasar.node-netserve', 'data_plane_management', ('data',), False, False, ('node-ctl',), ('netserve',)),
        Component('kuasar.node-resource', 'data_plane_management', ('data',), False, False, ('node-ctl',), ('resource',)),
        Component('kuasar.run-sandbox', 'sandbox_runtime', ('data',), True, False, ('node-ctl',), ('run-sandbox',)),
        Component('kuasar.sandbox-ctl', 'sandbox_runtime', ('data',), True, False, ('sandbox-ctl',)),
        Component('kuasar.cloud-hypervisor', 'sandbox_runtime', ('data',), True, False, ('cloud-hypervisor',)),
    ),
    'cubesandbox': (
        Component('cubesandbox.cube-api', 'management_plane', ('management',), False, False, ('cube-api',)),
        Component('cubesandbox.cubemaster', 'management_plane', ('management',), False, False, ('cubemaster',)),
        Component('cubesandbox.cubeops', 'management_plane', ('management',), False, False, ('cubeops',)),
        Component('cubesandbox.templatecenter', 'management_plane', ('management',), False, False, ('templatecenter',)),
        Component('cubesandbox.cube-proxy', 'management_plane', ('management', 'data'), False, False, ('nginx', 'openresty'), ('CubeProxy',), 'unknown'),
        Component('cubesandbox.cubelet', 'data_plane_management', ('data',), False, False, ('cubelet',)),
        Component('cubesandbox.cubevs', 'data_plane_management', ('data',), False, False, ('cubevs',)),
        Component('cubesandbox.lifecycle-manager', 'data_plane_management', ('data',), False, False, ('cube-lifecycle-manager',)),
        Component('cubesandbox.cube-egress', 'shared_auxiliary', ('data',), False, True, ('nginx', 'openresty'), ('CubeEgress',), 'unknown'),
        Component('cubesandbox.cube-s3lvol', 'shared_auxiliary', ('data',), False, True, (), (), 'library'),
        Component('cubesandbox.cubecow', 'shared_auxiliary', ('data',), False, True, (), (), 'library'),
        Component('cubesandbox.cube-runtime', 'sandbox_runtime', ('data',), True, False, ('cube-runtime',)),
        Component('cubesandbox.cube-hypervisor', 'sandbox_runtime', ('data',), True, False, ('cube-hypervisor',)),
    ),
    'agentenv': (
        Component('agentenv.gateway', 'management_plane', ('management',), False, False, ('gateway',)),
        Component('agentenv.scheduler', 'management_plane', ('management',), False, False, ('scheduler',)),
        Component('agentenv.server', 'data_plane_management', ('data',), False, False, ('server', 'agentenv')),
        Component('agentenv.ublk-daemon', 'data_plane_management', ('data',), False, False, ('uvm-ublk-daemon', 'ublk-daemon')),
        Component('agentenv.overlaybd', 'shared_auxiliary', ('data',), False, True, ('overlaybd', 'overlaybd-tcmu')),
        Component('agentenv.firecracker', 'sandbox_runtime', ('data',), True, False, ('firecracker',)),
    ),
}

IN_GUEST_COMPONENTS = {
    'kuasar': ('envd', 'guest-runtime agents'),
    'cubesandbox': ('guest-init', 'envd'),
    'agentenv': ('envd',),
}


def component_catalog(product: str) -> tuple[Component, ...]:
    if product not in _CATALOG:
        raise ValueError(f'unknown product {product!r}')
    return _CATALOG[product]


class Topology(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    product: Literal['cubesandbox', 'kuasar', 'agentenv']
    nodes: dict[str, tuple[NodeRole, ...]]

    @model_validator(mode='after')
    def validate_nodes(self):
        if not self.nodes:
            raise ValueError('at least one node required')
        for node_id, roles in self.nodes.items():
            if not node_id or not roles:
                raise ValueError('node id and at least one role required')
            if any(role not in ('management', 'data') for role in roles):
                raise ValueError('unknown node role')
        return self

    @property
    def has_management_plane(self) -> bool:
        return any('management' in roles for roles in self.nodes.values())


def planned_components(topology: Topology, node_id: str) -> tuple[Component, ...]:
    """Declared host components for one node, by its roles; in-guest excluded."""
    if node_id not in topology.nodes:
        raise KeyError(node_id)
    roles = set(topology.nodes[node_id])
    return tuple(c for c in component_catalog(topology.product)
                 if not c.guest_side and roles & set(c.node_roles))


def expected_component_ids(topology: Topology, node_id: str, *,
                           component_class: ComponentClass | None = None) -> tuple[str, ...]:
    return tuple(c.component_id for c in planned_components(topology, node_id)
                 if component_class is None or c.component_class == component_class)


def match_process(product: str, comm: str, argv: tuple[str, ...] = ()) -> str | None:
    """Read-only discovery hint: component id, or None when unresolved/ambiguous.

    A shared binary (e.g. ``node-ctl``, OpenResty) is only resolved by an explicit argv
    marker; without it the result is ambiguous and must stay unattributed.
    """
    matches = []
    for component in component_catalog(product):
        if component.guest_side or comm not in component.process_names:
            continue
        if component.argv_markers and not any(marker in argv for marker in component.argv_markers):
            continue
        matches.append(component.component_id)
    return matches[0] if len(matches) == 1 else None


def inventory_plan(topology: Topology) -> dict:
    """Declared per-node component expectations, used to validate discovery coverage."""
    plan = {}
    for node_id, roles in topology.nodes.items():
        components = planned_components(topology, node_id)
        plan[node_id] = {
            'roles': list(roles),
            'components': [c.component_id for c in components],
            'per_instance': sorted(c.component_id for c in components if c.per_instance),
            'shared_auxiliary': sorted(c.component_id for c in components if c.shared),
        }
    return {'product': topology.product, 'management_plane': topology.has_management_plane,
            'in_guest_excluded': list(IN_GUEST_COMPONENTS[topology.product]), 'nodes': plan}


def build_inventory(topology: Topology, node_id: str, discovered) -> dict:
    """Map discovered host processes to declared components (read-only, fail-closed).

    Each discovered item is ``{'identity': {...}, 'comm': str, 'argv': [...],  # optional
    'sandbox_id', 'generation'}``. A process that matches no declared component stays
    ``unresolved`` and is never assigned; a per-instance component without a sandbox id
    is unresolved too. Declared-but-not-discovered components are reported as ``missing``.
    """
    if node_id not in topology.nodes:
        raise KeyError(node_id)
    catalog = {c.component_id: c for c in planned_components(topology, node_id)}
    rows = []
    unresolved = []
    seen = set()
    for item in discovered:
        component_id = match_process(topology.product, item.get('comm', ''), tuple(item.get('argv', ())))
        component = catalog.get(component_id) if component_id else None
        if component is None:
            unresolved.append({'identity': item.get('identity'), 'comm': item.get('comm'),
                               'reason': 'no_declared_component'})
            continue
        if component.per_instance and not item.get('sandbox_id'):
            unresolved.append({'identity': item.get('identity'), 'component': component_id,
                               'reason': 'per_instance_requires_sandbox_id'})
            continue
        seen.add(component_id)
        rows.append({'identity': item.get('identity'),
                     'roles': [component.component_class], 'components': [component_id],
                     'sandbox_id': item.get('sandbox_id'), 'generation': item.get('generation')})
    missing = sorted(set(catalog) - seen - {c.component_id for c in catalog.values() if c.confidence == 'library'})
    return {'product': topology.product, 'node_id': node_id,
            'rows': rows, 'unresolved': unresolved, 'missing': missing,
            'status': 'valid' if not unresolved and not missing else 'partial'}
