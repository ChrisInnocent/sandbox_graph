"""Immutable evolution wire contracts; no I/O, trust lookup or authorization.

Hashes bind declared inputs, not their truth. Services must resolve snapshots,
verify producer identities, classify impact and check authorization under fence.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from sandbox_graph.domain.agent_lifecycle import (
    Count, Digest, Instant, LifecycleKey, LifecycleModel, PositiveInt,
)


Ref = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512, pattern=r'^\S+$')]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=16384)]
Path = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
Commit = Annotated[str, StringConstraints(strict=True, pattern=r'^(?:[a-f0-9]{40}|[a-f0-9]{64})$')]
Risk = Literal['R0', 'R1', 'R2', 'R3']
GateId = Literal['G0', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6.pre', 'G6.post']
Refs = Annotated[tuple[Ref, ...], Field(max_length=1024)]
NonemptyRefs = Annotated[tuple[Ref, ...], Field(min_length=1, max_length=1024)]
Cases = Annotated[tuple[Ref, ...], Field(max_length=10000)]
Paths = Annotated[tuple[Path, ...], Field(min_length=1, max_length=1024)]
Statements = Annotated[tuple[Text, ...], Field(max_length=1024)]
TARGET_FIELDS = (
    'target_plan_ref', 'target_plan_hash', 'target_contract_ref',
    'target_contract_hash', 'target_auth_ref', 'target_auth_hash', 'target_auth_revision',
)


def _digest(kind: str, values: dict) -> str:
    wire = json.dumps({'kind': kind, **values}, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(wire.encode('utf-8')).hexdigest()


def _unique(values: tuple, label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f'{label} must be unique')


def _paths(values: tuple[str, ...]) -> tuple[str, ...]:
    for path in values:
        if ('\\' in path or '\x00' in path or path.startswith('/')
                or any(part in ('', '.', '..') for part in path.split('/'))):
            raise ValueError('paths must be canonical relative POSIX paths')
    _unique(values, 'paths')
    return values


class EvolutionModel(LifecycleModel):
    """Version is mandatory; all fields and nested wire contracts are frozen."""


class TargetBinding(EvolutionModel):
    """Frozen successor-run inputs, independent of publication approval."""
    target_plan_ref: Ref
    target_plan_hash: Digest
    target_contract_ref: Ref
    target_contract_hash: Digest
    target_auth_ref: Ref
    target_auth_hash: Digest
    target_auth_revision: Count


class ChangeProposal(EvolutionModel):
    proposal_id: Ref
    source_key: LifecycleKey
    base_code_ref: Ref
    base_graph_ref: Ref
    failure_evidence_refs: NonemptyRefs
    facts: Annotated[Statements, Field(min_length=1)]
    hypotheses: Statements
    target_behavior: Text
    allowed_paths: Paths
    node_scope: NonemptyRefs
    contract_scope: Refs
    acceptance_refs: NonemptyRefs
    scope_auth: Ref

    @field_validator('allowed_paths')
    @classmethod
    def canonical_paths(cls, values):
        return _paths(values)

    @field_validator('target_behavior')
    @classmethod
    def meaningful_target(cls, value):
        if not value.strip():
            raise ValueError('target behavior must contain text')
        return value

    @field_validator('facts', 'hypotheses')
    @classmethod
    def meaningful_statements(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError('statements must contain text')
        return values

    @model_validator(mode='after')
    def unique_scope(self):
        for field in ('failure_evidence_refs', 'node_scope', 'contract_scope', 'acceptance_refs'):
            _unique(getattr(self, field), field)
        return self


class CandidateRevision(TargetBinding):
    candidate_ref: Ref
    proposal_id: Ref
    revision: PositiveInt
    parent_revision: PositiveInt | None
    base_commit: Commit
    tree_hash: Digest
    patch_hash: Digest
    graph_ref: Ref
    graph_hash: Digest
    lock_hash: Digest
    changed_paths: Paths
    risk: Risk
    workspace_ref: Ref
    budget_ref: Ref

    @field_validator('changed_paths')
    @classmethod
    def canonical_paths(cls, values):
        return _paths(values)

    @model_validator(mode='after')
    def sequential_parent(self):
        expected = None if self.revision == 1 else self.revision - 1
        if self.parent_revision != expected:
            raise ValueError('parent_revision must identify the preceding revision')
        return self

    def content_identity(self) -> dict:
        """Return a detached canonical payload, excluding ledger provenance.

        A service still verifies parent lineage, registered inputs, actual tree
        contents, path/symlink confinement and the trusted risk classification.
        """
        fields = ('schema_version', 'base_commit', 'tree_hash', 'patch_hash',
                  'graph_ref', 'graph_hash', 'lock_hash', 'risk', *TARGET_FIELDS)
        return {**{name: getattr(self, name) for name in fields},
                'changed_paths': tuple(sorted(self.changed_paths))}

    @property
    def candidate_hash(self) -> str:
        return _digest('candidate-content', self.content_identity())


class GateCaseResult(EvolutionModel):
    case_id: Ref
    status: Literal['pass', 'fail', 'blocked', 'skipped', 'xfail']
    log_hash: Digest


class GateResult(EvolutionModel):
    gate_id: GateId
    status: Literal['pass', 'fail', 'blocked', 'not_applicable']
    case_ids: Cases
    log_hash: Digest
    rule_ref: Ref | None = None
    input_diff_hash: Digest | None = None
    reason: Text | None = None

    @model_validator(mode='after')
    def explicit_nonapplicability(self):
        _unique(self.case_ids, 'case_ids')
        if self.status == 'not_applicable':
            if self.rule_ref is None or self.input_diff_hash is None or not (self.reason or '').strip():
                raise ValueError('not_applicable requires rule, diff hash and reason')
        return self


class GateManifest(TargetBinding):
    manifest_ref: Ref
    candidate_ref: Ref
    candidate_hash: Digest
    activation_ref: Ref | None = None
    run_ref: Ref | None = None
    trusted_suite_hash: Digest
    policy_hash: Digest
    environment_ref: Ref
    environment_hash: Digest
    runner_ref: Ref
    runner_version: Ref
    runner_bundle_hash: Digest
    required_gates: Annotated[tuple[GateId, ...], Field(min_length=1, max_length=8)]
    expected_case_ids: Cases
    actual_case_ids: Cases
    results: Annotated[tuple[GateResult, ...], Field(max_length=8)]
    case_results: Annotated[tuple[GateCaseResult, ...], Field(max_length=10000)]
    log_hash: Digest
    producer_identity: Ref
    producer_version: Ref
    observed_at: Instant
    expires_at: Instant
    clock: Literal['wall']

    @model_validator(mode='after')
    def unambiguous_observation(self):
        if self.expires_at <= self.observed_at:
            raise ValueError('gate proof must expire after observation')
        has_post = ('G6.post' in self.required_gates
                    or any(result.gate_id == 'G6.post' for result in self.results))
        if has_post:
            if self.activation_ref is None or self.run_ref is None:
                raise ValueError('G6.post requires activation_ref and run_ref')
        elif self.activation_ref is not None or self.run_ref is not None:
            raise ValueError('only G6.post may bind activation_ref and run_ref')
        for field in ('required_gates', 'expected_case_ids', 'actual_case_ids'):
            _unique(getattr(self, field), field)
        _unique(tuple(result.gate_id for result in self.results), 'gate results')
        _unique(tuple(result.case_id for result in self.case_results), 'case results')
        actual = set(self.actual_case_ids)
        if any(result.case_id not in actual for result in self.case_results):
            raise ValueError('case results must belong to actual_case_ids')
        if any(not set(result.case_ids) <= actual for result in self.results):
            raise ValueError('gate cases must belong to actual_case_ids')
        return self

    def matches_candidate(self, candidate: CandidateRevision) -> bool:
        """Content/target equality only: this never authenticates an attestation.

        Same-content revisions may reuse evidence after service compatibility,
        expiry, suite/policy/environment, producer and authorization checks.
        """
        return (self.candidate_hash == candidate.candidate_hash
                and all(getattr(self, field) == getattr(candidate, field) for field in TARGET_FIELDS))

    @property
    def input_hash(self) -> str:
        fields = ('schema_version', 'candidate_hash', *TARGET_FIELDS, 'trusted_suite_hash',
                  'policy_hash', 'environment_ref', 'environment_hash', 'runner_ref',
                  'runner_version', 'runner_bundle_hash')
        values = {name: getattr(self, name) for name in fields}
        values.update(required_gates=tuple(sorted(self.required_gates)),
                      expected_case_ids=tuple(sorted(self.expected_case_ids)))
        if self.activation_ref is not None:
            values.update(activation_ref=self.activation_ref, run_ref=self.run_ref)
        return _digest('gate-input', values)

    @property
    def manifest_hash(self) -> str:
        """Integrity digest of the full record, including results and provenance."""
        return _digest('gate-manifest', self.model_dump(mode='json'))


class ReviewFinding(EvolutionModel):
    finding_ref: Ref
    severity: Literal['info', 'warning', 'blocking']
    summary: Text
    evidence_refs: NonemptyRefs


class ReviewRecord(TargetBinding):
    review_ref: Ref
    candidate_ref: Ref
    candidate_hash: Digest
    gate_manifest_ref: Ref
    gate_manifest_hash: Digest
    policy_hash: Digest
    coder_identity: Ref
    reviewer_identity: Ref
    reviewer_kind: Literal['model', 'human', 'deterministic']
    producer_version: Ref
    result: Literal['approved', 'changes_requested', 'blocked']
    findings: Annotated[tuple[ReviewFinding, ...], Field(max_length=1024)]
    evidence_refs: NonemptyRefs
    observed_at: Instant
    expires_at: Instant
    clock: Literal['wall']

    @model_validator(mode='after')
    def independent_timed_review(self):
        if self.coder_identity == self.reviewer_identity:
            raise ValueError('reviewer must be distinct from coder')
        if self.expires_at <= self.observed_at:
            raise ValueError('review must expire after observation')
        _unique(tuple(finding.finding_ref for finding in self.findings), 'findings')
        _unique(self.evidence_refs, 'evidence_refs')
        return self


class PromotionRecord(TargetBinding):
    """A fenced publication request, never proof that activation was authorized."""
    request_id: Ref
    candidate_ref: Ref
    candidate_hash: Digest
    gate_manifest_ref: Ref
    gate_manifest_hash: Digest
    review_ref: Ref
    review_hash: Digest
    authorization_ref: Ref
    authorization_hash: Digest
    authorization_revision: Count
    worker_bundle_ref: Ref
    worker_bundle_hash: Digest
    expected_predecessor_revision: Count
    created_at: Instant
    expires_at: Instant
    clock: Literal['wall']

    @model_validator(mode='after')
    def finite_publication_window(self):
        if self.expires_at <= self.created_at:
            raise ValueError('promotion must expire after creation')
        return self


class RollbackRecord(PromotionRecord):
    """Explicitly authorized fresh activation of a previously verified bundle."""
    failed_run_ref: Ref
    reason: Text
