# Hermes / HealBite Task Lifecycle

Status: normative repository workflow
Scope: every repository, release, or operational task

This lifecycle turns an idea into a reviewable change without treating a plan,
chat transcript, or a green local command as authority for a later phase. It
complements the current task, `AGENTS.md`, the AI Agent Rulebook, and applicable
procedural skills; those sources retain their precedence.

## 1. IDEA

State the measurable outcome, affected users or systems, and why the work is
needed. Classify unverified context as historical, planned, unknown, or
inconclusive instead of presenting it as current fact.

## 2. TASK TEMPLATE

Start from [`TASK_TEMPLATE.md`](TASK_TEMPLATE.md). Bind the task to its
canonical remote and base, allowed and forbidden mutations, affected
invariants, required evidence, and its exact stop boundary. If those boundaries
cannot be stated, the next step is discovery rather than implementation.

## 3. PREPARE TASK CONTEXT

Before discovery, design, or editing in the target worktree, run:

```bash
python scripts/prepare_task.py --output .task_context/task-context.json
```

Read the generated package. Verify its Git head and branch, review changed
files, and confirm its tracked-document hashes. The package is local evidence,
is intentionally ignored by Git, and its pytest-cache classification cannot
prove a test PASS. If preparation fails or required documentation is absent,
stop as `BLOCKED`.

## 4. AI IMPLEMENTATION

Discover the current entry point, durable boundary, tests, ADRs, skills and
state records before selecting the least-mutating solution. Work only in a
clean isolated worktree, implement the smallest coherent change, preserve
invariants, and update current-state or decision records when confirmed facts
change. Follow a domain skill for deployment, memory, or Telegram work.

## 5. AI REVIEW CHECKLIST

Apply [`AI_REVIEW_CHECKLIST.md`](AI_REVIEW_CHECKLIST.md) to the final diff.
Confirm that implementation and tests support the claim, that the system model
and invariants still hold, and that no unrelated change or private material is
included. A checklist result records evidence; it does not waive a failed
technical gate.

## 6. PRODUCTION READINESS CHECKLIST

For release or production-operation work, apply
[`PRODUCTION_READINESS_CHECKLIST.md`](PRODUCTION_READINESS_CHECKLIST.md) before
the first authorized mutation. Its provenance, backup, migration, rollback and
health gates are fail-closed. For a repository-only task, record this step as
`NOT_APPLICABLE`; never simulate it with a local source checkout.

## 7. MERGE

Deliver exactly to the task's requested boundary: local diff, commit, Draft PR,
ready PR, or merge. Recheck exact-head CI, mergeability and conflicts when the
task requires them. A Draft-PR stop boundary forbids merging; a merge boundary
does not implicitly authorize a build, deployment, or smoke test.

## 8. KNOWLEDGE UPDATE

Record durable, confirmed learning in its canonical place:

- update `docs/CURRENT_STATE.md` and its changelog when confirmed project state
  changes;
- add an ADR for a lasting architectural decision;
- add or refine a procedural skill for an operational rule;
- add a sanitized failure record through
  [`FAILURE_CAPTURE_LOOP.md`](FAILURE_CAPTURE_LOOP.md) after a serious incident.

Do not duplicate an authoritative procedure in passive documentation. Link to
the source that owns the contract instead.

## v2 behaviour and LLM Ops extension

Hermes AI Engineering System v2 extends, rather than replaces, the lifecycle.
Task classification determines which additional controls are required:

```text
Task
-> prepare_task
-> behaviour/model classification
-> implementation
-> code validation
-> behaviour eval
-> security gate
-> cost gate
-> AI review
-> production readiness
-> merge/release
-> failure/eval feedback
```

For a complex model-facing task, the implementation phase also follows the canonical
Prompt Engineering workflow: select authoritative current context, construct a
versioned `PromptSpec`, isolate dynamic input, compile for declared capabilities, run
the validator/linter, and execute the relevant prompt-quality regressions. A malformed
prompt is repaired at its PromptSpec source and is not sent or patched with arbitrary
trailing text.

