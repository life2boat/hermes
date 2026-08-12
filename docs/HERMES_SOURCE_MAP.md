# Hermes / HealBite Source Map

Status: authoritative navigation map
Verified against canonical main: `86d334f0b3285e14c74f9f507dc3406735c376b3`

## Purpose

This map answers one question: where must an engineer or AI agent look before
making a claim or change? It names evidence-bearing sources; it is not a
replacement for those sources. Runtime facts must be reverified when a task is
time-sensitive, and production state must never be inferred from a checkout.

## Source precedence

When sources disagree, use this order:

1. The current task and its explicit safety/stop boundary.
2. `AGENTS.md` and repository-local instructions in the affected tree.
3. Executable contracts, schemas, tests, and versioned deployment policy.
4. The relevant procedural `SKILL.md` and current runbook.
5. `docs/CURRENT_STATE.md` for confirmed project state.
6. ADRs and design documents for accepted intent.
7. Git history for provenance and superseded decisions.
8. Chat transcripts, pasted reports, and external notes as evidence only.

Unknown or conflicting facts stay `UNKNOWN` or `INCONCLUSIVE`; they are not
filled in from memory.

## Architecture

| Concern | Primary sources | What they establish |
| --- | --- | --- |
| Project intent and layout | `AGENTS.md`, `README.md` | Hermes purpose, narrow-core policy, prompt-cache invariant, component map, contribution rules |
| AI task lifecycle, preparation and review | `docs/TASK_LIFECYCLE.md`, `scripts/prepare_task.py`, `docs/TASK_TEMPLATE.md`, `docs/AI_REVIEW_CHECKLIST.md`, `docs/PRODUCTION_READINESS_CHECKLIST.md` | Required task sequence, repository-bound context capture, review evidence and fail-closed readiness classification |
| AI Behaviour & LLM Ops | `docs/AGENT_BEHAVIOUR_CONTRACT.md`, `docs/BEHAVIOUR_EVALS.md`, `docs/LLM_OPS_POLICY.md`, `docs/AGENT_RELEASE_GATES.md`, `docs/SKILL_LOOP_GRAPH_LIFECYCLE.md`, `ai_engineering/contracts.py`, `ai_engineering/trace.py`, `ai_engineering/redaction.py`, `ai_engineering/scenario.py`, `ai_engineering/graders.py`, `ai_engineering/eval_runner.py`, `ai_engineering/model_policy.py`, `ai_engineering/cost_policy.py`, `ai_engineering/release_gate.py`, `ai_engineering/failure_candidate.py`, `ai_engineering/procedure_maturity.py`, `scripts/run_agent_behaviour_evals.py`, `scripts/check_llm_ops_policy.py`, `scripts/check_agent_release_gate.py`, `scripts/build_failure_eval_candidate.py`, `scripts/check_procedure_maturity.py`, `.github/workflows/agent-release-gate.yml`, `evals/agent_behaviour/` | Behaviour semantics, closed evidence, deterministic replay/grading, GOLDEN corpus/baseline, executable model/cost policies, candidate-only failure feedback, procedure-maturity evidence, and distinct exact-head merge/production release decisions |
| Agent core | `run_agent.py`, `agent/`, `agent/conversation_loop.py` | `AIAgent`, conversation/tool loop, transport adapters, context, retries, compression, provider-facing behavior |
| Model/provider routing | `hermes_cli/runtime_provider.py`, `agent/auxiliary_client.py`, `agent/transports/`, `providers/` | Runtime provider resolution, API transports, auxiliary model calls, provider isolation and fallback boundaries |
| Tools | `tools/registry.py`, `model_tools.py`, `toolsets.py`, `tools/` | Tool registration, discovery, schemas, availability gates, toolset filtering, dispatch |
| Messaging gateway | `gateway/run.py`, `gateway/session.py`, `gateway/config.py`, `gateway/platforms/` | Platform lifecycle, session/source identity, routing, delivery and adapter boundaries |
| Telegram and HealBite UI | `gateway/platforms/telegram.py`, `gateway/healbite_*_telegram.py` | Telegram transport, authorization/routing hooks, feature-specific FSMs, callbacks and presentation |
| HealBite domain | `gateway/healbite_*.py`, `docs/design/`, `docs/adr/` | Product services, SQLite schemas, household/user boundaries, weekly menu, shopping, nutrition and inventory behavior |
| Durable sessions | `hermes_state.py` | SQLite-backed Hermes session history, FTS, model/session metadata and repair boundaries |
| Scheduling and workers | `cron/`, `tools/delegate_tool.py`, `plugins/kanban/`, `tools/kanban_tools.py`, `batch_runner.py` | Scheduled jobs, synchronous delegated agents, durable Kanban workers and batch execution |
| Extensibility | `plugins/`, `skills/`, `optional-skills/`, `tools/mcp_tool.py` | Plugin, skill and MCP extension surfaces outside the narrow core |

