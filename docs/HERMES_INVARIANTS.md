# Hermes / HealBite Engineering Invariants

Status: normative engineering contract
Verified against canonical main: `fed59bd3a5b547c0f4ddfceffa59ee7787950844`

## How to use this document

An invariant is a condition that must remain true across implementation and
operation. Every safety-critical task must name the affected invariants, their
evidence and the stop condition. Detailed operational procedures remain in the
linked skills; this file is the compact cross-system index.

If an invariant cannot be proven, report `UNKNOWN`, `INCONCLUSIVE`, `BLOCKED`
or `FAIL` as appropriate. Do not convert absence of evidence into PASS.

## Agent and architecture invariants

### A1. Stable conversation prefix

**Invariant:** Past conversation context, toolsets and the system prompt remain
stable during a conversation except through the explicit compression/cache
invalidation contracts.

**Why:** Mid-session mutation breaks provider prompt caching, increases cost
and can violate message-role alternation.

**Evidence:** Focused prompt-cache and conversation-loop tests; no same-role
message pairs; no unplanned toolset/system-prompt rebuild in the diff.

**Authority:** `AGENTS.md`, `run_agent.py`, `agent/conversation_loop.py`.

### A2. Narrow core, gated edges

**Invariant:** Add capability at the least permanent surface that works:
existing code, CLI+skill, service-gated tool, plugin or MCP before a new core
model tool.

**Why:** Every core tool schema is paid for on every relevant model call and
widens the trust surface.

**Evidence:** Source-map/discovery notes identify the reused extension point;
new tool schemas have an explicit necessity and availability gate.

**Authority:** `AGENTS.md`, `tools/registry.py`, `model_tools.py`, `toolsets.py`.

### A3. Untrusted model output

**Invariant:** LLM and Vision output is parsed and locally validated before it
can drive persistence, authorization, tool execution or user-visible claims.

**Why:** Model output is probabilistic and can be malformed, incomplete or
outside the requested scope.

**Evidence:** Strict parser/schema tests, rejection tests and proof that failure
paths perform no unauthorized write/send.

**Authority:** feature validators under `gateway/healbite_*`, tool guardrails
and their tests.

## Identity and data invariants

### D1. User, chat and household isolation

**Invariant:** Every read, write, callback and memory operation is scoped to the
authorized user and, where applicable, chat/thread/session and household
membership. A raw transport or domain identifier is not authorization.

**Why:** Mixed scopes can disclose or mutate another user's durable data.

**Evidence:** Scoped SQL predicates, authoritative membership resolution,
owner/session-bound callback validation and cross-user/cross-household denial
tests. Reports contain classifications, not identifiers.

**Authority:** `skills/memory/SKILL.md`, `skills/telegram/SKILL.md`,
`gateway/healbite_households.py`, `gateway/session.py`.

### D2. SQLite is durable truth for HealBite

**Invariant:** HealBite product data and Memory OS facts remain authoritative in
SQLite where their current contracts apply. Qdrant, FTS, views and generated
drafts are derived.

**Why:** Derived indexes can be unavailable, stale, duplicated or incomplete
without changing the durable record.

**Evidence:** Reads rehydrate Qdrant hits from scoped SQLite, SQLite fallback
works and Qdrant-only operations preserve SQLite fingerprints.

**Authority:** `skills/memory/SKILL.md`,
`gateway/platforms/healbite_memory_bridge.py`.

### D3. Qdrant mutations are explicit and scoped

**Invariant:** Qdrant defaults to health/count metadata and dry-run behavior.
Upsert, replacement, cutover, deletion or cleanup requires explicit scoped
authority and an appropriate rollback plan.

**Why:** The live collection is shared derived state and the current rebuild is
upsert-only; counts alone cannot identify stale or missing points.

**Evidence:** Pinned collection/vector/DB/user scope, aggregate dry-run and
post-state evidence, and explicit proof of which mutation classes did or did
not occur.

**Authority:** `skills/memory/SKILL.md`,
`scripts/rebuild_qdrant_memory_index.py`.

### D4. Transactional state changes fail closed

**Invariant:** Multi-row or multi-table product changes commit atomically or
leave no partial durable state. For Memory OS, the canonical fact mutation and
minimal derived-vector intent share one SQLite transaction; the external
Qdrant mutation remains asynchronous and recoverable.

**Why:** A partial save can violate domain constraints. A successful SQLite
write does not prove immediate Qdrant convergence, but it must prove that
restart-safe synchronization intent exists.

**Evidence:** Rollback/error injection tests, foreign-key checks, atomic
fact/outbox tests and durable reconciliation evidence for external indexes.

**Authority:** HealBite stores/services, `skills/memory/SKILL.md`, focused tests.

### D5. Memory vector convergence is revisioned and owner-scoped

**Invariant:** Every applicable Memory OS insert, update or delete has a durable
owner-bound vector operation. Duplicate delivery is idempotent; a stale upsert
or delete cannot win over a newer fact generation/revision; failed work remains
privacy-safe `PENDING`, `DEGRADED` or `BLOCKED` state.

**Why:** An ephemeral Future, accepted-but-unconfirmed request or timestamp-only
ordering cannot prove eventual derived-state correctness across crash/restart.

**Evidence:** Disposable SQLite migration tests, failure injection before/after
commit and acknowledgement, restart/replay, delete/recreate, owner-mismatch,
bounded-batch and aggregate-observability tests.

**Authority:** `docs/adr/ADR-0079-durable-memory-vector-convergence.md`,
`gateway/memory/convergence.py`, `skills/memory/SKILL.md`.

## Telegram invariants

### T1. One polling owner per token

**Invariant:** Exactly one active `getUpdates` consumer owns a Telegram bot
token, across profiles, containers and hosts.

**Why:** Competing consumers create Telegram conflicts, retry loops and gaps in
update ownership.

