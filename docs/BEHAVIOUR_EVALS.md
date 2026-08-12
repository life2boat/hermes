# Behaviour Evals

Status: normative evaluation methodology
Scope: agent behaviour evidence for Hermes engineering tasks

## Purpose

Behaviour evals test whether an agent respected authority, scope, safety, and
the task's stop boundary. They complement code tests; they do not replace unit,
integration, security, deployment, or production-readiness evidence.

## Evaluation levels

| Level | Purpose | Typical evidence |
| --- | --- | --- |
| Component Eval | Validate one decision or boundary in isolation. | Deterministic policy input/output, tool-selection or status case. |
| Scenario Eval | Replay an end-to-end task narrative with expected actions and forbidden effects. | Versioned scenario, trace, effect ledger, final report. |
| Adversarial/Security Eval | Challenge authority, tenant isolation, secret handling, unsafe targets, and gate bypasses. | Negative corpus and proof that protected effects remain zero. |
| Production/Live Eval | Validate separately authorized behaviour that cannot be proved offline. | Sanitized live receipt bound to exact source/runtime and operator authority. |

## Deterministic-first rule

Run deterministic offline evals first. A critical PR or release gate must not
depend only on a live LLM, network availability, an external provider, or an
LLM-as-judge result. Live evals are used only when the task classification
requires them and supplies separate authority.

LLM-as-judge is `SUPPLEMENTAL_ONLY` for critical safety decisions. It may help
triage or compare qualitative outputs, but cannot be the sole evidence for
authority, secret safety, tenant isolation, destructive-action safety, or a
production release gate.

## Golden datasets

Golden datasets must be versioned, sanitized, and human reviewed. They should
include:

- expected successful outcomes;
- real sanitized failure cases;
- negative and malformed-input cases;
- authority and scope boundaries;
- stop-boundary cases;
- security, secret, and private-data cases;
- explicit statuses for missing or inconclusive evidence.

The same evaluated model must not generate the entire golden dataset and also
act as its sole judge. Human-reviewed expectations and deterministic assertions
own critical outcomes.

## Minimum eval-case contract

The schema-versioned scenario and replay substrate in ai_engineering binds the
following minimum case fields:

```text
case_id
dataset_version
task_classification
canonical_source_or_fixture_version
required_behaviour_dimensions
allowed_effect_classes
forbidden_effect_classes
expected_stop_boundary
expected_status
sanitized_input_reference
deterministic_assertions
```

The result should record the evaluated implementation/model identity, exact
case version, observed effect classes, assertion results, and sanitized evidence
references. A result is not reusable after its source, dataset, or required
contract changes unless replay proves compatibility.

## Gate interpretation

- Required deterministic cases all PASS: the offline behaviour requirement may
  pass.
- A required case FAILS: the behaviour gate fails.
- A required case is missing, not run, or inconclusive: the behaviour gate is
  closed.
- Optional qualitative evidence is unavailable: report it separately; do not
  overwrite deterministic PASS or FAIL.

The task classification determines which levels are required. A trivial docs
change need not run production/live evals. A high-risk production operation may
require all applicable offline, security, and separately authorized live
evidence.

## Privacy and reproducibility

Fixtures and traces must contain no secrets, private production data, raw user
messages, health data, or raw provider payloads. Replace sensitive identity
with deterministic classifications and use fixed-schema receipts. Eval tooling
must preserve enough provenance to reproduce the decision without preserving
the sensitive interaction.

## Corpus content identity and review

`corpus_digest` identifies immutable behavioural content. Its canonical input
is the manifest projection `schema_version`, `dataset_version`, and `datasets`,
plus all referenced dataset records and trace fixtures. `corpus_status` and the
baseline pointer remain strictly validated but are excluded because they are
mutable lifecycle/evaluation metadata rather than behavioural content.

Human approval binds dataset version, corpus digest, eval engine version, and
the candidate reviewed PR head. The head is an audit anchor. Approval remains
applicable after a promotion-only metadata commit only when the final corpus
digest equals the reviewed digest. Any change to scenarios, expected outcomes,
criticality, required graders/assertions, dataset membership/configuration, or
trace evidence changes the digest and requires a new human review.

## Implementation state

The closed trace/scenario schemas, sanitization boundary, canonical
serialization, immutable behavioural-content digest, safe fixture loading,
provider-free replay, closed assertion registry, deterministic graders, offline
eval runner, approval-applicability check, and baseline comparison are
implemented in ai_engineering. The scenario contract reads schema v1 without
reinterpretation and requires
canonical_source_or_fixture_version in schema v2.

The committed evals/agent_behaviour corpus is versioned and sanitized, but its
state is GOLDEN following human review bound to its exact dataset version,
corpus digest, engine version, and candidate reviewed head in CORPUS_REVIEW.md.
Technical CI did not self-certify it as golden. Executable model/cost policy,
release aggregation and CI behaviour gating are implemented. Failure evidence
can now produce only a deterministic `CANDIDATE` outside the Golden corpus;
human review, dataset change, eval, PR, CI and merge remain required for any
promotion. Candidate automation cannot self-certify GOLDEN content or mutate
production policy.
