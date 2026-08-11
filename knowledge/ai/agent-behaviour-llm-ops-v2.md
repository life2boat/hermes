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

PR-1 establishes these contracts only. The behaviour trace/replay format,
eval runner, release-gate aggregator, CI behaviour gate, and cost evaluator are
planned follow-up implementation, not current runtime capability.