**Evidence:** Token-scoped lock, sanitized runtime inventory, one updater and no
unresolved polling conflict.

**Authority:** `skills/telegram/SKILL.md`, `gateway/platforms/telegram.py`,
`gateway/status.py`.

### T2. Diagnostics are no-send by default

**Invariant:** Health checks use updater state, `getMe` or synthetic events
without sending/editing a live user message unless a task explicitly authorizes
a smoke.

**Why:** A diagnostic send mutates a user conversation; a read-only probe also
must not be overstated as end-to-end delivery proof.

**Evidence:** Zero send/edit calls, no outbound messages and synthetic routing
tests; live-smoke evidence is separately classified.

**Authority:** `skills/telegram/SKILL.md`.

## Source and release invariants

### R1. Canonical exact source

**Invariant:** Repository work begins from the trusted canonical remote in a
clean isolated worktree whose `HEAD` equals the resolved exact `main` SHA.

**Why:** A branch name, stale remote or dirty checkout cannot bind reviewed
source to the resulting change or image.

**Evidence:** Canonical remote URL, remote main SHA, HEAD equality, branch and
empty porcelain status.

**Authority:** `AGENTS.md`, `deploy/hermes-production.json`,
`skills/deploy/SKILL.md`.

### R2. Exact immutable image provenance

**Invariant:** Build from the verified exact Git tree and release/deploy only an
immutable digest or local image ID whose single OCI revision equals the exact
source SHA.

**Why:** Raw worktrees can contain ignored inputs and mutable tags can resolve
to unreviewed bytes.

**Evidence:** Exact-tree manifest, CI binding, registry/image receipt, immutable
digest/ID and exact `org.opencontainers.image.revision`.

**Authority:** `.github/workflows/healbite-exact-main-ghcr.yml`,
`scripts/build_verified_playwright_image.py`, `skills/deploy/SKILL.md`.

### R3. Complete image-secret coverage

**Invariant:** A release image passes the canonical scan across metadata,
history, every recoverable layer and the final filesystem with zero findings or
only exact evidence-bound approved exceptions.

**Why:** Source scans miss build/dependency artifacts, while final-filesystem
scans miss secrets deleted in later layers.

**Evidence:** Sanitized receipt bound to immutable image identity, OCI revision,
ordered layers and scan-policy hash; all finding counts zero after policy.

**Authority:** `scripts/hermes_image_secret_scan.py`,
`deploy/hermes-image-secret-exceptions.json`, `skills/deploy/SKILL.md`.

### R4. CI is necessary, not deployment authority

**Invariant:** Required exact-head CI must pass, but it never authorizes a
production pull, migration, restart, feature change or deploy by itself.

**Why:** CI proves a source/build contract; production mutation needs current
runtime evidence, operator authority and rollback readiness.

**Evidence:** Exact-SHA CI results plus a separate operation-specific production
authority package when production execution is requested.

**Authority:** `deploy/hermes-production.json`, `skills/deploy/SKILL.md`.

## Migration and backup invariants

### M1. Quiescent database for baseline operations

**Invariant:** Active writers equal zero before a migration baseline, backup or
publication, and the required leases remain held through the protected window.

**Why:** Concurrent writes can move the database/WAL between observations and
invalidate both the migration input and rollback point.

**Evidence:** Writer/process classification, lease identity, pinned DB
device/inode and `active_writers=0` before the first mutation.

**Authority:** `skills/deploy/SKILL.md`,
`scripts/hermes_production_staged_migrate.py`.

### M2. Fresh verified SQLite backup

**Invariant:** Before SQLite mutation, create a fresh backup from the exact live
DB via the SQLite backup API or approved online equivalent and prove isolated
restore integrity.

**Why:** A plain copy with writers or an old backup is not a coherent immediate
rollback state.

**Evidence:** Source identity, timestamp, backup SHA-256, integrity/FK results
and successful isolated restore.

**Authority:** `skills/deploy/SKILL.md`, `skills/memory/SKILL.md`.

### M3. Integrity, foreign keys and schema compatibility

**Invariant:** SQLite integrity is `ok`, foreign-key violations are zero and the
post-migration schema is compatible with both the candidate and any automatic
rollback image.

**Why:** Container health cannot repair corruption, and an old image may be
unsafe after a schema-breaking migration.

**Evidence:** Pre/post pragmas, schema/user-version fingerprints, idempotent
rehearsal and candidate/rollback compatibility tests.

**Authority:** `skills/deploy/SKILL.md`, migration contract tests.

### M4. Effective migration scope equals authority

**Invariant:** The full canonical component registry remains intact, while the
ordered expected mutation subset must exactly equal the read-only derived
effective subset at planning and immediately before DDL.

**Why:** Registry membership must not silently broaden operator permission, and
schema drift must not expand real DDL after review.

**Evidence:** Bound approval/policy/plan/final-authority artifacts, per-component
schema classification and repeated exact expected/effective equality.

**Authority:** `scripts/hermes_production_staged_migrate.py`,
`scripts/hermes_release_authority.py`, `skills/deploy/SKILL.md`.

### M5. Rollback semantics match durable state

**Invariant:** Automatic image rollback is allowed only when the previous image
is available, provenance/identity-bound and compatible with the post-operation
schema. Database restore is a distinct, explicit operation.

**Why:** Recreating an old image after incompatible DDL can worsen damage;
silently restoring a DB discards user writes after the recovery point.

**Evidence:** Rollback image identity and rehearsal, schema compatibility,
explicit outcome (`PASS`, `ROLLED_BACK`, `FAIL`) and separate DB-restore
authority if needed.

**Authority:** `skills/deploy/SKILL.md`, deployment/migration scripts.

## Secret invariants

### S1. Secrets never enter repository or evidence

**Invariant:** Tokens, API keys, `.env` contents, private identifiers and raw
production payloads never enter Git, logs, command arguments, PR text or shared
evidence.

**Why:** Redaction after emission cannot reliably revoke copied bearer data.

