# Hermes core

Activation intent: **Always On** for this workspace.

This file is a thin Antigravity adapter. The canonical engineering contracts
remain:

- [AGENTS.md](../../AGENTS.md)
- [Hermes Source Map](../../docs/HERMES_SOURCE_MAP.md)
- [Current State](../../docs/CURRENT_STATE.md)
- [Task Lifecycle](../../docs/TASK_LIFECYCLE.md)
- [AI Agent Rulebook](../../docs/AI_AGENT_RULEBOOK.md)

Before non-trivial work, read those sources, establish the task-named canonical
repository, remote and exact main SHA, verify a clean isolated worktree, and run
`python scripts/prepare_task.py`. Inspect its repository-bound output before
planning. Repository contracts and fresh evidence override conversational
memory; never infer an ambiguous `main` or force a missing gate to PASS.

`FAST_TRACK` removes optional governance delay, not provenance, authorization,
test, security, evidence, rollback, or stop-boundary safeguards. Use
`PASS | FAIL | BLOCKED | ROLLED_BACK` for terminal operation outcomes and retain
the wider repository gate taxonomy where its contracts require it.

Do not mutate production, databases, Qdrant, secrets, provider state, feature
flags, or deployments unless the current task explicitly authorizes that exact
effect and every applicable technical gate passes. Production mutation has one
serialized owner. Antigravity and Codex must never mutate the same branch or
worktree concurrently.

When authorized and technically safe, repository implementation, validation,
PR, exact-head CI, merge, ancestry verification, and source-of-truth closure may
proceed continuously. Stop before the first unauthorized boundary.
