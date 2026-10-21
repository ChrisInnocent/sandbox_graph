"""创建端点拓扑与就绪信号的只读解析（BCD-13 §13.2／§13.4，图侧 D17）。

同步/异步是入口与已审计产品行为的属性，禁止按接口名称推断。解析结果既可用于
脚本侧实现，也可被图侧（组 29）作为身份与可比性键使用。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Callable

from .scenarios import ScenarioBlocked

_TOPOLOGIES = ('router', 'node_direct', 'co_located_cli')
_PRODUCTS = ('cubesandbox', 'kuasar', 'agentenv')
# Endpoints observable from outside the node without an injected descriptor.
_BLACKBOX_ENDPOINTS = ('api_complete', 'state_ready', 'server_terminal', 'exec_complete')


@dataclass(frozen=True)
class EndpointResolution:
    """Resolved creation-entry semantics (BCD-13 §13.4).

    status: 'resolved' | 'unverified' | 'blocked'. A blocked/unverified record must
    not be silently downgraded to another topology.
    """
    product: str
    topology: str
    endpoint_kind: str
    readiness_signal: str
    synchronous: bool
    management_plane_present: bool | None
    path_evidence: str
    status: str

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_endpoint(*, product: str, product_version: str, topology: str,
                     probe: Callable[[str], dict] | None, evidence_ref: str) -> EndpointResolution:
    """Resolve endpoint semantics from the declared topology and a read-only probe.

    `probe(topology)` must only observe; it may return management_plane_present as a
    bool or leave it unknown. Unsupported (product, topology) pairs raise
    ScenarioBlocked('endpoint_unresolved') rather than guessing.
    """
    if topology not in _TOPOLOGIES or product not in _PRODUCTS:
        raise ScenarioBlocked('endpoint_unresolved')
    if not product_version:
        # No audited version means the synchrony claim is unverified, not assumed.
        return EndpointResolution(product, topology, 'api_complete', 'none',
                                  synchronous=False, management_plane_present=None,
                                  path_evidence=evidence_ref or 'missing-product-version',
                                  status='unverified')

    management: bool | None = None
    if probe is not None:
        observed = probe(topology)
        if isinstance(observed, dict):
            declared = observed.get('management_plane_present')
            if declared is not None and not isinstance(declared, bool):
                raise ValueError('management_plane_present must be boolean or null')
            management = declared

    if topology == 'co_located_cli':
        # The ready-fd endpoint is only audited for the Kuasar node runner.
        if product != 'kuasar':
            raise ScenarioBlocked('endpoint_unresolved')
        return EndpointResolution(product, topology, 'guest_ready', 'ready_fd',
                                  synchronous=False, management_plane_present=False,
                                  path_evidence=evidence_ref, status='resolved')

    if topology == 'router':
        if management is False:
            return EndpointResolution(product, topology, 'api_complete', 'none',
                                      synchronous=True, management_plane_present=False,
                                      path_evidence=evidence_ref, status='blocked')
        if management is None:
            return EndpointResolution(product, topology, 'api_complete', 'none',
                                      synchronous=True, management_plane_present=None,
                                      path_evidence=evidence_ref, status='unverified')

    # Audited behaviour (BCD-13 §13.2): Kuasar router blocks to running; the data-plane
    # node API returns `starting`. Cube and AgentENV return ready on both entries.
    if product == 'kuasar':
        synchronous = topology == 'router'
        return EndpointResolution(product, topology, 'api_complete',
                                  'none' if synchronous else 'poll_state',
                                  synchronous=synchronous,
                                  management_plane_present=(topology == 'router'),
                                  path_evidence=evidence_ref, status='resolved')
    return EndpointResolution(product, topology, 'api_complete', 'none',
                              synchronous=True,
                              management_plane_present=(topology == 'router'),
                              path_evidence=evidence_ref, status='resolved')


def endpoint_requires_poll(resolution: EndpointResolution) -> bool:
    """True only when the create entry returns before the sandbox is ready."""
    return resolution.readiness_signal == 'poll_state'


def endpoint_comparable(left: EndpointResolution, right: EndpointResolution) -> bool:
    """Comparable only when both share the same black-box endpoint kind."""
    return (left.endpoint_kind == right.endpoint_kind
            and left.endpoint_kind in _BLACKBOX_ENDPOINTS
            and right.endpoint_kind in _BLACKBOX_ENDPOINTS)