**Evidence:** Repository/staged secret scan, sanitized fixed-schema receipts,
presence/fingerprint classifications only and review of changed artifacts.

**Authority:** `AGENTS.md`, `RUNBOOK_CODING_LOOP.md`, `scripts/secret_check.sh`.

### S2. Production secret source is closed and protected

**Invariant:** Production uses the exact approved secret source, closed variable
set, required ownership/mode and no symlink/ambient fallback; value changes need
explicit authority.

**Why:** An alternate or permissive source can silently substitute, omit or
expose credentials.

**Evidence:** Path/inode/type/owner/mode validation, duplicate/unknown-key
rejection and protected-value fingerprints without outputting values.

**Authority:** `deploy/hermes-production.json`,
`scripts/hermes_production_deploy.py`, `skills/deploy/SKILL.md`.

### S3. Technical gates cannot be waived

**Invariant:** Every required technical gate is explicit PASS before mutation.
Governance warnings are classified separately, but urgency or preference cannot
override a failed, missing or unknown technical gate.

**Why:** Fail-closed gates are the proof that source, artifact, data and rollback
assumptions are true; a human preference is not substitute evidence.

**Evidence:** Gate matrix retains every result and blocks on `FAIL`, `UNKNOWN`,
`INCONCLUSIVE` or absence.

**Authority:** `skills/deploy/SKILL.md`.

## AI behaviour and LLM Ops invariants

### AI1 (INV-AI-V2-001). Code PASS is not production release eligibility

**Invariant:** Code PASS alone does not prove production release eligibility.

**Why:** Code tests do not prove agent authority, required behaviour, security,
cost limits, current production readiness, or rollback safety.

**Evidence:** Separate gate matrix with task-required behaviour, security, cost,
and production-readiness statuses.

**Authority:** `docs/AGENT_RELEASE_GATES.md`.

### AI2 (INV-AI-V2-002). Behaviour evidence is independent

**Invariant:** Required behavioural evidence may not be inferred from code
tests.

**Why:** A command can succeed while violating scope, authority, or the stop
boundary.

**Evidence:** Deterministic behaviour cases bound to the task and exact source.

**Authority:** `docs/AGENT_BEHAVIOUR_CONTRACT.md`,
`docs/BEHAVIOUR_EVALS.md`.

### AI3 (INV-AI-V2-003). Missing evidence never becomes PASS

**Invariant:** `UNKNOWN`, `NOT_RUN`, and `INCONCLUSIVE` required evidence never
becomes PASS.

**Why:** Aggregating absence into success silently bypasses fail-closed gates.

**Evidence:** Gate aggregation tests for missing and ambiguous evidence.

**Authority:** `docs/AGENT_RELEASE_GATES.md`.

### AI4 (INV-AI-V2-004). Model choice does not expand authority

**Invariant:** A selected or recommended model never expands task authority.

**Why:** Capability is not permission to access secrets, mutate production, or
cross the task's stop boundary.

**Evidence:** Model-policy receipt plus unchanged allowed/forbidden effect
classes across selection and substitution.

**Authority:** `docs/LLM_OPS_POLICY.md`.

### AI5 (INV-AI-V2-005). Self-improvement is candidate-only

**Invariant:** An agent-generated improvement remains a candidate until the
repository lifecycle passes.

**Why:** Direct self-modification would bypass eval, review, CI, and activation
authority.

**Evidence:** Candidate PR, required evals, exact-head CI, review, and merge
record before any separately authorized activation.

**Authority:** `docs/SKILL_LOOP_GRAPH_LIFECYCLE.md`.

### AI6 (INV-AI-V2-006). Critical behaviour is not solely LLM-judged

**Invariant:** Critical behaviour decisions cannot rely solely on LLM-as-judge.

**Why:** A probabilistic or unavailable judge cannot be the only authority for
security and production release outcomes.

**Evidence:** Human-reviewed expected outcomes and deterministic assertions;
LLM-as-judge classified as supplemental only.

**Authority:** `docs/BEHAVIOUR_EVALS.md`.

### AI7 (INV-AI-V2-007). Eval fixtures are sanitized

**Invariant:** Behaviour/eval fixtures contain no secrets or private production
data.

**Why:** Reproducible test evidence must not become a durable disclosure path.

**Evidence:** Fixed-schema fixtures, secret scan, and review showing only
sanitized classifications and synthetic or approved data.

**Authority:** `docs/BEHAVIOUR_EVALS.md`, `scripts/secret_check.sh`.

## Workspace and execution plane invariants

### W1 (INV-WS-V4-001). Canonical checkout is never an agent workspace

**Invariant:** The canonical checkout (`/root/hermes_workspace/hermes` or the
primary repository root) is never assigned or used as an agent execution workspace.

**Why:** Direct mutation of the canonical working tree destroys provenance,
bypasses change review, and risks production drift.

**Evidence:** `WorkspaceSecurityError(CANONICAL_CHECKOUT_COLLISION)` raised on
any attempt to register, create, or resolve an execution workspace to the canonical root.

**Authority:** `ai_engineering/workspaces/workspace_manager.py`,
`ai_engineering/workspaces/worktree_manager.py`.

### W2 (INV-WS-V4-002). Single authoritative workspace per AgentRun

**Invariant:** Exactly one authoritative isolated workspace is bound to a given
`AgentRun` or `WorktreeLease`.

**Why:** Concurrent or overlapping execution across unisolated runs causes
cross-agent state contamination.

**Evidence:** `WorkspaceIdentity` and `WorktreeLease` validation enforcing
`WORKTREE_IDENTITY_MISMATCH` on foreign caller access.

**Authority:** `ai_engineering/workspaces/workspace_contracts.py`.

### W3 (INV-WS-V4-003). Strict workspace path containment

**Invariant:** All requested write and resolution paths must strictly resolve
within the authoritative workspace root.

