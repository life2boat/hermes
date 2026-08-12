# Hermes / HealBite System Model

Status: repository-grounded model
Verified against canonical main: `86d334f0b3285e14c74f9f507dc3406735c376b3`

## Purpose

Hermes is a personal AI-agent runtime shared by CLI, TUI, desktop and messaging
entry points. HealBite is a product/domain layer in this repository that uses
the Hermes gateway and agent infrastructure for nutrition, diary, household,
inventory, shopping and weekly-menu workflows.

This document describes repository-defined behavior. It does not claim that a
particular image, flag, database schema or container is active in production;
those are operational facts that require fresh evidence.

## Core components

### Telegram Gateway

`gateway/run.py` owns gateway lifecycle and routes platform events into Hermes
sessions. `gateway/platforms/telegram.py` receives and sends Telegram updates,
applies platform/user/chat gates, exposes HealBite commands and delegates
feature flows to focused controllers. `gateway/session.py` binds messages to a
platform, chat, user, thread and session context.

The Telegram adapter is a transport and UI boundary. It must not become the
authoritative store for product data or trust a callback/user identifier
without revalidating ownership.

### LLM Router

There is no single file named `llm_router`. Routing is composed from:

- `hermes_cli/runtime_provider.py` for configured runtime-provider resolution;
- `run_agent.py` and `agent/conversation_loop.py` for the agent loop;
- `agent/transports/` for provider/API protocol differences;
- `agent/auxiliary_client.py` for bounded auxiliary-model calls;
- provider-specific adapters and credential sources under `agent/` and
  `providers/`.

Provider failures must remain contained and sanitized. A task-scoped benchmark
or auxiliary provider does not prove that the same provider is configured or
eligible in production.

### Memory OS

HealBite Memory OS is implemented by
`gateway/platforms/healbite_memory_bridge.py` with settings and adapters under
`gateway/memory/`. It writes authoritative, user-scoped facts to SQLite,
supports SQLite FTS5/LIKE fallback, and can asynchronously upsert a derived
semantic representation to Qdrant.

This is distinct from the broader Hermes memory-provider/plugin system under
`agent/memory_*`, `tools/memory_tool.py` and `plugins/memory/`. Engineers must
name which memory subsystem a task affects.

### Qdrant

`gateway/memory/qdrant_adapter.py` provides optional semantic lookup. The
adapter filters by `user_id`, uses strict timeouts and degrades to SQLite when
unavailable. The repository Compose model defines a `qdrant` service with a
persistent volume, but runtime identity and health remain deployment facts.

Qdrant is a derived index for HealBite Memory OS, not the authority for facts.
The current rebuild path is upsert-only; equal counts do not prove identity
convergence and deleted SQLite facts can leave stale vector points.

### SQLite

SQLite has two important roles:

1. `hermes_state.py` persists Hermes sessions, history and FTS metadata.
2. The configured HealBite database persists product and Memory OS domain data
   through `gateway/healbite_*` stores and schemas.

In the production policy, the HealBite database bind is
`/var/lib/hermes/production-db/healbite.db` to
`/home/hermes/healbite.db`. That is a policy contract, not proof that a live
host currently matches it.

### Knowledge Pack

Knowledge Pack is not currently an executable subsystem. Phase 0 defines it as
the reviewable engineering knowledge layer formed by:

- this system model and `HERMES_SOURCE_MAP.md`;
- `HERMES_INVARIANTS.md` and `AI_AGENT_RULEBOOK.md`;
- `TASK_TEMPLATE.md`, ADRs, design contracts, skills and runbooks;
- the `knowledge/` indexes for architecture, decisions, failures, patterns,
  operations and AI-agent lessons;
- executable tests and versioned policies that prove the prose.

Phase 1 gives that layer a stable repository structure and populated decision
records. It still has no daemon, API, database, vector collection or production
topology.

### Engineering Control Layer

Hermes AI Engineering System v2 defines a conceptual engineering-control layer
around the existing lifecycle:

