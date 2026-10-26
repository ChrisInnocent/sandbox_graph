# Lifecycle contracts

Status: **locally_verified** for the pure contracts and transition rules in
`sandbox_graph/domain/agent_lifecycle.py`. Persistent lifecycle services, worker
integration, actual resource disposition and provider/backend verification are
outside this implementation. These models do not establish production readiness.

The source requirements are ALD-02a/04 in
[the lifecycle design](../../openspec/changes/sandbox-evaluation-workflow/agent-lifecycle-design.md).

## Wire boundary

Every new model requires integer `schema_version=1`. Unknown fields, boolean
integer budgets, implicit string-to-number conversions, empty/whitespace-bearing
identifiers and non-finite numbers are rejected. Models are frozen. Nested
policies use the same strict boundary. Python inputs use tuples for tuple fields;
JSON arrays round-trip through `model_validate_json`.

References are opaque, nonempty identifiers. The service must resolve their
versions, prove run/group/work/node membership and validate authorization before
using them. A syntactically valid reference is not authorization or provenance.

| Model | Contract |
|---|---|
| `LifecycleKey` | `run_ref`, `scope_kind`, `scope_ref`, `node_id`, `lifecycle_id`, `lineage_budget_ref`; scope kind is run/group/work/node. |
| `RepairPolicy` | Required policy version, positive finite budgets, wall-clock deadline, explicitly named token units, typed backoff and safety allowance, and postcheck ref. `actions=()` grants no action. |
| `BackoffPolicy` | Positive finite `seconds`, with `unit='seconds'`. |
| `SafetyBudget` | Separate stop/query/cleanup limits, positive cost limit with explicit unit, wall-clock deadline at least as late as the business deadline. |
| `BudgetUsage` | Nonnegative integer usage snapshot. Zero usage grants no budget; reservation and lineage accounting belong to the service. |
| `LifecycleRecord` | Identity, graph/plan/contract/policy refs, phase/revision/owner epoch, source receipt, failure digest, absolute deadline, saved resume phase, stop intent, attempt/operation/isolation refs and event history. |
| `LifecycleEvent` | Required event ID, source, expected revision, nonempty evidence refs and observation time; optional bounded backoff time, saved operation kind/refs and isolation refs. |
| `RepairRequest` | Caller request ID, lifecycle/source receipt, expected revision, versioned action/input/proposal/authorization refs. Caller cannot provide attempt, owner, dispatch generation or remaining budget. |
| `ResourceDispositionReceipt` | Work/operation identity, stopped/admission/sampler facts, owned/residual resources, isolation domains, observation/expiry and producer/version. Expiry must follow observation. |
| `RepairOutcome` | Order/operation identity, effect certainty, independent execution/data/cleanup statuses, postcheck and raw refs. Successful execution does not imply a successful repair. |
| `RecoveryPlan` | Immutable ref; graph/contract refs and SHA-256 hashes; input hash, work revision, source attempt, entry node, revalidation/reuse/invalidation/resource proof sets and measurement generation. |
| `ContextPacket` | Lifecycle key, node supervisor, role/version refs, failure summary, facts/hypotheses, evidence scope/byte limit, usage snapshot and latest disposition receipt ref. Tool capabilities default to empty. |

`ContextPacket` rejects extra parent-conversation, unrelated-log and plaintext-secret
fields. Its service must still sanitize text and enforce evidence membership,
sharing permissions, byte limits and secret exclusion. `RecoveryPlan` must be
produced by the deterministic recovery compiler and checked again by the driver;
its schema does not authorize an agent-selected entry node.

`LifecycleRecord.node_result` keeps the existing `NodeResult` contract. Lifecycle
transitions preserve its execution, data, review, performance and cleanup facts.
There are no flat lifecycle fields that overwrite those statuses.

## Pure transition API

```python
transition_lifecycle(
    record: LifecycleRecord,
    event: LifecycleEvent,
    guards: TransitionGuards,
) -> LifecycleRecord
```

The returned record has the next revision and appended event. The input record is
unchanged. An exact replay of a saved event returns the same record, including
after terminal closure. A reused ID with different content raises
`InvalidLifecycleTransition`; an unmatched expected revision raises
`StaleLifecycleEvent`. All unlisted phase/event pairs are rejected.

`TransitionGuards` contains **service-verified facts**, never claims supplied by
the model, provider or external caller. Authority, capabilities, disposition,
budgets, proofs and lease validity default to false. `effects_in_flight` defaults
to true. The service must calculate these facts and persist the event/CAS update
inside the same fenced transaction as any budget reservation or dispatch.
`safety_deadline` has no implicit default authorization.