The authoritative semantics live in
[`AGENT_BEHAVIOUR_CONTRACT.md`](AGENT_BEHAVIOUR_CONTRACT.md),
[`BEHAVIOUR_EVALS.md`](BEHAVIOUR_EVALS.md),
[`LLM_OPS_POLICY.md`](LLM_OPS_POLICY.md), and
[`AGENT_RELEASE_GATES.md`](AGENT_RELEASE_GATES.md). The executable behaviour
runner, model/cost policy, release gate and exact-head CI are repository
controls. Failure-to-Eval candidates and procedure-maturity receipts are also
offline evidence only: they require review, eval, PR, CI and merge before a
separately authorized repository change can promote anything.

## Intent Control Plane path

For tasks where `INTENT_CONTROL_PLANE=REQUIRED` or `OPTIONAL`, the Intent Control Plane integrates deterministic offline intent bounding and evidence convergence:

```text
IDEA
→ TASK TEMPLATE
→ TaskIntent
→ prepare_task --intent
→ Clarify
→ Requirements Quality Gate
→ Design / TaskLineage
→ Cross-Artifact Analyze
→ Implement
→ EvidenceBundle
→ Evidence-Bound Convergence
→ Effective Policy / Source Attribution
→ AI Review / Agent Release Gate
→ Merge / Release boundary
→ Knowledge Update
```

### Classification guidance

- `INTENT_CONTROL_PLANE=REQUIRED`: Architecture, bounded multi-step implementation, security-sensitive work, production-sensitive engineering preparation, migrations / rollback design, tasks with multiple acceptance criteria or required release gates.
- `INTENT_CONTROL_PLANE=OPTIONAL`: Small precise implementation where lineage adds useful traceability.
- `INTENT_CONTROL_PLANE=NOT_APPLICABLE`: Trivial passive documentation or typo-only changes with no behavioral contract.

### Lineage and Graph Contract (v1)

- `TaskIntent` directly owns and defines `acceptance_criteria`.
- `TaskLineage` models traceability across four node kinds: `INTENT`, `CRITERION`, `TASK`, `EVIDENCE`.
- Valid edge relations:
  - `TASK -[IMPLEMENTS]-> CRITERION`
  - `EVIDENCE -[VERIFIES]-> CRITERION`
  - `EVIDENCE -[VERIFIES]-> TASK`
- Note on Lineage v1: There is no direct `INTENT -> CRITERION` edge relation in `TaskLineage` because acceptance criteria are intrinsic children of the `TaskIntent` schema.

### Canonical Construction & Identity Binding

- **Clarification**: `clarification_id` is content-addressed via `compute_clarification_id(intent_digest, questions, schema_version)`.
- **Requirements Quality Review**: `review_id` is content-addressed via `compute_requirements_review_id(task_id, intent_digest, intent_revision, reviewer_id, criterion_reviews, global_reviews, schema_version)`. Reviews MUST be instantiated via `create_requirements_quality_review(...)`.
- **Evidence Bundle**: Observations and bundles MUST be constructed via public factory helpers `create_evidence_observation(...)` and `create_evidence_bundle(...)` with deterministic content-addressed hashes.

### Authority semantics

Deterministic evidence and validation gates must never be confused with authority:

- `TaskIntent != production authority` (structured intent bounds execution; it does not grant deployment or runtime mutation rights)
- `Requirements Gate != implementation proof` (passing requirements gate proves intent readiness, not code correctness)
- `Cross-Artifact Analysis != implementation proof` (structural lineage consistency does not prove tests pass)
- `Convergence != production authority` (evidence convergence proves engineering criteria and gates passed; it does not authorize production)
- `Effective Policy != authorization` (resolving policy sources and attribution provides explainability without expanding authority)
- `Agent Release Gate != deployment authority` (merge qualification does not authorize production deployment)
- `Production readiness remains separately required when applicable.`

## Completion evidence

A task is complete only when its final report distinguishes completed work from
`NOT RUN`, `NOT PERFORMED`, `UNKNOWN`, or `INCONCLUSIVE` work; names the base,
branch, worktree, validations and remaining risk; and stops at the authorized
boundary.
