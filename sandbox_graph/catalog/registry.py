"""Declarative node/method catalogue. Registration never implies adapter verification."""
from dataclasses import dataclass
from typing import Callable

NODE_GROUPS = {
    'run': ('R0', 'R1', 'RP', 'R2', 'R3', 'R6', 'R7'),
    'group': ('G0', 'T0', 'T1', 'T2', 'T4', 'T5', 'T6', 'T8', 'TC', 'TB', 'GB', 'GW', 'GE', 'E1', 'E2', 'C1', 'C2', 'C3'),
    'work': tuple(f'N{i:02}' for i in range(11)),
    'analysis': ('A1', 'A2', 'A3', 'A4', 'A5', 'A7'),
    'report': ('F1', 'F2', 'F4'),
}
METHODS = (
    'B01.TEMPLATE', 'B01.SNAPSHOT', 'B02.FULL', 'B02.PREPARED', 'B03.PAUSE',
    'B04.MEMORY', 'B04.DISK', 'B05.FULL', 'B05.PREPARED', 'B05.NATIVE',
    'B06.RUNNING', 'B06.PAUSED', 'B07.FIRST', 'B07.STEADY', 'B07.OUTPUT', 'B07.ERROR',
    'B08.LOCAL', 'B08.PUBLISHED', 'B09.PLATFORM', 'B09.IDLE', 'B09.PAUSED',
    'B10.GROWTH_LIST', 'B10.GROWTH_READY', 'B10.PROJECTION',
    'B10.FIXED_REQUESTS.WINDOW', 'B10.FIXED_REQUESTS.BATCH', 'B10.SLO_VERIFY',
    'E01', 'E02', 'E03', 'E04', 'E05', 'E06',
)

@dataclass(frozen=True)
class NodeDefinition:
    node_id: str
    handler_ref: str
    owner_role: str
    effect_class: str
    version: str = '1'
    implementation_status: str = 'planned'
    human_reason: str | None = None
    model_reason: str | None = None


class Registry:
    def __init__(self):
        self.nodes: dict[str, NodeDefinition] = {}
        self.handlers: dict[str, Callable] = {}
        self.methods: dict[tuple[str,str], dict] = {}

    def resolve_node(self, node_id: str, version: str = '1') -> NodeDefinition:
        node = self.nodes[node_id]
        if node.version != version:
            raise ValueError('node version not registered')
        return node

    def resolve_method(self, method_id: str, version: str = '1') -> dict:
        return dict(self.methods[(method_id, version)])

    def register_handler(self, name: str, handler: Callable):
        if name in self.handlers and self.handlers[name] is not handler:
            raise ValueError('handler reference is immutable')
        self.handlers[name] = handler


def standard_registry() -> Registry:
    r = Registry()
    for group, nodes in NODE_GROUPS.items():
        for node in nodes:
            r.nodes[node] = NodeDefinition(node, node, group, 'external_mutation' if node in {'C2','N02','N05','N09'} else 'read_only')
    for method in METHODS:
        r.methods[(method, '1')] = {'method_id': method, 'version': '1', 'node_id': 'N05', 'adapter_status': 'unverified'}
    return r
