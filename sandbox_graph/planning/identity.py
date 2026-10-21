"""来源与端点身份（D17）。图侧只做绑定与可比性判定，不按接口参数名称推断语义。

这些字段随 `Case` 进入计划哈希，并随 `WorkKey.condition_hash` 决定工作实例身份；
不同 kind／path／topology／endpoint 的工作实例因此天然分离，不靠事后标签区分。
"""
from __future__ import annotations

SOURCE_DIMENSIONS = ('artifact_kind', 'launch_path', 'producer_step', 'cost_scope')
ENDPOINT_DIMENSIONS = ('topology', 'endpoint_kind', 'readiness_signal')
REQUIRED_SOURCE_DIMENSIONS = ('artifact_kind', 'launch_path', 'producer_step')
BLACKBOX_ENDPOINTS = ('api_complete', 'state_ready', 'server_terminal', 'exec_complete')
CREATION_METHOD_PREFIXES = ('B00.', 'B01.')


def source_identity(case) -> dict:
    return {name: getattr(case, name, None) for name in SOURCE_DIMENSIONS}


def endpoint_identity(case) -> dict:
    return {name: getattr(case, name, None) for name in ENDPOINT_DIMENSIONS}


def binding_payload(case) -> dict:
    """Identity carried into the work body so N05/N08 bind the same experiment."""
    return {'source_identity': source_identity(case),
            'endpoint_identity': endpoint_identity(case),
            'endpoint_ref': getattr(case, 'endpoint_ref', None)}


def condition_identity(case, condition: dict) -> dict:
    """Condition hash input: the declared condition plus the source/endpoint identity."""
    return {'condition': condition, **source_identity(case), **endpoint_identity(case)}


def missing_required_identity(case) -> tuple[str, ...]:
    """Identity a creation case must declare; never defaulted from the method name."""
    if not str(getattr(case, 'method_id', '')).startswith(CREATION_METHOD_PREFIXES):
        return ()
    return tuple(name for name in REQUIRED_SOURCE_DIMENSIONS
                 if not getattr(case, name, None))


def is_whitebox(case) -> bool:
    """guest_ready is an injected white-box endpoint, never merged with black-box data."""
    return getattr(case, 'endpoint_kind', None) == 'guest_ready'


def comparability_key(case) -> tuple:
    """Only samples sharing this key may be aggregated into one distribution."""
    kind = getattr(case, 'endpoint_kind', None)
    return (kind, 'whitebox' if is_whitebox(case) else 'blackbox')