```text
Code validation
Behaviour validation
Security validation
Cost validation
Production readiness
```

These are evidence and release-decision boundaries, not product/runtime
components. The repository has a provider-free deterministic behaviour eval
engine, closed assertions/graders, human-reviewed GOLDEN corpus, and baseline
comparison. The same stdlib-only layer now has versioned executable model
recommendation/substitution receipts and deterministic usage/cost receipts
bound to external canonical rate-card identity. Release gate schema/policy
version `1` adds deterministic merge and production-release aggregation over
independent fixed-schema evidence, plus a read-only exact-head PR workflow for
the conservative merge profile. A merge PASS reports production eligibility as
`NOT_PERFORMED`; it cannot grant deployment authority. These policies make no
model or pricing network calls and cannot expand task authority. Failure-to-Eval
candidate construction and procedure-maturity evaluation are now offline,
candidate/review controls; they create no daemon, provider call path, database,
worker, graph compiler, or production topology and cannot expand authority.

### Tools

Tool modules self-register through `tools/registry.py`; `model_tools.py`
discovers tools, constructs model-visible schemas and dispatches calls.
`toolsets.py` limits exposure by platform and capability. Availability checks,
approval gates and environment backends are security boundaries, not merely
user-interface choices.

The core tool surface is intentionally narrow because every exposed schema
affects model context and prompt caching. New capability should prefer existing
code, a CLI+skill, a gated tool, plugin or MCP before a new core tool.

### Workers

The repository has several worker forms rather than one `worker` service:

- `tools/delegate_tool.py`: synchronous, isolated-context subagents bounded by
  parent lifetime and delegation policy;
- `plugins/kanban/` plus `tools/kanban_tools.py`: durable SQLite-backed task
  dispatch and scoped worker/orchestrator roles;
- `cron/`: scheduled jobs with duplicate-tick protection and bounded sessions;
- `batch_runner.py`: parallel batch execution;
- internal thread workers for transport/tool operations.

The default Compose file defines only `hermes-bot` and `qdrant`; it does not
define a separate general worker container. A design document may propose an
in-process worker for a feature, but that must not be generalized into deployed
topology without code and runtime evidence.

## System boundaries

| Boundary | Inside | Outside / separately authorized |
| --- | --- | --- |
| Hermes core | Conversation loop, session context, tool schemas/dispatch, provider transports | Product-specific policy, external services, production operations |
| HealBite domain | Controllers, services, schemas, deterministic formatting/validation | Telegram API, LLM providers, deployed database contents |
| Messaging | Platform adapters, authorization/routing, delivery formatting | Bot provider control plane, user devices, live sends/smokes |
| Durable state | SQLite records and transactional constraints | Qdrant projections, caches, logs and generated evidence |
| Semantic search | Qdrant adapter and hydrated results | Durable truth; raw Qdrant payloads are never sufficient authority |
| Release | Versioned policy, exact-main build, attestations, deploy/migration tools | Operator authority, production credentials, provider infrastructure |
| Engineering knowledge | Versioned docs, ADRs, skills, tests, Git history | Chat memory and uncommitted operator notes |
| Engineering control | Evidence contracts, offline behaviour graders/runner, GOLDEN corpus, executable model/cost policies, release aggregator, and exact-head merge CI | Production authority and live/cost/readiness evidence; a merge decision cannot authorize production |

## Runtime architecture

The repository-defined production Compose topology is:

```text
Telegram / other platforms
          |
          v
  hermes-bot container
  gateway -> session -> AIAgent -> provider transport
      |          |          |
      |          |          +-> registered tools / external APIs
      |          +-> Hermes SessionDB
      +-> HealBite services -> HealBite SQLite
                              |
                              +-> optional Qdrant derived index

  qdrant container -> persistent qdrant_data volume
```

`docker-compose.yml` defines both services on the Compose default network.
`hermes-bot` mounts Hermes home, the HealBite DB bind, backups and model cache;
`qdrant` publishes port 6333 and mounts its data volume. The production
override defines selected feature-state defaults. Exact live networks, mounts,
images and flags must be discovered read-only before an operation.

