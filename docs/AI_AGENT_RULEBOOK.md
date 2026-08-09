# AI Agent Rulebook

Status: normative repository workflow
Scope: Hermes / HealBite engineering tasks

## Purpose and authority

This rulebook turns repository knowledge into a repeatable decision protocol
for AI-assisted engineering. It complements, and never overrides, the current
task, `AGENTS.md`, a more local instruction file or the applicable procedural
skill.

Read `HERMES_SOURCE_MAP.md` to find evidence and
`HERMES_INVARIANTS.md` to identify what must remain true.

## Decision protocol

### 1. Parse the contract

Before acting, extract:

- exact goal and deliverables;
- allowed and forbidden mutations;
- canonical repository/remote/base, if supplied;
- production, data, secret and external-service boundaries;
- required validations and evidence fields;
- stop boundary and delivery state (local diff, commit, Draft PR, merge, build,
  deploy or manual smoke).

Do not treat model recommendation, urgency or `FAST_TRACK` as a waiver of a
safety invariant.

### 2. Establish provenance

Fetch the canonical remote when network access is allowed. Resolve the exact
base SHA and require a clean isolated worktree. Record:

```text
repository=
remote=
remote_main_sha=
head_sha=
branch=
worktree_clean=
```

If a required exact base moved, stop or re-plan according to the task. Never
silently continue from a stale checkout.

### 3. Discover before designing

Use the smallest read-only search that can answer:

- where the current behavior is implemented;
- which tests prove it;
- which ADR/skill/runbook records intent;
- which state is durable and which is derived;
- which trust and ownership boundary applies;
- whether the claimed component actually exists.

For a feature, trace entry point -> routing/controller -> service/store ->
external dependency -> tests. For an operation, trace versioned policy ->
producer/consumer CLI -> evidence schema -> rollback path.

### 4. Classify facts

Label important statements as one of:

- `CONFIRMED_CURRENT`: proved from current canonical code or fresh evidence;
- `CONFIRMED_HISTORICAL`: proved in history but not current runtime;
- `PLANNED`: documented intent without implementation proof;
- `UNKNOWN`: evidence is absent;
- `INCONCLUSIVE`: evidence exists but cannot decide the claim.

Do not promote a design plan, old PR, chat report, branch name, tag or container
label into current truth without verification.

### 5. Select the least-mutating solution

Prefer extending an existing boundary. For new capability follow the repository
footprint ladder: existing code, CLI+skill, gated tool, plugin, MCP, then core
tool as a last resort. Keep product behavior out of generic transport modules
when a controller/service boundary already exists.

For production diagnosis, prefer read-only and no-send probes. For data work,
prefer rehearsal copies and dry runs. A workaround that bypasses an invariant
is not a solution.

### 6. Plan evidence with the change

Before editing, name:

- files in scope;
- invariants at risk;
- focused tests and negative cases;
- project-level checks;
- rollback/recovery for any authorized mutation;
- documentation/state records that must change;
- stop conditions.

Plan semantics, not ceremonial phases. Small documentation changes may use a
short plan; production migrations require the full procedural skill.

### 7. Implement narrowly

Change only the intended files. Preserve unrelated user changes. Add behavior
tests that assert relationships and failure boundaries rather than snapshots of
incidental counts or versions. Avoid speculative abstractions and broad
refactors unless the task explicitly asks for them.

### 8. Validate proportionally

Run, in order when applicable:

1. repository secret check;
2. focused tests for the changed behavior;
3. adjacent contract/regression tests;
4. repository project check (`scripts/agent_check.sh`);
5. `git diff --check` and final status/diff review;
6. exact-head CI when delivery requires it.

Use `scripts/run_tests.sh` for repository tests. Never report PASS for a check
that did not run or whose result cannot be bound to the final commit.

### 9. Update decision memory

If the task changes confirmed state, update `docs/CURRENT_STATE.md` and its
changelog in the same PR. Record durable architectural decisions in `docs/adr/`
and procedural safety rationale in the relevant skill. Do not duplicate a
canonical procedure into passive docs; link to it.

### 10. Deliver at the exact stop boundary

Stage only intended files, commit intentionally and publish only when requested.
A task ending at Draft PR must not merge. A task ending after merge must not
build or deploy. A build-only task must not materialize on a production-bound
daemon. A deploy awaiting manual smoke must not impersonate the user.

Report facts, unknowns, remaining risk and the single next authorized action.

## Always

- Read the current task, `AGENTS.md`, `RUNBOOK_CODING_LOOP.md` and
  `docs/CURRENT_STATE.md` before repository changes.
- Load the relevant deployment, memory or Telegram skill when its domain is in
  scope.
- Use the canonical remote and exact SHA; work in a separate clean worktree.
- Inspect code, tests and history before relying on a prior PR or plan.
- Preserve user/profile/chat/household isolation and validate ownership at the
  durable boundary.
- Treat SQLite as authoritative where the current HealBite contract says so;
  treat Qdrant as derived.
- Treat LLM/Vision output as untrusted and validate locally.
- Keep secrets and private identifiers out of commands, output, Git and
  evidence.
- Use explicit statuses: `PASS`, `FAIL`, `BLOCKED`, `NOT RUN`,
  `NOT PERFORMED`, `UNKNOWN`, or `INCONCLUSIVE`.
- Stop on missing technical evidence before an irreversible or production
  mutation.
- Review the final diff for unrelated changes and run `git diff --check`.

## Never

- Never infer production state from local source, old evidence or a mutable tag.
- Never edit a dirty canonical checkout or erase user changes with reset, clean,
  stash or broad restore.
- Never print `.env`, tokens, API keys, user/chat IDs, health data, messages,
  raw production logs or correlation identifiers.
- Never run a second Telegram poller with a token already in use.
- Never make Qdrant authoritative for a HealBite fact or infer convergence from
  equal counts alone.
- Never copy an active SQLite file as a backup without approved quiescence;
  never migrate without exact-path, integrity, FK, backup and scope evidence.
- Never use a direct schema initializer or manual DDL as a substitute for the
  canonical production migration path.
- Never deploy by mutable tag, dirty context or unbound image identity.
- Never bypass a failed/unknown technical gate because a warning was called
  governance-only or a task is urgent.
- Never merge, deploy, restart, recreate, send live messages, change secrets or
  mutate durable data beyond the current task's explicit authority.
- Never claim tests, CI, build, deployment or manual smoke that did not occur.
- Never invent a service, architecture or historical incident to make a
  document look complete.

## Review checklist

- [ ] Goal, scope and stop boundary are explicit.
- [ ] Canonical base and clean-worktree evidence are recorded.
- [ ] Current implementation and tests were discovered.
- [ ] Affected invariants and trust boundaries are named.
- [ ] The solution uses the least permanent/mutating surface.
- [ ] Negative, isolation and failure cases are tested.
- [ ] Secrets/private data are absent from diff and evidence.
- [ ] Required focused, project, diff and CI checks are accurately classified.
- [ ] Current state/ADR/skill documentation is updated or explicitly not needed.
- [ ] Final report distinguishes completed, unperformed and blocked work.
