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
