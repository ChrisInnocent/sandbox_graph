"""Deterministic, bounded resolution of environment tuning parameters."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from fractions import Fraction
from typing import Any, Mapping


_UNIT_FACTORS = {
    "B": ("bytes", 1), "KiB": ("bytes", 1024), "MiB": ("bytes", 1024**2), "GiB": ("bytes", 1024**3),
    "mCPU": ("cpu", 1), "CPU": ("cpu", 1000),
    "count": ("count", 1), "%": ("percent", 1), "ratio": ("ratio", 1), "requests/s": ("rate", 1),
    "seconds": ("time", 1), "ms": ("time", 0.001),
}
_MAX_FINITE_BUDGET = 10**12


@dataclass(frozen=True)
class ParameterRule:
    path: str
    dimension: str
    unit: str
    minimum: float
    maximum: float
    zero_semantics: str = "literal"

    def __post_init__(self):
        if not self.path or self.dimension not in {"host_attribute", "platform_limit", "instance_spec", "performance_policy"}:
            raise ValueError("parameter path or dimension is invalid")
        if self.unit not in _UNIT_FACTORS:
            raise ValueError(f"unsupported unit: {self.unit}")
        if not isfinite(self.minimum) or not isfinite(self.maximum) or self.minimum > self.maximum:
            raise ValueError("parameter range must be finite and ordered")
        if self.zero_semantics not in {"literal", "forbidden", "disable"}:
            raise ValueError("unknown zero semantics")


@dataclass(frozen=True)
class ResolvedParameter:
    path: str
    dimension: str
    value: float
    unit: str
    mode: str
    budget_ref: str | None = None


def _convert(value: Any, source_unit: str, target_unit: str) -> float:
    if source_unit not in _UNIT_FACTORS or target_unit not in _UNIT_FACTORS:
        raise ValueError("unknown unit")
    src_dimension, src_factor = _UNIT_FACTORS[source_unit]
    dst_dimension, dst_factor = _UNIT_FACTORS[target_unit]
    if src_dimension != dst_dimension:
        raise ValueError("unit dimension mismatch")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError("parameter value must be a finite number")
    converted=Fraction(str(value))*Fraction(str(src_factor))/Fraction(str(dst_factor))
    return converted.numerator if converted.denominator==1 else float(converted)


def _validate_value(rule: ParameterRule, value: float) -> float:
    if not isfinite(value) or value < rule.minimum or value > rule.maximum:
        raise ValueError(f"value outside allowed range [{rule.minimum}, {rule.maximum}]")
    if value == 0 and rule.zero_semantics == "forbidden":
        raise ValueError("zero is forbidden for this parameter")
    return value


def resolve_parameter(rule: ParameterRule, *, mode: str, value: Mapping[str, Any] | None = None,
                      observed: Mapping[str, Any] | None = None,
                      budget: Mapping[str, Any] | None = None) -> ResolvedParameter:
    if mode == "preserve":
        if not observed or "value" not in observed or "unit" not in observed:
            raise ValueError("preserve requires a discovered value and unit")
        resolved = _validate_value(rule, _convert(observed["value"], observed["unit"], rule.unit))
        return ResolvedParameter(rule.path, rule.dimension, resolved, rule.unit, mode)
    if mode == "exact":
        if not value or "value" not in value or "unit" not in value:
            raise ValueError("exact requires a value and unit")
        resolved = _validate_value(rule, _convert(value["value"], value["unit"], rule.unit))
        return ResolvedParameter(rule.path, rule.dimension, resolved, rule.unit, mode)
    if mode == "relax_for_plan":
        required = {"minimum", "maximum", "step", "budget_ref"}
        if not budget or not required.issubset(budget):
            raise ValueError("relax_for_plan requires an explicit finite budget")
        low = _convert(budget["minimum"], budget.get("unit", rule.unit), rule.unit)
        high = _convert(budget["maximum"], budget.get("unit", rule.unit), rule.unit)
        step = _convert(budget["step"], budget.get("unit", rule.unit), rule.unit)
        if high > _MAX_FINITE_BUDGET or low < 0 or step <= 0 or high < low:
            raise ValueError("relaxation budget must be finite, nonnegative and ordered")
        if not str(budget["budget_ref"]).startswith("artifact://"):
            raise ValueError("budget_ref must identify a recorded finite budget")
        start = _convert(observed["value"], observed["unit"], rule.unit) if observed else low
        candidate = high
        # Resolve to the highest permitted planned value; the explicit budget, not an implicit 'unlimited', bounds it.
        candidate = low + int((candidate - low) // step) * step
        return ResolvedParameter(rule.path, rule.dimension, _validate_value(rule, candidate), rule.unit,
                                 mode, str(budget["budget_ref"]))
    raise ValueError("mode must be preserve, exact, or relax_for_plan")


__all__ = ["ParameterRule", "ResolvedParameter", "resolve_parameter"]
