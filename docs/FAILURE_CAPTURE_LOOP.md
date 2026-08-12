# Failure Capture Loop

Status: normative post-incident workflow
Scope: serious repository, release, safety, data-integrity, or production incidents

This loop turns a serious incident into a sanitized, evidence-bound improvement.
It does not authorize production mutation, credential handling, broad log
collection, or a bypass of a technical safety gate.

## Trigger

Start the loop after a safety-relevant incident, a data-integrity concern, a
release/deployment failure, an unexpected production behavior, or a recurring
CI/runtime failure whose cause may affect users. Do not invent an incident from
an unverified report; classify unsupported claims as `UNKNOWN` or
`INCONCLUSIVE`.

## Required loop

1. **Protect the boundary.** Follow the current task and applicable skill to
   contain risk without deleting evidence, weakening a gate, printing secrets,
   or making an unapproved production change.
2. **Preserve sanitized evidence.** Record only reproducible facts: canonical
   revision, timestamps, deterministic command status, component boundary, and
   non-sensitive error class. Never store secret values, user data, raw
   production logs, or correlation identifiers in Git.
3. **Establish root cause.** Trace the failing path through source, tests,
   versioned policy, and current evidence. Separate the symptom, contributing
   conditions, root cause, and unresolved hypotheses. A plausible explanation
   is not a root cause without supporting evidence.
4. **Capture the reusable lesson.** Add a sanitized record to
   [`knowledge/failures/`](../knowledge/failures/) using its documented cause,
   resolution, and lesson structure. Link the record to the authoritative test,
   policy, skill, runbook, or commit that proves the conclusion.
5. **Record a decision when needed.** Create or update an ADR when the incident
   changes a lasting architectural decision. Update a procedural skill or
   runbook when it changes an operational rule. Avoid copying the same procedure
   into multiple passive documents.
6. **Harden and validate.** Add a focused regression or contract test when it
   can deterministically prevent recurrence. Run proportional repository checks
   and retain the same fail-closed boundary for any production follow-up.
7. **Close with evidence.** State what is fixed, what remains `UNKNOWN` or
   `INCONCLUSIVE`, and the next authorized action. Do not close an incident
   solely because a workaround hid the symptom.

## Candidate boundary

A sanitized, repository-bound failure may be converted to an offline candidate
with `scripts/build_failure_eval_candidate.py`. The builder verifies its
failure record and trace identity but creates no Golden member, policy change,
graph, runtime action, or production mutation. Review, dataset change, eval,
PR, CI, and merge remain separate required lifecycle steps.

## Relationship to the task lifecycle

The incident loop feeds Step 8 of [`TASK_LIFECYCLE.md`](TASK_LIFECYCLE.md).
The next remediation task still begins with `TASK_TEMPLATE.md` and
`scripts/prepare_task.py`; failure knowledge informs the task but never expands
its authority.

## Minimum failure record

Each serious incident record should contain:

- scope and impact without private data;
- evidence-backed cause and resolution status;
- a clear lesson and the contract that now enforces it;
- links to the relevant test, ADR, skill, policy, runbook, or canonical commit;
- open questions and the next authorized action, if any.
