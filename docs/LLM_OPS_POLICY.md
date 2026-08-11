# LLM Ops Policy

Status: normative model-selection and budget policy
Scope: AI-assisted Hermes engineering work

## Executable contract

The repository implements this policy in `ai_engineering/model_policy.py` and
`ai_engineering/cost_policy.py` with these independent identities:

```text
MODEL_POLICY_VERSION=1
COST_POLICY_VERSION=1
RATE_CARD_SCHEMA_VERSION=1
```

`scripts/check_llm_ops_policy.py` exposes provider-free `recommend`,
`evaluate-model`, `evaluate-cost`, and combined `evaluate` modes. Exit codes
are `0=PASS`, `1=FAIL`, `2=BLOCKED`, and `3=unexpected internal failure`.

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

Provider pricing is not hardcoded into this normative contract or Python
implementation. The cost evaluator consumes a versioned, externally supplied
rate card, binds its canonical SHA-256 identity and pricing-source identifier
in evidence, uses deterministic decimal-string arithmetic, and reports
`UNKNOWN`, `NOT_PERFORMED`, or
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

The typed model-policy receipt binds task classification, selected tier,
reasoning level, substitution (if any), authority-preservation status, and
selection status. The separate cost receipt binds budgets, observed usage,
pricing-source identity when cost is estimated, and dimensional status. The
aggregate preserves both receipts without becoming a release gate. Receipts
are sanitized and
canonically serialized; they contain no raw prompt, user message, provider
response, credential, environment dump, or chain-of-thought.

The closed task taxonomy is:

```text
repository_search_logs
small_precise_fix
bounded_implementation
architecture
migration_rollback_design
security_audit
high_risk_production_deployment_or_migration
```

Unknown task classes and required missing evidence are `BLOCKED`, not mapped to
a generic default. Substitution is explicit (`NONE`, `ALLOWED_ALTERNATIVE`,
`ESCALATION`, `FALLBACK`, or `PROVIDER_CHANGE`); provider/security-changing
substitution requires approved compatibility evidence. A stronger model or
higher reasoning level never changes authority.

The cost receipt keeps call, input-token, output-token, and estimated-cost
dimensions separate. Every primary, retry, judge, fallback, and live-eval call
counts. Proven overrun is `FAIL`; required unknown usage, pricing identity,
model rate, or compatible currency is `BLOCKED`. An unavailable optional price
estimate cannot become numeric zero. The aggregate `LLMOpsReceipt` combines
only model and cost policy results; it is not the release gate. Release-gate
aggregation remains a separately governed later component.