## Data flow

### Ordinary agent turn

1. A platform adapter authenticates and normalizes an incoming event.
2. Gateway routing derives a `SessionSource` and session key.
3. The gateway loads/reuses the session-scoped `AIAgent` and stable prompt/tool
   configuration.
4. The provider transport returns assistant/tool calls.
5. Tool dispatch applies registry, toolset and approval boundaries.
6. The response returns through the originating adapter and session history is
   finalized in durable state.

### HealBite deterministic flow

1. Telegram routing validates user/chat/feature scope.
2. A feature controller validates FSM or callback state.
3. Domain services read/write user- or household-scoped SQLite state.
4. LLM/Vision calls, when present, return untrusted structured candidates that
   local validators must accept before persistence or rendering.
5. The adapter formats the result; state-changing actions require explicit,
   current, owner-bound confirmation where the feature contract requires it.

### Memory recall/write

1. A normalized `user_id` scopes the request.
2. Writes commit to SQLite first.
3. An optional, non-atomic Qdrant upsert is scheduled.
4. Reads may query Qdrant, but every hit is rehydrated from user-scoped SQLite.
5. FTS5 or LIKE provides authoritative fallback when vector search is disabled,
   unavailable or yields no valid hydrated result.

## State management

| State | Authority | Derived/ephemeral forms |
| --- | --- | --- |
| Hermes conversations | `hermes_state.py` SQLite | in-process agent/session cache, rendered messages |
| HealBite product data | HealBite SQLite schemas and stores | Telegram views, generated drafts, summaries |
| Memory OS facts | SQLite `memory_os_facts` | SQLite FTS and Qdrant points |
| FSM/callback state | Feature-specific controller contract, scoped to current owner/session | Inline buttons and callback payloads |
| Runtime config | Profile-aware `config.yaml`; protected credentials from approved secret sources | environment rendered into a process/container |
| Release identity | exact Git SHA plus immutable image ID/digest and OCI revision | mutable tags, checkout branch names |
| Operational evidence | root/private evidence defined by deployment contracts | terminal summaries and chat reports |

## Trust model

- User, chat, thread, household and profile identifiers are claims until
  authorized in their proper scope.
- LLM/Vision output is untrusted input; local schema and domain validation owns
  persistence eligibility.
- SQLite is trusted only after exact-path, integrity and foreign-key checks.
- Qdrant is useful but non-authoritative and may be stale or unavailable.
- A Git branch, tag or dirty worktree is mutable; exact SHA/tree and immutable
  image identity establish release provenance.
- Secrets are bearer credentials. Their values must not enter logs, reports,
  command arguments, Git or diagnostic artifacts.
- Green CI proves the tested source contract, not production state or operator
  authorization.
- Production mutation requires explicit authority plus every technical gate;
  urgency cannot supply missing evidence.

## Failure model

The system is designed to degrade or stop at boundaries:

- Provider/network failure: mask provider details, use only configured fallback
  semantics and preserve conversation-role/prompt-cache invariants.
- Qdrant failure: fall back to SQLite; do not mutate or downgrade durable facts.
- Invalid model output: reject before persistence and request safe clarification
  or return a bounded error.
- Authorization/session mismatch: fail closed without cross-user read/write.
- Telegram polling conflict: treat as duplicate ownership; do not start another
  consumer as a probe.
- SQLite integrity, path or schema uncertainty: stop state-changing work and
  preserve the last verified rollback point.
- Image/source/secret attestation uncertainty: block release or deployment.
- Post-deploy runtime failure: use the contractually bound image rollback only
  when schema compatibility is proven; database restore is separate authority.
- Evidence-write failure: operation is not a technical PASS even when the
  apparent runtime state looks healthy.

The safe default is explicit `FAIL`, `BLOCKED`, `UNKNOWN` or `INCONCLUSIVE`,
never an inferred PASS.