## Deployment and release

| Concern | Primary sources | What they establish |
| --- | --- | --- |
| Production policy | `deploy/hermes-production.json` | Canonical remote/main, required CI, Compose identity, DB mount, protected-secret source, capacity and runtime paths |
| Compose topology | `docker-compose.yml`, `deploy/docker-compose.production.yml` | Repository-defined `hermes-bot` and `qdrant` services, mounts, volumes and production overrides |
| Exact-main image build | `.github/workflows/healbite-exact-main-ghcr.yml`, `scripts/build_verified_playwright_image.py`, `scripts/attest_remote_registry_image.py`, `scripts/hermes_image_secret_scan.py` | Exact Git-tree build, immutable registry identity, OCI revision and full image-secret evidence |
| Ordinary deploy | `scripts/hermes_production_deploy.sh`, `scripts/hermes_production_deploy.py` | Read-only planning, technical gates, exact-image recreation, post-state attestation and image rollback |
| Legacy provenance transition | `scripts/hermes_legacy_provenance_bootstrap.py` | One-time, fail-closed transition from an unlabelled legacy runtime to a provenance-valid exact-main image |
| Staged schema migration | `scripts/hermes_production_staged_migrate.py`, `scripts/hermes_execution_authority.py`, `scripts/hermes_release_authority.py` | Authority package, effective migration scope, staging/exchange, evidence and rollback constraints |
| Operator procedure | `skills/deploy/SKILL.md`, `docs/runbooks/hermes-production-deployment.md`, `docs/runbooks/hermes-remote-exact-main-build.md` | Required order, evidence, failure and recovery semantics |
| Feature rollout detail | `docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md` and feature runbooks | Feature-specific rollout and migration gates; these do not override the deploy skill |

## Memory and state

| Concern | Primary sources | What they establish |
| --- | --- | --- |
| Hermes session state | `hermes_state.py` | Durable conversation/session records and FTS behavior |
| HealBite Memory OS | `gateway/platforms/healbite_memory_bridge.py` | SQLite fact source of truth, user-scoped access, FTS/LIKE fallback and asynchronous vector synchronization |
| Qdrant adapter | `gateway/memory/qdrant_adapter.py`, `gateway/memory/embedding_adapter.py`, `gateway/memory/settings.py` | Optional, best-effort semantic index, user filters, collection/config boundary and graceful degradation |
| Memory operations | `skills/memory/SKILL.md`, `RUNBOOK_MEMORY_OS.md`, `scripts/rebuild_qdrant_memory_index.py` | Read-only baseline, reconciliation, mutation authorization, backup and recovery procedure |
| Product data | `gateway/healbite_*_schema.py`, `gateway/healbite_*.py` | HealBite SQLite schema and service-level user/household rules |
| Confirmed state | `docs/CURRENT_STATE.md`, `docs/CURRENT_STATE_CHANGELOG.md` | Current confirmed facts and historical state updates; not a substitute for fresh production discovery |

## Security and trust

