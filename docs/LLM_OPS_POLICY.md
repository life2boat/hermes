# LLM Ops Policy

Status: normative model-selection and budget policy
Scope: AI-assisted Hermes engineering work

## Principles

Model selection is a versioned task-policy decision, not an agent preference.
The selected model tier must match task complexity and risk, but:

```text
model recommendation != authority
```

A more capable model never expands the task's allowed scope, production
authority, secret access, destructive permissions, or stop boundary.

## Initial engineering matrix

| Task class | Recommended tier | Reasoning |
| --- | --- | --- |
| Repository search / logs | GPT-5.6 Terra | medium/high |
| Small precise fix | GPT-5.6 Terra or GPT-5.5 medium | medium/high |
| Bounded implementation | GPT-5.6 Sol | medium/high |
| Architecture | GPT-5.6 Sol | high |
| Migration / rollback design | GPT-5.6 Sol | high |
| Security audit | GPT-5.6 Sol | high |
| High-risk production deployment or migration | GPT-5.6 Sol Ultra | high |

This matrix is a repository engineering recommendation. Actual model
availability is an execution-environment fact and must not be invented. If the
recommended tier is unavailable, the task must explicitly classify the
substitution and preserve every safety gate.

## Classification and selection

Selection should consider:

- scope breadth and architectural coupling;
- reversibility and production/data risk;
- security and authority complexity;
- need for exact cross-document consistency;
- expected tool use and external side effects;
- deterministic validation available after the work.

Use the least costly tier that can reliably satisfy the contract. Escalation is
appropriate when discovery proves higher complexity or risk; it does not waive
the need to revalidate scope and budgets.

## Budget dimensions

Tasks may bind any of these versioned limits:

```text
max_model_calls
max_input_tokens
max_output_tokens
max_estimated_cost
```

Provider pricing must not be hardcoded into this normative contract. A future
cost evaluator should consume a versioned pricing source or operator-supplied
rate card, bind its identity in evidence, and report `UNKNOWN` or
`INCONCLUSIVE` when a required estimate cannot be made trustworthy.

Budget accounting must distinguish primary calls, retries, judge calls,
fallbacks, and live-eval calls. Hidden retries or cross-provider fallback may
not be excluded from required cost evidence.

## Safety and fallback

- Model fallback must preserve the same authority, privacy, and stop boundary.
- A fallback that changes provider, data disclosure, tool access, or cost class
  requires an explicit policy decision.
- Secrets and raw prompts must not enter model-selection or cost receipts.
- A budget overrun closes a required cost gate; urgency cannot convert it to
  PASS.
- An unavailable recommended model is not permission to use an unreviewed
  provider or credential source.

## Evidence

A future model-policy receipt should bind task classification, selected tier,
reasoning level, substitution (if any), budgets, observed usage, pricing-source
identity when cost is estimated, and final status. The executable selector and
cost evaluator are `PLANNED` for later PRs.
