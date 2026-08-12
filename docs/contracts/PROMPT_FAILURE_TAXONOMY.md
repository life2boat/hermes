# Prompt Failure Taxonomy

Status: normative classification vocabulary

| Class | Meaning | Smallest normal repair |
| --- | --- | --- |
| `CONTEXT_MISSING` | Required authoritative/current context is absent. | Add or refresh the exact context source; otherwise block. |
| `INSTRUCTION_MISSING` | The requested operation is missing or ambiguous. | Rewrite the immediate task or add an operational instruction. |
| `ORDERING_ERROR` | A multi-step task lacks a safe deterministic order. | Correct the `execution_order` field. |
| `AMBIGUOUS_CONSTRAINT` | Constraints are vague or contradictory. | Resolve the conflicting `PromptSpec` constraint. |
| `MISSING_EXAMPLE` | A reviewed failure needs a concrete pattern. | Add one curated sanitized few-shot regression. |
| `OUTPUT_CONTRACT_ERROR` | Machine-consumed output lacks a compatible schema. | Add/fix the typed output contract. |
| `TOOL_POLICY_ERROR` | Trust or tool boundaries are missing or unsafe. | Correct the trusted/untrusted or authorization contract. |
| `MODEL_CAPABILITY_LIMIT` | The selected model/provider lacks a required capability. | Select a declared compatible capability or change the output design. |

The mapping is deterministic in `classify_prompt_failure`. Classification is
not authority to rewrite a prompt, promote a corpus, change a model, call a
provider, or mutate runtime policy.

## Failure to regression loop

```text
sanitized reproducible failure
-> taxonomy classification
-> root-cause PromptSpec field
-> smallest reviewed repair
-> deterministic fixture
-> prompt eval
-> normal PR / CI / merge
```

A candidate generated from a failure cannot self-approve. If an example is the
smallest effective repair, it must be sanitized, versioned, reviewed, and kept
only while its regression remains relevant.
