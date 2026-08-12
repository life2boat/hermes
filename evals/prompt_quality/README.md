# Prompt Quality Corpus

This directory contains the provider-free deterministic Prompt Engineering
regression corpus. `manifest.json` binds the dataset and `cases.jsonl` contains
only synthetic, sanitized PromptSpec fixtures and closed expected diagnostics.

Run it with:

```bash
python scripts/run_prompt_quality_evals.py
```

The corpus covers repository repair, ambiguity, missing evidence, structured
output, untrusted prompt injection, multi-step execution, failure handling,
and a historical contradictory-instruction regression. A technical PASS does
not self-promote lifecycle metadata: `CORPUS_REVIEW.md` remains authoritative
for CANDIDATE versus GOLDEN review state.
