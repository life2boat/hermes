# AI-Assisted Change Review Checklist

Status: normative review aid

Use this checklist for repository changes produced or substantially assisted by
an AI agent. It complements `AGENTS.md`, `AI_AGENT_RULEBOOK.md`, local
instructions, tests, and domain skills; it does not override them.

Record every applicable item as `PASS`, `FAIL`, `INCONCLUSIVE`, or
`NOT_APPLICABLE` and cite deterministic evidence. An unchecked item is not a
PASS.

## 1. Contract and provenance

- [ ] `scripts/prepare_task.py` was run before implementation, and the generated
  context is bound to the reviewed repository SHA and branch.
- [ ] Goal, deliverables, allowed mutations, forbidden actions, and the exact
  stop boundary are explicit.
- [ ] The canonical remote/main and clean isolated worktree were verified.
- [ ] Changed files in the prepared context match the intended scope; unrelated
  user changes are absent.
- [ ] Historical plans, PRs, reports, and chat claims were revalidated against
  current canonical code or classified as historical/unknown.

## 2. Architecture alignment

- [ ] The change aligns with `docs/HERMES_SYSTEM_MODEL.md`; affected components
  and trust boundaries are named.
- [ ] The implementation follows the least-permanent footprint: existing code,
  CLI plus skill, gated tool, plugin, or MCP before a new core tool.
- [ ] Transport/UI code does not become authoritative product storage or absorb
  service behavior that belongs behind an existing controller/store boundary.
- [ ] SQLite remains durable authority where specified; Qdrant, FTS, views,
  caches, and generated drafts remain derived.
- [ ] The change does not invent a deployed service, worker, data flow, or
  production state that the repository and fresh evidence do not prove.
- [ ] Any durable architectural choice is recorded in an ADR, or the review
  explains why no ADR is required.

## 3. Invariants and trust boundaries

- [ ] Every affected invariant from `docs/HERMES_INVARIANTS.md` is named with
  evidence and a failure/stop condition.
- [ ] User, chat, thread, session, profile, and household access remains scoped
  at the authoritative boundary; caller/model identifiers are not authorization.
- [ ] LLM/Vision output is treated as untrusted and locally validated before
  persistence, authorization, tool execution, or user-visible claims.
- [ ] Multi-row durable changes are atomic; external dual writes have explicit
  partial-failure and reconciliation semantics.
- [ ] Telegram polling ownership and no-send diagnostic boundaries are preserved
  when applicable.
- [ ] Secrets, private identifiers, health data, raw production logs, and message
  contents are absent from code, tests, fixtures, output, and evidence.

## 4. Code quality and failure behavior

- [ ] The implementation is the smallest coherent change and avoids unrelated
  refactors or speculative abstractions.
- [ ] Interfaces, names, types, and error classifications communicate the actual
  contract rather than an optimistic assumption.
- [ ] Failures are bounded, sanitized, and fail closed at authorization, data,
  source, release, and production boundaries.
- [ ] Retries, concurrency, idempotency, and partial completion are handled where
  the code can be invoked more than once or interrupted.
- [ ] Generated files, caches, and local evidence are excluded from Git unless
  the task explicitly makes them reviewed artifacts.

## 5. Tests and validation

- [ ] Focused tests cover the intended success path.
- [ ] Negative tests cover malformed input, missing evidence, and unauthorized or
  out-of-scope actions.
- [ ] Isolation tests cover cross-user/cross-household/session mismatch where
  identity is involved.
- [ ] Failure-injection or rollback tests prove no partial durable mutation where
  state changes are involved.
- [ ] Adjacent regression/contract tests cover behavior that must not change.
- [ ] `scripts/secret_check.sh`, focused tests, `scripts/agent_check.sh`, and
  `git diff --check` are accurately reported against the final diff/commit.
- [ ] Required exact-head CI is complete and bound to the reviewed head SHA, or
  is explicitly `PENDING`/`NOT_RUN`; it is never inferred from a prior commit.

## 6. Documentation and delivery

- [ ] `docs/CURRENT_STATE.md` and its changelog are updated when confirmed state
  changes, or the review records why they are not required.
- [ ] Source maps, ADRs, skills, runbooks, and Knowledge Pack indexes link to one
  authoritative record rather than duplicating procedures.
- [ ] The final report distinguishes `PASS`, `FAIL`, `BLOCKED`, `NOT RUN`,
  `NOT PERFORMED`, and `INCONCLUSIVE`.
- [ ] Commit, PR, merge, build, deploy, smoke, and production claims stop at the
  task's exact authorized boundary.

## Reviewer outcome

```text
REVIEW_STATUS=PASS|FAIL|BLOCKED|INCONCLUSIVE
REVIEWED_SHA=
AFFECTED_INVARIANTS=
FOCUSED_TESTS=
RELATED_TESTS=
AGENT_CHECK=
SECRET_SCAN=
DIFF_CHECK=
EXACT_HEAD_CI=
BLOCKING_ISSUES=
REMAINING_RISKS=
```
