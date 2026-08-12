# Prompt Quality Corpus Review

```text
DATASET_VERSION=prompt-quality-v1
CORPUS_STATUS=CANDIDATE
CORPUS_DIGEST=d52adea60862ad5ca2b71a23dfd506adc02ca8dcb3b6270ab79a51bc949c86ea
EVAL_ENGINE_VERSION=1
TECHNICAL_EVAL=PASS
HUMAN_REVIEW=NOT_PERFORMED
```

The eight expected outcomes are deterministic and sanitized. CI may prove
their technical conformance, but it must not treat PR approval or an agent's
own output as digest-bound human Golden review. Promotion to `GOLDEN` requires
an explicit review of this exact dataset version and corpus digest in a later
authorized repository change; it never authorizes provider or production
activity.