**Why:** Relative traversal (`../`), external absolute paths, canonical
repository paths, and symlink escapes could overwrite external files.

**Evidence:** `validate_workspace_path` enforcing `WORKSPACE_PATH_ESCAPE` on path escapes.

**Authority:** `ai_engineering/workspaces/workspace_manager.py`.

### W4 (INV-WS-V4-004). Proven base SHA validation

**Invariant:** Every execution workspace must start from and verify its exact
expected base commit SHA.

**Why:** Starting from an unverified or drifted base creates silent merge
conflicts and invalidates behaviour assertions.

**Evidence:** `validate_worktree_base_sha` raising `WORKTREE_BASE_SHA_MISMATCH`
if git HEAD differs from `base_sha`.

**Authority:** `ai_engineering/workspaces/worktree_manager.py`.

### W5 (INV-WS-V4-005). Dirty worktree reuse protection

**Invariant:** An unexpected dirty worktree is never silently reused or
automatically cleaned with destructive git commands (`reset --hard`, `clean -fd`, `stash`).

**Why:** Automatic destructive cleaning can destroy uncommitted human or agent
work and hide state anomalies.

**Evidence:** `validate_clean_worktree` raising `WORKTREE_DIRTY_REUSE`.

**Authority:** `ai_engineering/workspaces/worktree_manager.py`.

### W6 (INV-WS-V4-006). Quarantined workspace terminality

**Invariant:** A `QUARANTINED` or `RELEASED` workspace lease is terminal and
cannot be reactivated or reused for active execution.

**Why:** Quarantined workspaces contain suspected corrupted or malicious state
that must not re-enter the active execution plane.

**Evidence:** `LeaseState` state machine rejecting transitions from
`QUARANTINED` or `RELEASED` with `LEASE_TRANSITION_INVALID`.

**Authority:** `ai_engineering/workspaces/workspace_contracts.py`.

### W7 (INV-WS-V4-007). Workspace isolation does not expand authorization

**Invariant:** Creating or leasing an isolated workspace grants only filesystem
containment and does not grant production authorization, data mutation, secret
access, or external communication rights.

**Why:** Sandboxing and workspace allocation are orthogonal to authority and
execution boundaries.

**Evidence:** Non-interference checks and immutable `AuthorityBoundary` preservation.

**Authority:** `ai_engineering/workspaces/workspace_contracts.py`,
`ai_engineering/contracts.py`.

## Agent run identity, execution epoch, and stale event fencing invariants

### E1 (INV-EXEC-V4-001). Immutable agent run identity and collision protection

**Invariant:** `run_id` uniquely identifies an immutable `AgentRunIdentity`.
Registering an existing `run_id` with differing identity attributes is strictly
rejected with `RUN_IDENTITY_COLLISION`.

**Why:** Duplicate or mutated run identities cause non-deterministic execution
attribution and cross-run state pollution.

**Evidence:** `ActiveRunRegistry.register_run` enforcing `RUN_IDENTITY_COLLISION`
on identity field mismatches.

**Authority:** `ai_engineering/execution/run_contracts.py`,
`ai_engineering/execution/run_registry.py`.

### E2 (INV-EXEC-V4-002). Execution epoch lifecycle fencing

**Invariant:** `execution_epoch` strictly increments across lifecycle
generations for an execution slot. Inbound events with mismatched or older
epochs are rejected with `STALE_RUN_MUTATION`.

**Why:** Network retries, asynchronous message delivery, and delayed worker
callbacks can deliver stale telemetry that corrupts newer execution epochs.

**Evidence:** `ActiveRunRegistry.process_event` raising `STALE_RUN_MUTATION` on
epoch divergence.

**Authority:** `ai_engineering/execution/run_registry.py`.

### E3 (INV-EXEC-V4-003). Stale run event fencing

**Invariant:** Events from superseded, unmapped, or completed runs cannot mutate
active run state or prematurely terminate new active runs.

**Why:** Late exit or failure events from old runs could otherwise kill newly
spawned active replacement runs.

**Evidence:** `ActiveRunRegistry.process_event` rejecting stale run identifiers
with `STALE_RUN_EVENT`.

**Authority:** `ai_engineering/execution/run_registry.py`.

### E4 (INV-EXEC-V4-004). Cancellation request non-exit invariant

**Invariant:** A cancellation request (`CANCEL_REQUESTED`) transitions run
intent but does not assume immediate process exit (`EXITED`).

**Why:** Cancellation requests are in-flight control signals; process exit must
be explicitly confirmed by process termination evidence.

**Evidence:** `RunState` transition table and `ActiveRunRegistry.request_cancel`.

**Authority:** `ai_engineering/execution/run_state.py`.

### E5 (INV-EXEC-V4-005). Idempotent spawn contract

**Invariant:** Repeated spawn requests for an already active run return
`ALREADY_ACTIVE` idempotently without duplicate process spawning or lease
mutations.

**Why:** Retries or parallel orchestration ticks must not trigger duplicate
execution processes for the same run authority.

**Evidence:** `ActiveRunRegistry.spawn_agent` returning `(record, SpawnStatus.ALREADY_ACTIVE)`.

**Authority:** `ai_engineering/execution/run_registry.py`.

### E6 (INV-EXEC-V4-006). Run-to-workspace and lease authority containment

**Invariant:** An agent run cannot exceed its bound workspace authority; lease
ownership mismatch is rejected with `RUN_LEASE_OWNERSHIP_MISMATCH`.

**Why:** Agent runs must only operate on isolated workspaces where they hold
explicit and matching lease ownership.

**Evidence:** `ActiveRunRegistry.register_run` validating workspace registration
and lease owner equality.

**Authority:** `ai_engineering/execution/run_registry.py`.

### E7 (INV-EXEC-V4-007). Zero process spawning in PR-2 contracts

**Invariant:** PR-2 implements immutable contracts, fencing rules, and state
machines only; actual OS subprocess spawning and parallel execution remain
completely deactivated (`OFF`).

