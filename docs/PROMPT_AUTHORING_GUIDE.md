# Prompt Authoring Guide

Use this guide after the task lifecycle has established exact source,
authority, scope, and stop boundary. The normative rules live in
[`contracts/PROMPT_DESIGN_CONTRACT.md`](contracts/PROMPT_DESIGN_CONTRACT.md).

## Workflow

1. Identify the measurable outcome, result consumer, required evidence,
   constraints, and failure conditions.
2. Select only relevant, necessary, authoritative, current context.
3. Put external/user-derived material in `PromptInputBlock(trusted=False)`.
4. Classify the prompt as `SIMPLE` or `COMPLEX`.
5. Build a typed `PromptSpec`; do not concatenate an ad hoc monolith.
6. Declare actual model capabilities through the existing provider/model
   abstraction; never infer support from a historical provider name.
7. Run `validate_prompt` and `lint_prompt`.
8. Repair the source `PromptSpec` field if validation fails.
9. Compile once validation passes and record its provenance digest.
10. Run the relevant offline prompt and behavioural regressions.

## Minimal example

```python
from ai_engineering.prompt_contracts import (
    ModelCapabilities,
    OutputMode,
    PromptComplexity,
    PromptFormat,
    PromptOutputContract,
    PromptSpec,
)
from ai_engineering.prompt_system import compile_prompt

spec = PromptSpec(
    schema_version=1,
    prompt_id="bounded-repair",
    prompt_version="1.0.0",
    template_version="prompt-template-v1",
    eval_set_version="prompt-quality-v1",
    complexity=PromptComplexity.COMPLEX,
    task_context="Repair one reproduced repository defect.",
    role=None,
    success_criteria=("The focused regression passes.",),
    background_context=(),
    dynamic_input=(),
    instructions=("Change only the affected implementation.",),
    execution_order=("Inspect.", "Reproduce.", "Patch.", "Validate."),
    examples=(),
    constraints=("Do not deploy.",),
    failure_behaviour=("Missing evidence returns STATUS=BLOCKED.",),
    immediate_task="Fix only the reproduced defect.",
    output_contract=PromptOutputContract(
        mode=OutputMode.TEXT,
        schema_id=None,
        instructions=("Return status, root cause, validation, and risks.",),
    ),
    critical_reminders=(),
)

capabilities = ModelCapabilities(
    model_id="reviewed-model-id",
    model_family="reviewed-model-family",
    supports_structured_output=True,
    supports_tool_calls=True,
    supports_vision=False,
    supports_extended_reasoning=True,
    supports_prefill=False,
    context_limit=128000,
    preferred_prompt_format=PromptFormat.TAGGED,
)
compiled = compile_prompt(spec, capabilities)
```

The caller sends `compiled.text`, not the spec when validation failed. It may
record `compiled.provenance` in sanitized evidence. It must not persist raw
chain-of-thought, secrets, or private source payloads.

## Self-review

- Is the exact task unambiguous?
- Is success observable?
- Is selected context sufficient but not excessive?
- Are trusted instructions separated from untrusted data?
- Is execution order safe and explicit where required?
- Are constraints, failure semantics, and output schema concrete?
- Are conflicting rules and unsupported capability assumptions absent?
- Can deterministic tests verify the result without exposing private content?
