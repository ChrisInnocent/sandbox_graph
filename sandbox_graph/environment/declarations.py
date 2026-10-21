"""Load, resolve and authenticate environment declarations and artifact refs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from ..domain.models import EvidenceRef
from ..artifacts.store import FileArtifactStore
from .contracts import EnvironmentDeclaration


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def load_declaration(path: str | Path) -> dict[str, Any]:
    """Read JSON/YAML as data; no tags, duplicate keys or executable values."""
    source = Path(path)
    raw = source.read_bytes()
    if len(raw) > 1_048_576:
        raise ValueError("environment declaration exceeds 1 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("environment declaration must be UTF-8") from exc
    try:
        if source.suffix.lower() == ".json":
            value = json.loads(text, object_pairs_hook=_unique_json_object)
        elif source.suffix.lower() in {".yaml", ".yml"}:
            value = yaml.load(text, Loader=_UniqueKeyLoader)
        else:
            raise ValueError("environment declaration must use .json, .yaml or .yml")
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid environment declaration: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("environment declaration root must be an object")
    return value


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


class DeclarationResolution:
    def __init__(self, declaration: EnvironmentDeclaration, discovered_fields: tuple[str, ...]):
        self.declaration = declaration
        self.discovered_fields = discovered_fields


def resolve_declaration(
    declaration: EnvironmentDeclaration | Mapping[str, Any],
    discovered: Mapping[str, Mapping[str, Any]],
) -> DeclarationResolution:
    """Fill only absent explicitly discoverable node fields; declared input wins."""
    raw = declaration.model_dump(mode="python") if isinstance(declaration, EnvironmentDeclaration) else dict(declaration)
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) and not isinstance(nodes, tuple):
        raise ValueError("environment declaration requires a nodes list")
    by_id = {node.get("node_id"): dict(node) for node in nodes if isinstance(node, dict)}
    if len(by_id) != len(nodes):
        raise ValueError("node IDs must be present and unique before fact discovery")
    accepted = {"ssh_port", "runtime_namespace", "paths"}
    filled: list[str] = []
    for node_id, facts in discovered.items():
        if node_id not in by_id:
            continue
        for key, value in facts.items():
            if key not in accepted:
                continue
            if key not in by_id[node_id] or by_id[node_id][key] is None:
                by_id[node_id][key] = value
                filled.append(f"{node_id}.{key}")
    raw["nodes"] = [by_id.get(node.get("node_id"), dict(node)) for node in nodes]
    resolved = EnvironmentDeclaration.model_validate(raw)
    return DeclarationResolution(resolved, tuple(sorted(filled)))


def validate_declaration(
    raw: Mapping[str, Any], discovered: Mapping[str, Mapping[str, Any]] | None = None
) -> DeclarationResolution:
    """Validate a partially supplied declaration after merging probe facts."""
    return resolve_declaration(raw, discovered or {})


def verify_artifact_reference(
    ref: EvidenceRef, store: FileArtifactStore, *, expected_scope: str
) -> EvidenceRef:
    if ref.scope != expected_scope:
        raise PermissionError("artifact reference scope mismatch")
    store.verify(ref)
    return ref


__all__ = [
    "DeclarationResolution",
    "load_declaration",
    "resolve_declaration",
    "validate_declaration",
    "verify_artifact_reference",
]