**Why:** Foundation safety contracts must be validated and merged before
enabling execution capability at runtime.

**Evidence:** Feature flag verification and domain-only implementation.

**Authority:** `ai_engineering/execution/`.

## Parallelization policy and concurrency budget invariants

### P1 (INV-PAR-V4-001). Explicit policy approval for parallel execution

**Invariant:** Parallel agent execution requires explicit positive approval
from `ParallelizationPolicy`. In the absence of an explicit approval rule,
`strategy` defaults strictly to `NONE`.

**Why:** Implicit or speculative fan-out causes runaway resource usage and
state indeterminism.

**Evidence:** `ParallelizationPolicy.evaluate` default fallback to `NONE` with `allowed=False`.

**Authority:** `ai_engineering/parallel/parallel_policy.py`.

### P2 (INV-PAR-V4-002). Bounded concurrency budget

**Invariant:** Total concurrent candidates and agents are strictly bounded by
`ConcurrencyBudget` and can never exceed hard system limits (`max_candidates <= 3`).

**Why:** Unbounded parallelism creates resource exhaustion and unmanageable
candidate convergence trees.

**Evidence:** `ConcurrencyBudget.__post_init__` and policy clamping.

**Authority:** `ai_engineering/parallel/parallel_contracts.py`.

### P3 (INV-PAR-V4-003). Parallelism does not expand authorization

**Invariant:** Child parallel candidates strictly inherit the intersection of
parent authority and node boundary; parallelization never grants permissions
absent from parent scope.

**Why:** Subordinate execution paths must never escalate privilege or bypass
environmental boundaries.

**Evidence:** `ParallelizationPolicy.evaluate` validating against `AuthorityBoundary`.

**Authority:** `ai_engineering/parallel/parallel_policy.py`,
`ai_engineering/contracts.py`.

### P4 (INV-PAR-V4-004). Production mutation parallelism prohibition

**Invariant:** Direct parallel execution of production mutation (deployment, DB
migration, runtime modification, Qdrant vector mutation, secret rotation) is
strictly prohibited.

**Why:** Concurrent production writers cause non-deterministic race conditions,
corrupted state, and irrecoverable outages.

**Evidence:** `ParallelizationPolicy.evaluate` returning `allowed=False`, `strategy=NONE`,
`requires_single_mutation_owner=True`, and `PARALLEL_MUTATION_CONFLICT`.

**Authority:** `ai_engineering/parallel/parallel_policy.py`.

### P5 (INV-PAR-V4-005). Strict rehearsal separation from production mutation

**Invariant:** `REHEARSAL` strategy is strictly limited to simulation, schema
validation, and analysis with non-production/read-only side effects; it never
grants active production mutation authority.

**Why:** Rehearsal exists to build confidence prior to a single-owner production
mutation barrier.

**Evidence:** `ParallelizationPolicy.evaluate` allowing `REHEARSAL` only under
read-only side effects while enforcing `requires_single_mutation_owner=True`.

**Authority:** `ai_engineering/parallel/parallel_policy.py`.

### P6 (INV-PAR-V4-006). Deterministic policy evaluation

**Invariant:** Policy evaluation is a deterministic function of normalized task
metadata, complexity, uncertainty, risk, and budget; same inputs always produce
identical decisions and JSON representations.

**Why:** Non-deterministic routing causes unpredictable pipeline behaviour and
flaky execution graphs.

**Evidence:** `ParallelizationDecision.to_json` byte-equivalence across repeated evaluations.

**Authority:** `ai_engineering/parallel/parallel_policy.py`.

### P7 (INV-PAR-V4-007). Zero process spawning in policy layer

**Invariant:** `ParallelizationPolicy` evaluates policy contracts and bounds
only; it does not spawn processes, create worktrees, or invoke model APIs.

**Why:** Policy routing must remain decoupled from execution runtime.

**Evidence:** Non-interference assertions in test suites.

**Authority:** `ai_engineering/parallel/`.

## Parallel repository investigation invariants

### I1 (INV-INV-V4-001). Preparatory strategy constraint for repository investigation

**Invariant:** Parallel repository investigation batches require an approved
`ParallelizationDecision` with strategy `PREPARATORY`; non-preparatory strategies
are strictly rejected.

**Why:** Investigation fan-out is strictly intended for read-only exploration and
information gathering before implementation synthesis.

**Evidence:** `ParallelRepositoryInvestigator.execute_batch` asserting strategy `PREPARATORY`.

**Authority:** `ai_engineering/investigation/investigation_runner.py`.

### I2 (INV-INV-V4-002). Strict read-only investigator authority and command allowlist

**Invariant:** Repository investigators have pure read-only authority. File writes,
git mutations (`commit`, `checkout`, `reset`, `clean`, `merge`), deletions (`rm`),
and in-place editors (`sed -i`) are strictly prohibited and blocked in code.

**Why:** Background investigation must never alter working tree state, branch pointers,
or repository history.

**Evidence:** `validate_investigation_command` and read-only file scanners.

**Authority:** `ai_engineering/investigation/investigation_runner.py`.

### I3 (INV-INV-V4-003). Exact base SHA binding across investigation batches

**Invariant:** All investigators within a batch must bind to the exact same base SHA;
any mismatch against current repository HEAD fails closed immediately.

**Why:** Divergent base SHAs produce contradictory search observations and broken
evidence provenance.

**Evidence:** `execute_single_investigation` asserting `git rev-parse HEAD == base_sha`.

**Authority:** `ai_engineering/investigation/investigation_runner.py`.

### I4 (INV-INV-V4-004). Repository-relative result paths and filesystem fencing

**Invariant:** All match paths and scope paths must resolve strictly within the
repository root and be serialized as repository-relative paths (`path/to/file.py`);
absolute paths and `..` traversals are rejected.

