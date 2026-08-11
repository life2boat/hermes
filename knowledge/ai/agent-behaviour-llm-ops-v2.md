# Agent Behaviour and LLM Ops v2

Hermes AI Engineering System v2 extends the existing task lifecycle with
reviewable behaviour, model, security, cost, and release semantics. It is a
repository contract layer, not a runtime service or hidden agent memory.

Authoritative sources:

- [Agent Behaviour Contract](../../docs/AGENT_BEHAVIOUR_CONTRACT.md) defines
  correct agent conduct independently of code success.
- [Behaviour Evals](../../docs/BEHAVIOUR_EVALS.md) defines deterministic-first
  evaluation methodology and golden-dataset safety.
- [LLM Ops Policy](../../docs/LLM_OPS_POLICY.md) defines task-based model and
  budget concepts.
- [Agent Release Gates](../../docs/AGENT_RELEASE_GATES.md) separates merge
  eligibility from production release eligibility.
- [Skill -> Loop -> Graph Lifecycle](../../docs/SKILL_LOOP_GRAPH_LIFECYCLE.md)
  defines procedure maturity and governed improvement.

Durable decisions are recorded in
[ADR-0074](../../docs/adr/ADR-0074-agent-behaviour-contract.md),
[ADR-0075](../../docs/adr/ADR-0075-behaviour-evals-release-gates.md),
[ADR-0076](../../docs/adr/ADR-0076-llm-ops-model-policy.md), and
[ADR-0077](../../docs/adr/ADR-0077-governed-agent-improvement.md).

PR-1 established the architecture and contracts. PR-2 implemented the
stdlib-only ai_engineering trace/replay substrate. PR-3 adds compatible
scenario schema v2, deterministic graders, a closed assertion registry, an
offline eval runner/CLI, stable reports, a versioned 49-case corpus, and a
digest-bound baseline.

The corpus is GOLDEN / HUMAN_REVIEW=PASS. Its review evidence binds the dataset
and immutable corpus identity, engine version, and candidate reviewed head;
promotion-only metadata may change the PR head only while that digest remains
identical. Executable model/cost policy, release-gate aggregation, CI behaviour
gating, and failure-candidate automation remain planned. The package is a
repository library, not product runtime capability or production activation.
