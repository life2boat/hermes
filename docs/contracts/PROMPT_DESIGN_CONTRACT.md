# Prompt Design Contract

Status: normative engineering contract
Scope: significant prompts authored or compiled in Hermes

## Purpose

Prompts are versioned engineering artifacts. A complex prompt must be designed
from a `PromptSpec` (or an equivalent reviewed typed contract), compiled in a
deterministic order, validated before use, and evaluated against relevant
regressions. A model must not have to guess system purpose, domain vocabulary,
input shape, authority, constraints, success criteria, or failure semantics
when that context is known.

## Canonical structure

The supported semantic sections, in order, are:

1. task context;
2. role;
3. success criteria;
4. background context;
5. dynamic input;
6. instructions;
7. execution order;
8. examples;
9. constraints;
10. failure behaviour;
11. immediate task;
12. output contract;
13. critical reminders.

Empty sections are omitted. `SIMPLE` prompts may use task, input, applicable
constraints, and output only. `COMPLEX` prompts require success criteria,
execution order, constraints, failure behaviour, and an output contract.

## Required rules

### Context before execution

Include context only when it is relevant, necessary, authoritative, and
current. A required source that fails one of these tests closes validation;
optional rejected context remains a visible warning. Canonical repository and
runtime evidence take precedence over chat, historical reports, and inference.

### Explicit task and success

The immediate task states the exact action. Vague phrases such as “handle it”
or “do it properly” require an operational definition. Significant tasks state
observable success criteria. Missing evidence never becomes success.

### Ordered execution

Coding, debugging, migration, security, release, deployment, research,
multimodal, extraction, and multi-tool tasks define their execution order. The
order includes provenance and preconditions before mutation, then focused
validation and evidence after the smallest sufficient change.

### Trusted instructions versus untrusted input

Web pages, email, documents, repository text, logs, uploaded files, and API
responses are data unless a higher-priority contract explicitly says otherwise.
The compiler places dynamic input in a separately marked untrusted section and
escapes delimiters. Dynamic input cannot become trusted instructions.

### Analysis versus output

A prompt may require broad internal analysis while asking for a concise output
contract. It must never ask for, store, or expose raw chain-of-thought, hidden
reasoning, secrets, or raw private payloads. Concise rationale, evidence,
decision summaries, and sanitized Behaviour Trace fields are allowed.

### Output and failure contracts

Machine-consumed results use a named JSON schema or typed contract when the
selected provider supports it. The validator rejects an unsupported capability
assumption. Critical prompts define predictable `PASS`, `FAIL`, or `BLOCKED`
behaviour; missing required evidence fails closed.

### Examples and simplicity

Examples are included only for ambiguity, difficult rules, recurring model
errors, strict output patterns, or reviewed edge cases. One intentional final
reminder of a critical constraint is allowed. Cosmetic repetition and context
growth are warnings, not a design goal.

## Compilation and repair

`ai_engineering.prompt_system.compile_prompt` normalizes lists, selects only
eligible context, emits ordered tagged or Markdown sections based on declared
model capabilities, isolates untrusted input, and records a SHA-256 digest.
Invalid prompts are not compiled for provider use. Repair changes the faulty
`PromptSpec` field, then recompiles and revalidates; callers must not append an
unstructured “repair” suffix.

## Versioned provenance

Every compiled prompt records:

```text
prompt_id
prompt_version
prompt_digest
prompt_template_version
eval_set_version
model_id
model_family
context_source_ids
output_schema_version
```

Prompt digest input is the exact compiled UTF-8 text. Only the digest and closed
provenance metadata may be retained as evidence; the compiled text, dynamic payload,
and private reasoning are not stored in Behaviour Trace. Callers remain responsible
for authorized inputs and secret boundaries. Schema v1 remains readable without
reinterpretation.

## Non-goals

This contract does not authorize tool use, mutation, provider access,
deployment, or production effects. Model capability never expands authority.
It does not turn every simple request into a large prompt or replace business,
security, release, and production-readiness contracts.