**Why:** Machine-readable handoff contracts must remain portable and protected
against path traversal escapes.

**Evidence:** `RepositoryMatch.__post_init__` and `RepositoryInvestigationRequest.__post_init__`.

**Authority:** `ai_engineering/investigation/investigation_contracts.py`.

### I5 (INV-INV-V4-005). Stale result fencing and cancellation handling

**Invariant:** Results from cancelled, superseded, or non-live agent runs are
fenced and rejected from aggregate state.

**Why:** Asynchronous late-arriving results from aborted branches must not pollute
the active execution context.

**Evidence:** `execute_single_investigation` validating against `ActiveRunRegistry`.

**Authority:** `ai_engineering/investigation/investigation_runner.py`.

### I6 (INV-INV-V4-006). Concurrency budget compliance

**Invariant:** Concurrent investigator threads/processes must never exceed the
effective agent limit defined by `ParallelizationDecision` and `ConcurrencyBudget`.

**Why:** Execution must not overwhelm system resources or exceed concurrency quotas.

**Evidence:** `ParallelRepositoryInvestigator.execute_batch` clamping thread workers to budget.

**Authority:** `ai_engineering/investigation/investigation_runner.py`.

### I7 (INV-INV-V4-007). Repository state non-mutation

**Invariant:** Repository `HEAD` and `git status --porcelain` must be byte-identical
before and after repository investigation batch execution.

**Why:** Proof of zero side effects in read-only investigation plane.

**Evidence:** Fixture assertions in `tests/ai_engineering/test_investigation_invariants.py`.

**Authority:** `ai_engineering/investigation/`.

### I8 (INV-INV-V4-008). Zero TaskGraph and production mutation

**Invariant:** Repository investigators do not mutate `TaskGraph` nodes or acquire
production mutation authority.

**Why:** Decoupled investigation produces raw evidence for subsequent planning
without auto-advancing graph lifecycle.

**Evidence:** Pure functional return of `RepositoryInvestigationAggregate`.

**Authority:** `ai_engineering/investigation/`.

## Candidate implementations invariants

### C1 (INV-CAND-V4-001). Candidate strategy constraint for candidate batch execution

**Invariant:** Parallel candidate implementation batches require an approved
`ParallelizationDecision` with strategy `CANDIDATE`; non-candidate strategies
(`NONE`, `PREPARATORY`, `REVIEW`, `REHEARSAL`) are strictly rejected.

**Why:** Creating isolated writable candidate worktrees is permissible only under
an explicit candidate exploration authorization.

**Evidence:** `ParallelCandidateRunner.execute_batch` asserting strategy `CANDIDATE`.

**Authority:** `ai_engineering/candidates/candidate_runner.py`.

### C2 (INV-CAND-V4-002). Isolated writable worktree and lease ownership per candidate

**Invariant:** Every candidate receives a dedicated, isolated writable Git worktree
with a unique branch, unique workspace ID, unique AgentRunIdentity, and active
`WorktreeLease`. Two candidates never share a writable worktree.

**Why:** Prevents cross-candidate race conditions, dirty file leaks, and checkout collisions.

**Evidence:** `ParallelCandidateRunner.execute_batch` allocating separate worktrees and leases.

**Authority:** `ai_engineering/candidates/candidate_runner.py`.

### C3 (INV-CAND-V4-003). Exact base SHA binding across candidate batch

**Invariant:** All candidates in a batch must originate from the exact same approved
base SHA; mismatch against repository HEAD fails closed immediately.

**Why:** Divergent base SHAs corrupt comparative candidate evaluation and evidence provenance.

**Evidence:** `execute_single_candidate` asserting base SHA equality.

**Authority:** `ai_engineering/candidates/candidate_runner.py`.

### C4 (INV-CAND-V4-004). Strict scope fencing and non-expansion of candidate authority

**Invariant:** Candidates may modify only files matching declared repository-relative
`allowed_paths`. Any out-of-scope mutation invalidates the candidate with `CANDIDATE_SCOPE_VIOLATION`.

**Why:** Prevents untrusted side-effects and maintains bounded blast radius.

**Evidence:** `execute_single_candidate` diff inspection against `allowed_paths`.

**Authority:** `ai_engineering/candidates/candidate_runner.py`.

### C5 (INV-CAND-V4-005). Canonical main non-mutation and main branch protection

**Invariant:** Candidates must never execute inside the canonical checkout or against
the `main` branch. Canonical HEAD and porcelain status must be byte-identical before
and after candidate batch execution.

**Why:** Protects authoritative canonical working tree and primary branch pointers.

**Evidence:** `execute_single_candidate` rejecting canonical checkout and `main` branch.

**Authority:** `ai_engineering/candidates/candidate_runner.py`.

### C6 (INV-CAND-V4-006). Zero production mutation authority

**Invariant:** Candidate implementations never receive production mutation ownership
or access to deployment, migration, secret rotation, or remote SSH execution.

**Why:** Candidate exploration is strictly repository-local and pre-merge.

**Evidence:** `validate_candidate_command` and sandbox execution boundaries.

**Authority:** `ai_engineering/candidates/candidate_runner.py`.

### C7 (INV-CAND-V4-007). Stale result fencing and cancellation handling

**Invariant:** Events and completions from cancelled or superseded execution epochs
are fenced and cannot overwrite current candidate state.

**Why:** Asynchronous late-arriving callbacks from aborted runs must not pollute active state.

**Evidence:** Integration with `ActiveRunRegistry` in `execute_single_candidate`.

**Authority:** `ai_engineering/candidates/candidate_runner.py`.

### C8 (INV-CAND-V4-008). Concurrency budget compliance

**Invariant:** Concurrent candidate workers are strictly clamped to
`min(batch.max_parallel, decision.max_agents, decision.max_candidates)` with a default
hard ceiling of 3.

**Why:** Prevents resource starvation and runaway concurrency fan-out.