| Current phase | Event → next phase |
|---|---|
| draining | disposition_confirmed → diagnosing; effect_unknown → reconciling |
| reconciling | effect_known → saved resume; observation_due → reconciling; budget_exhausted → blocked or bounded safety disposition |
| diagnosing | known_action → remediation_pending; needs_code → candidate_building; needs_input → waiting_decision; no_safe_route → blocked |
| remediation_pending | claimed → remediating |
| remediating | postcheck_pass → reentering; postcheck_fail_with_budget → diagnosing/backoff; postcheck_fail_exhausted → blocked or bounded safety disposition; unknown → reconciling |
| backoff | timer_due → diagnosing |
| reentering | node_accepted → fulfilled; node_failed → draining |
| candidate_building | revision_frozen → candidate_verifying; provider_unknown → reconciling; budget_exhausted → blocked or bounded safety disposition |
| candidate_verifying | tests_fail → candidate_building; tests_pass → promotion_pending; environment_missing → waiting_dependency |
| promotion_pending | approved_and_ready → superseded; needs_authorization → waiting_decision; worker_missing → verified_waiting_worker; proof_invalid → candidate_verifying; nonruntime_candidate_accepted → diagnosing/blocked |
| verified_waiting_worker | ready → promotion_pending; proof_expired → candidate_verifying |
| waiting_decision / waiting_dependency | valid_decision / dependency_ready → saved resume |
| cancelling | disposition_closed → saved terminal intent; unknown → blocked with isolation evidence |
| blocked | authorized_resume → saved resume; close_partial → blocked_closed |

For every nonterminal phase: cancel initiates stopping, owner_lost preserves the
phase and attempts, irrecoverable_contract_error requires confirmed closed
disposition before failed. Deadline/budget events block directly only when no
effects are in flight and disposition is closed; otherwise they use finite
cancelling/reconciling safety work. Exhausted/expired safety allowance requires
isolation evidence and leaves the lifecycle blocked. Isolation refs are retained
through partial closure.

`fulfilled`, `superseded`, `blocked_closed`, `cancelled` and `failed` are terminal.
Waiting, backoff, reconciling and blocked remain open. Superseded requires a bound
successor, valid promotion proof/authorization, ready worker and closed old
resource disposition. Fulfilled requires a valid recovery plan, accepted new
attempt and closed disposition. Neither changes performance outcomes.

## Guard rules and stopping

New actions require a current work lease, valid authority and remaining budget;
registered repairs additionally require the registered action. They require known
effects and confirmed disposition. New work cannot start at/after the absolute
business deadline. Successful postchecks and candidate verification results can
be accepted on the last allowed attempt/revision without an extra unused unit.
Publication still requires an unexpired authorization and deadline.

A failed repair may return to diagnosis or a future backoff time within the
absolute deadline. At the repeated-failure limit, the service must establish a
different strategy/count or new evidence. A failed candidate can request another
revision when budget remains. Proof expiry returns to verification without
changing attempts or refreshing any budget.

Reconciliation stores site/provider operation identity and the original phase;
it does not authorize another operation. Waits preserve their original resume
phase. An event cannot select another resume destination. Observation requires
both finite observation and safety allowances, and either an unexpired business
deadline or the separately supplied safety deadline after a stop decision.

Stop events also require a complete operation kind/reference pair whenever
effects are in flight or unknown. An already bound operation is retained when an
event omits it; repeating its exact binding is permitted. Partial identities or a
different kind/reference pair are rejected. Safety cancellation and its unknown
result preserve the original recoverable phase and operation, so an authorized
resume can continue normal progress after effects and disposition are confirmed.

Stop reason and terminal intent are an atomic validated pair:

| Reason | Terminal intent |
|---|---|
| explicit_cancel | cancelled |
| deadline | blocked |
| budget_exhausted | blocked |

Explicit cancellation takes precedence over deadline, which takes precedence
over budget exhaustion. Cleanup called “cancel” does not turn budget exhaustion
into user cancellation. Blocking preserves the resume entrance. Explicitly
authorized resume needs known effects, closed disposition and adjusted remaining
budget, then clears the stop pair; it does not clear prior events or attempts.

## Verification boundary

The focused tests cover the literal ALD-02a edge oracle, all unlisted local edges,
global events, terminal refusal, guard rejection, event replay/CAS checks, final
allowed success, bounded stopping, quarantine preservation and independent result
statuses. These are pure contract tests for ALU-01/04/06/20 responsibilities.

Repository-level cross-scope validation, persistent event uniqueness, work leases,
atomic reservations and lineage accounting, current disposition lookup,
postcheck execution, deterministic recovery compilation, provider reconciliation
and actual closure remain service/runtime responsibilities. No backend, adapter
or production gate is established by these tests.