| Concern | Primary sources | What they establish |
| --- | --- | --- |
| Repository safety policy | `AGENTS.md`, `SECURITY.md`, `RUNBOOK_CODING_LOOP.md` | Change authority, secret handling, worktree discipline and validation workflow |
| Command/tool authorization | `tools/approval.py`, `agent/tool_guardrails.py`, `toolsets.py` | Approval boundaries and which tools can reach a model/runtime context |
| Gateway identity and access | `gateway/authz_mixin.py`, `gateway/slash_access.py`, `gateway/session.py`, platform adapters | Sender/chat/session scope and fail-closed authorization |
| Telegram safety | `skills/telegram/SKILL.md`, `gateway/platforms/telegram.py`, `gateway/status.py` | Token secrecy, single polling owner, safe diagnostics and no-send health checks |
| Source secret scanning | `scripts/secret_check.sh`, `scripts/secret_scanner.py`, `.dockerignore` | Staged changes, exact base-to-candidate Git-tree screening, and build-context exclusions |
| Image secret scanning | `scripts/hermes_image_secret_scan.py`, `deploy/hermes-image-secret-exceptions.json` | Metadata, layer and final-filesystem scanning with exact evidence-bound exceptions |
| Production secrets | `deploy/hermes-production.json`, `scripts/hermes_production_deploy.py` | Approved source path, ownership/mode, closed variable set and value-preserving publication contract |

## Historical decisions

| Source | Use |
| --- | --- |
| `docs/adr/` | Accepted architecture decisions and their alternatives/consequences |
| `docs/design/` | Feature contracts and implementation plans; verify implementation before treating a plan as current behavior |
| `docs/CURRENT_STATE_CHANGELOG.md` | Superseded project-state entries and safety outcomes |
| `git log`, `git show`, `git blame` | Exact provenance, intent and whether a change is an ancestor of canonical main |
| `docs/runbooks/` | Operational procedures; prefer the current linked skill when instructions moved |
| Tests under `tests/` | Executable behavioral and safety contracts |
| `knowledge/` | Curated architecture, decision, failure, pattern, operations and AI-agent indexes; follow their links to authoritative contracts |
| `docs/FAILURE_CAPTURE_LOOP.md` | Required sanitized learning loop after a serious incident; links the incident record to a test, ADR, skill or runbook as appropriate |

Chat history is not an architecture record. If a decision matters after the
task, record it in an ADR, a skill decision-memory section, a design contract,
or `CURRENT_STATE` as appropriate.

## Knowledge Pack status

The repository contains a structured `knowledge/` documentation layer covering
architecture, decisions, failures, patterns, operations and AI-agent lessons.
Together with the source map, system model, invariants, rulebook, task template,
ADRs, skills, runbooks and tests, it forms the reviewable Knowledge Pack. It is
not a service, database, retrieval pipeline or deployed runtime; any future
executable component requires separate implementation and repository evidence.

The stdlib-only ai_engineering library implements closed behaviour trace
and scenario schemas, sanitization, canonical serialization/digest, safe
fixture loading, provider-free replay, deterministic graders, and the offline
eval runner. `evals/agent_behaviour` is the human-reviewed GOLDEN corpus with a
digest-bound baseline and review evidence. The library also implements the
versioned provider-free model-selection/substitution policy, usage accounting,
external rate-card identity, deterministic cost-budget evaluation, and release
gate schema/policy version `1`. The release gate consumes independent fixed-
schema code, behaviour, security, live, cost, and production-readiness evidence
without inferring one gate from another. The read-only exact-head PR workflow
enforces the conservative merge profile while reporting production eligibility
as `NOT_PERFORMED`. This is not a service or product runtime. Failure-candidate
automation and procedure-maturity evaluation are offline candidate/review
controls; they cannot mutate the Golden corpus or grant authority.

## Fast lookup

- Changing agent/tool behavior: start with `AGENTS.md`, the affected module,
  registry/toolset definitions and focused tests.
- Changing Telegram/HealBite behavior: start with the adapter, feature
  controller/service, schema/store and cross-user/FSM tests.
- Changing durable data: start with the schema/service contract and load
  `skills/memory/SKILL.md` or `skills/deploy/SKILL.md` as applicable.
- Building or releasing: start with `deploy/hermes-production.json` and
  `skills/deploy/SKILL.md`; do not infer permission from a runbook command.
- Diagnosing production: perform fresh sanitized read-only discovery first;
  do not treat local files, old evidence or container tags as current truth.