**Evidence:** `CandidateImplementationBatch.__post_init__` and `ParallelCandidateRunner`.

**Authority:** `ai_engineering/candidates/candidate_contracts.py`.

### C9 (INV-CAND-V4-009). Candidate result is evidence, not winner

**Invariant:** `CandidateResult` represents raw execution evidence (diffs, validation
outcomes, changed paths) and never authorizes merge, selection, or winner declaration.
Semantic judging and selection belong strictly to PR-6.

**Why:** Clean separation between candidate execution and candidate evaluation.

**Evidence:** `CandidateResult` contract containing no winner/selection fields.

**Authority:** `ai_engineering/candidates/candidate_contracts.py`.

## Candidate judge invariants

### J1 (INV-JUDGE-V4-001). Hard validation strictly precedes and dominates semantic review

**Invariant:** Mandatory deterministic hard gates must be evaluated first. A candidate
failing any hard gate (test failure, scope violation, base mismatch, stale run, invalid state)
can NEVER be eligible or selected regardless of any semantic or LLM score.

**Why:** Prevents non-deterministic semantic evaluations from overriding deterministic safety boundaries.

**Evidence:** `CandidateJudge.evaluate_hard_gates` and `CandidateJudgement` invariant assertions.

**Authority:** `ai_engineering/judge/candidate_judge.py`.

### J2 (INV-JUDGE-V4-002). Failed hard gates strictly skip semantic review

**Invariant:** When a candidate fails any hard validation gate, the semantic evaluator
MUST NOT be invoked for that candidate.

**Why:** Conserves compute/evaluator resources and prevents evaluation of unsafe or invalid artifacts.

**Evidence:** `CandidateJudge.judge` evaluating semantic scores only for `eligible_candidates`.

**Authority:** `ai_engineering/judge/candidate_judge.py`.

### J3 (INV-JUDGE-V4-003). Batch base SHA consistency and cross-base rejection

**Invariant:** All candidate results within a single judge request must originate from
the exact same approved base SHA matching the request. Mixed-base candidate batches fail closed with `CANDIDATE_BASE_DRIFT`.

**Why:** Comparing candidates across divergent base commits yields invalid comparative evaluations.

**Evidence:** `CandidateJudgeRequest.__post_init__` asserting base SHA identity.

**Authority:** `ai_engineering/judge/judge_contracts.py`.

### J4 (INV-JUDGE-V4-004). Stale execution and superseded epoch fencing

**Invariant:** Candidate results containing stale run event blockers (`STALE_RUN_EVENT`,
`STALE_RUN_MUTATION`, `RUN_WORKSPACE_MISMATCH`) are rejected by hard validation.

**Why:** Outdated or superseded execution artifacts must never pollute current decision state.

**Evidence:** Hard gate `NO_STALE_EXECUTION` in `CandidateJudge.evaluate_hard_gates`.

**Authority:** `ai_engineering/judge/candidate_judge.py`.

### J5 (INV-JUDGE-V4-005). Deterministic ranking and input-order independence

**Invariant:** Candidate judging outputs identical rankings, scores, and selection
regardless of the order candidates appear in the request.

**Why:** Eliminates non-deterministic decision changes caused by array permutation or scheduling jitter.

**Evidence:** Deterministic pre-sorting by `candidate_id` in `CandidateJudge.judge`.

**Authority:** `ai_engineering/judge/candidate_judge.py`.

### J6 (INV-JUDGE-V4-006). Explicit tie policy and deterministic tie-breaking

**Invariant:** Tied semantic scores are resolved via documented deterministic lexical
tie-breaking when `allow_tie_break=True`, or explicit `TIE` state without selection when `allow_tie_break=False`.

**Why:** Rejects arbitrary or random winner selection.

**Evidence:** `CandidateJudge.judge` tie handling logic and `CandidateDecisionState.TIE`.

**Authority:** `ai_engineering/judge/candidate_judge.py`.

### J7 (INV-JUDGE-V4-007). Read-only candidate judge non-mutation

**Invariant:** The CandidateJudge operates strictly read-only against candidate
workspaces, canonical main, Git branches, and database stores.

**Why:** Prevents unintended side-effects during candidate evaluation.

**Evidence:** `CandidateJudge` class containing zero write/mutation methods.

**Authority:** `ai_engineering/judge/candidate_judge.py`.

### J8 (INV-JUDGE-V4-008). Selection evidence only without merge or production authority

**Invariant:** `CandidateJudgeResult.selected_candidate_id` represents evaluation evidence
only and does not authorize merge, cherry-pick, TaskGraph transition, or production deployment.

**Why:** Preserves separation of concerns between candidate evaluation and merge/deployment orchestration.

**Evidence:** `CandidateJudgeResult` contract schema.

**Authority:** `ai_engineering/judge/judge_contracts.py`.

### J9 (INV-JUDGE-V4-009). Zero provider calls and injected semantic evaluation

**Invariant:** Candidate judging utilizes an injectable evaluator protocol with zero
uncontrolled remote provider calls in core judging infrastructure.

**Why:** Ensures determinism, testability, and fail-closed local verification.

**Evidence:** `SemanticCandidateEvaluator` protocol in `ai_engineering/judge/semantic_evaluator.py`.

**Authority:** `ai_engineering/judge/semantic_evaluator.py`.

## Candidate workspace snapshot & diff artifact invariants (v4.1 PR-7)

### S1 (INV-SNAPSHOT-V4-001). WorkspaceSnapshot is immutable point-in-time evidence

**Invariant:** Once emitted, `WorkspaceSnapshot` and `DiffArtifact` instances are frozen
and cannot be mutated. Any subsequent workspace state changes produce a new snapshot ID.

**Why:** Prevents historical state mutation, race conditions, and observational tampering.

**Evidence:** `WorkspaceSnapshot` and `DiffArtifact` dataclasses (`frozen=True`).

