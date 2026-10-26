"""Structured configuration loading and source-aware effective-value checks."""
from __future__ import annotations

import copy
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .declarations import _UniqueKeyLoader


@dataclass(frozen=True)
class ResolvedConfig:
    values: dict[str, Any]
    sources: dict[str, str]


@dataclass(frozen=True)
class ConfigValueEvidence:
    path: str
    expected: Any
    file_value: Any
    resolved_value: Any
    effective_value: Any
    source: str
    status: str
    evidence_level: str


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def load_config(path: str | Path, *, max_bytes: int = 1_048_576) -> dict[str, Any]:
    source = Path(path)
    with source.open('rb') as stream:payload=stream.read(max_bytes+1)
    if len(payload) > max_bytes:
        raise ValueError("configuration exceeds size limit")
    try:
        text = payload.decode("utf-8")
        suffix = source.suffix.lower()
        if suffix == ".toml":
            result = tomllib.loads(text)
        elif suffix in {".yaml", ".yml"}:
            result = yaml.load(text, Loader=_UniqueKeyLoader)
        elif suffix == ".json":
            result = json.loads(text, object_pairs_hook=_unique_pairs)
        else:
            raise ValueError("configuration format must be TOML, YAML or JSON")
    except (UnicodeDecodeError, yaml.YAMLError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid configuration: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("configuration root must be an object")
    return result


def _scalar(value: str, path: str):
    if any(word in path.lower() for word in ("key", "token", "secret", "password")):
        return value
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        return int(value)
    if re.fullmatch(r"-?(0|[1-9][0-9]*)\.[0-9]+", value):
        return float(value)
    return value


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    segments = path.split(".")
    if not path or any(not segment for segment in segments):
        raise ValueError("configuration override path is invalid")
    current = target
    for segment in segments[:-1]:
        child = current.setdefault(segment, {})
        if not isinstance(child, dict):
            raise ValueError(f"configuration override conflicts at {segment}")
        current = child
    current[segments[-1]] = value


def _flatten(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            result.update(_flatten(item, path))
        else:
            result[path] = item
    return result


def resolve_config(file_values: Mapping[str, Any], *, env: Mapping[str, str] | None = None,
                   cli: Mapping[str, Any] | None = None) -> ResolvedConfig:
    """Apply file < environment < CLI overrides while retaining provenance."""
    values = copy.deepcopy(dict(file_values))
    sources = {path: "file" for path in _flatten(values)}
    env_overrides: dict[str, str] = {}
    for name, value in (env or {}).items():
        if "__" not in name:
            continue
        path = name.lower().replace("__", ".")
        if path in env_overrides:
            raise ValueError(f"duplicate override path: {path}")
        env_overrides[path] = value
    for path, raw in env_overrides.items():
        _set_path(values, path, _scalar(raw, path))
        sources[path] = "environment"
    for path, value in (cli or {}).items():
        _set_path(values, path, value)
        sources[path] = "cli"
    return ResolvedConfig(values, sources)


def evaluate_config_value(path: str, *, expected: Any, file_value: Any,
                          resolved_value: Any, effective_value: Any) -> ConfigValueEvidence:
    if effective_value is not None:
        source = "runtime"
        status = "satisfied" if effective_value == expected else "mismatch"
        level = "runtime_effective"
    elif resolved_value is not None:
        source, status, level = "resolved_config", "unknown", "resolved_only"
    elif file_value is not None:
        source, status, level = "file", "unknown", "file_only"
    else:
        source, status, level = "unobserved", "unknown", "no_evidence"
    return ConfigValueEvidence(path, expected, file_value, resolved_value,
                               effective_value, source, status, level)


__all__ = ["ConfigValueEvidence", "ResolvedConfig", "evaluate_config_value", "load_config", "resolve_config"]
