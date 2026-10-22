"""结果与报告的合理性/正确性审视（只出 findings，绝不修改事实）。

由归属 agent 在节点/报告现场执行；可复核性要求它**只能报告问题**，不能改测量值、
证据或报告文字。规则可确定性执行，无法判定的保持显式缺口。
"""
from __future__ import annotations

import math

_PLAUSIBLE = ('latency_s', 'throughput', 'pss_bytes', 'cpu_seconds', 'memory_peak_bytes',
              'elapsed_s', 'successes', 'submitted')


def review_result_numbers(values):
    """节点结果里的数值合理性：必须是非负有限数，布尔不算数。"""
    findings = []
    for key in _PLAUSIBLE:
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, bool):
            findings.append(f'{key} must be a number, not a boolean')
        elif isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
            findings.append(f'{key} is not a plausible non-negative finite number')
    if all(k in values for k in ('successes', 'submitted')):
        if isinstance(values['successes'], int) and isinstance(values['submitted'], int) \
                and values['successes'] > values['submitted']:
            findings.append('successes exceed submitted requests')
    return findings


def review_report_facts(facts, *, expected_case_ids=None):
    """事实报告的正确性：引用可溯源、覆盖分母、可比性不得越界、有效值不得为空。"""
    findings = []
    cases = facts.get('cases') or []
    case_ids = {c.get('case_id') for c in cases if isinstance(c, dict)}
    manifests = {c.get('manifest_sha256') for c in cases if isinstance(c, dict)}
    if expected_case_ids is not None:
        missing = set(expected_case_ids) - case_ids
        if missing:
            findings.append(f'coverage is missing selected cases: {sorted(missing)}')
    for fact in facts.get('facts') or []:
        if not isinstance(fact, dict):
            findings.append('fact entry is not an object')
            continue
        value = fact.get('value')
        if fact.get('qualification') == 'valid' and value is None:
            findings.append(f"valid fact {fact.get('fact_id')} has a null value")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            findings.append(f"non-finite value for fact {fact.get('fact_id')}")
        if fact.get('manifest_sha256') not in manifests:
            findings.append(f"fact {fact.get('fact_id')} cites an unknown manifest")
    for pair in facts.get('pairs') or []:
        if not isinstance(pair, dict):
            continue
        if pair.get('status') == 'comparable' and (pair.get('left') not in case_ids or pair.get('right') not in case_ids):
            findings.append('comparable pair references cases outside the frozen input')
    return findings


def review_interpretation(layer):
    """可选解释层：每条必须引用已有 fact_id，且不得改写事实层。"""
    findings = []
    if layer is None:
        return findings
    if layer.get('interpretation_status') not in (None, 'unverified_model_interpretation'):
        findings.append('interpretation must be marked unverified_model_interpretation')
    return findings