**Authority:** `ai_engineering/workspaces/snapshot_contracts.py`.

### S2 (INV-SNAPSHOT-V4-002). Snapshot capture is strictly read-only

**Invariant:** Snapshot capture executes only non-mutating Git operations (`status`, `rev-parse`, `diff`, `ls-files`).
It must never execute `git add`, `commit`, `reset`, `clean`, `checkout`, `restore`, `merge`, `rebase`, `cherry-pick`, or `push`.

**Why:** Guarantees that observation cannot pollute, reset, or alter isolated candidate workspaces.

**Evidence:** `WorkspaceSnapshotManager` read-only operations.

**Authority:** `ai_engineering/workspaces/snapshot_manager.py`.

### S3 (INV-SNAPSHOT-V4-003). Deterministic diff digest

**Invariant:** Normalized diff content produces a deterministic SHA-256 digest independent of timestamps,
absolute paths, OS platform, or thread scheduling.

**Why:** Enables tamper-evident cryptographic attestation of worktree changes.

**Evidence:** `compute_diff_digest` and `verify_diff_artifact` in `ai_engineering/workspaces/diff_artifacts.py`.

**Authority:** `ai_engineering/workspaces/diff_artifacts.py`.

### S4 (INV-SNAPSHOT-V4-004). Changed paths are repository-relative and non-escaping

**Invariant:** All paths exposed in snapshots and diff artifacts are repository-relative, normalized,
and strictly fenced against traversal escapes (`..`, absolute Linux/Windows paths, UNC paths, backslashes).

**Why:** Prevents directory traversal attacks and environment-dependent file resolution bugs.

**Evidence:** `validate_repository_relative_path` validator.

**Authority:** `ai_engineering/workspaces/snapshot_contracts.py`.

### S5 (INV-SNAPSHOT-V4-005). Foreign absolute path handoff is forbidden

**Invariant:** Candidate-to-judge and inter-agent handoffs rely exclusively on workspace IDs, candidate IDs,
run IDs, base SHAs, repository-relative paths, and artifact identifiers, never on host-specific absolute paths.

**Why:** Enables seamless multi-host and container execution isolation.

**Evidence:** `DiffArtifact` and `WorkspaceSnapshot` contract interfaces.

**Authority:** `ai_engineering/workspaces/snapshot_contracts.py`.

### S6 (INV-SNAPSHOT-V4-006). Canonical checkout protection

**Invariant:** Candidate workspace snapshot capture against the canonical repository checkout is strictly forbidden
and blocked with `WORKSPACE_SNAPSHOT_CANONICAL_FORBIDDEN`.

**Why:** Guarantees that the canonical checkout cannot be confused with an isolated candidate worktree.

**Evidence:** Canonical checkout path validation in `WorkspaceSnapshotManager`.

**Authority:** `ai_engineering/workspaces/snapshot_manager.py`.

### S7 (INV-SNAPSHOT-V4-007). Base SHA and workspace identity binding

**Invariant:** Snapshot capture verifies that workspace identity, candidate identity, run identity,
base SHA, and active worktree lease ownership are mutually consistent and valid.

**Why:** Prevents cross-workspace pollution and orphaned execution capture.

**Evidence:** Identity binding checks in `WorkspaceSnapshotManager`.

**Authority:** `ai_engineering/workspaces/snapshot_manager.py`.

### S8 (INV-SNAPSHOT-V4-008). Stale run and epoch fencing

**Invariant:** Snapshot capture and artifact registration are rejected if the active agent run identity
is non-live, terminated, or from a stale execution epoch (`STALE_RUN_EVENT`).

**Why:** Fences against delayed or out-of-order background execution events.

**Evidence:** Run state verification in `WorkspaceSnapshotManager`.

**Authority:** `ai_engineering/workspaces/snapshot_manager.py`.

### S9 (INV-SNAPSHOT-V4-009). Snapshot evidence does not grant merge or deployment authority

**Invariant:** `WorkspaceSnapshot` and `DiffArtifact` represent observational evidence only.
They do not grant automatic merge, cherry-pick, TaskGraph mutation, or deployment authority.

**Why:** Maintains fail-closed separation between evidence collection and governance actions.

**Evidence:** Architectural boundary specifications.

**Authority:** `docs/HERMES_INVARIANTS.md`.

### S10 (INV-SNAPSHOT-V4-010). PR-8 owns drift requalification

**Invariant:** PR-7 snapshot contracts capture base SHA and diff metadata as-is.
Requalification and base drift reconciliation are owned exclusively by PR-8.

**Why:** Enforces bounded scope and clean evolutionary stages across PRs.

**Evidence:** `WorkspaceSnapshot` and `DiffArtifact` contracts.

**Authority:** `ai_engineering/workspaces/snapshot_contracts.py`.

### S11 (INV-SNAPSHOT-V4-011). Raw prompt storage is forbidden

**Invariant:** No raw model prompts, untrusted inputs, or provider payloads may be stored
in durable snapshot telemetry or diff artifacts (`RAW_PROMPT_STORAGE=FORBIDDEN`).

**Why:** Enforces zero secret leakage and data minimization policies.

**Evidence:** Strict schema validation in `DiffArtifact` and `WorkspaceSnapshot`.

**Authority:** `ai_engineering/workspaces/snapshot_contracts.py`.

## Change validation invariant

### V1. Claims match executed evidence

**Invariant:** Reports distinguish `PASS`, `FAIL`, `NOT RUN`, `NOT PERFORMED`
and `INCONCLUSIVE`; no test, build, deploy or production state is claimed
without direct evidence.

**Why:** A plausible or historical result can misdirect the next agent into an
unsafe action.

**Evidence:** Command/result matrix, exact commit SHA, clean diff/status,
focused/project checks and explicit production-changed fields.

**Authority:** `AGENTS.md`, `RUNBOOK_CODING_LOOP.md`, `AI_AGENT_RULEBOOK.md`.
