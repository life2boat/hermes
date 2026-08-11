# Skill -> Loop -> Graph Lifecycle

Status: normative procedure-maturity lifecycle
Scope: agent procedures and deterministic workflow promotion

## Lifecycle

```text
New task
  -> SKILL or natural-language procedure
  -> Agent Loop
  -> Sanitized traces
  -> Failures and successful cases
  -> Eval Dataset
  -> Repeated stable behaviour
  -> Deterministic Graph or code workflow
  -> Regression suite
```

A procedure is not ready for graph hardening merely because it exists. Graph
promotion is justified by evidence that the intent and sequence have become
stable enough to encode without hiding unresolved judgement or authority.

## Stages

1. **Procedure candidate.** State intent, authority, stop boundary, expected
   evidence, and known safety invariants in a skill or task procedure.
2. **Agent loop.** Execute the procedure under bounded tasks and preserve only
   sanitized, reviewable outcomes.
3. **Eval corpus.** Turn repeated success and failure cases into human-reviewed
   deterministic scenarios, including negative and authority cases.
4. **Maturity review.** Demonstrate stable intent, sequence, failure handling,
   side effects, and required evidence.
5. **Graph/code candidate.** Encode deterministic steps, branches, schemas, and
   recovery behaviour in a reviewed repository change.
6. **Regression ownership.** Bind the graph to tests, versioning, observability,
   and a rollback/deprecation path.

## Graph-candidate maturity criteria

Promotion requires evidence for all applicable criteria:

```text
stable intent
stable sequence
known failure modes
adequate regression corpus
side effects understood
authority boundary known
```

The review must also identify which decisions remain agent judgement and must
not be frozen into a graph. Missing evidence leaves the procedure at its
current stage; it is not a reason to invent deterministic certainty.

## Authority and side effects

Graph hardening does not expand authority. The graph must revalidate dynamic
preconditions at execution time and retain the same fail-closed behaviour for
secrets, identity, durable data, external calls, production mutation, and stop
boundaries. A successful replay from old evidence does not prove current
production readiness.

## Governed self-improvement

Agent-generated improvements follow:

```text
failure
  -> sanitized trace
  -> candidate improvement
  -> eval
  -> PR
  -> CI and review
  -> merge
```

Until the repository lifecycle passes, the improvement is `CANDIDATE`. The
forbidden path is:

```text
failure
  -> agent rewrites production policy
  -> direct production activation
```

An agent may propose a skill edit, dataset case, graph change, or policy update,
but may not directly mutate production policy or activate the candidate.

## Evidence and implementation state

Future graph candidates should bind the source procedure version, eval dataset,
required invariants, allowed effect classes, failure branches, regression
suite, and promotion approval. This PR defines the lifecycle only; a graph
compiler, trace store, and automatic promotion mechanism are not implemented.
