"""Validate BCT-02 fixture metadata: provenance, classification, and hash binding.

Fixture expected values must never be produced by the algorithm under test; metadata
binds every golden file to its source, classification and content hash so a hand-edit
cannot silently change an oracle.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CLASSIFICATIONS = ('synthetic', 'captured')
REQUIRED = ('schema_version', 'classification', 'source', 'input_ref', 'sha256', 'oracle_independence')


def _resolve_input(path: Path, input_ref: str) -> Path:
    candidates = [Path(input_ref)]
    if not Path(input_ref).is_absolute():
        candidates.append(path.parent / input_ref)
        for parent in [path.resolve(), *path.resolve().parents]:
            if (parent / 'pyproject.toml').exists():
                candidates.append(parent / input_ref)
                break
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise ValueError(f'input_ref does not resolve: {input_ref}')


def validate_fixture_metadata(path) -> dict:
    """Return a normalized record, verifying provenance and the content hash."""
    path = Path(path)
    metadata = json.loads(path.read_text())
    missing = [key for key in REQUIRED if key not in metadata]
    if missing:
        raise ValueError(f'fixture metadata missing {missing}')
    if metadata['classification'] not in CLASSIFICATIONS:
        raise ValueError('classification must be synthetic or captured')
    if not metadata['source'] or not metadata['oracle_independence']:
        raise ValueError('fixture provenance and oracle independence are required')
    if metadata['classification'] == 'captured' and not metadata.get('deployment_version'):
        raise ValueError('captured fixture requires a deployment_version')
    target = _resolve_input(path, metadata['input_ref'])
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != metadata['sha256']:
        raise ValueError(f'sha256 does not match input_ref {target.name}')
    payload = json.loads(target.read_text())
    return {'schema_version': metadata['schema_version'], 'classification': metadata['classification'],
            'source': metadata['source'], 'input_ref': str(target), 'sha256_verified': True,
            'oracle_independence': metadata['oracle_independence'],
            'requirements': payload.get('requirements', [])}
