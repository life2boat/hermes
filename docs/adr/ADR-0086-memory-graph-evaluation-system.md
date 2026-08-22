# ADR-0086: Memory Graph Evaluation System

## Problem
We need an offline, deterministic evaluation system to verify Memory Graph v3 (retrieval, convergence, privacy, isolation, etc) without requiring live LLM calls, production data, or Qdrant.

## Decision
We implemented `ai_engineering/memory_graph_eval.py` to run offline, synthetic scenarios defined in `evals/memory_graph/`. This engine uses an ephemeral SQLite database (ephemeral DB) to evaluate exact retrieval semantics, isolation, freshness, transaction bounds, and convergence integrity without side effects.

The evaluation system relies on the following design decisions:
- **Strict Parsing & Manifest**: We use a strict manifest to define the corpus. The JSON parser is strict, including duplicate-key rejection and rejection of NaN/Infinity values.
- **Semantic Corpus Digest**: A deterministic corpus digest is generated over the semantic content.
- **Review-State Separation**: The engine runs independently from human review workflows.
- **Static Oracle**: Expected fixture outcomes are independent of the system under test (static oracle).
- **SHA-256 Report ID**: A deterministic SHA-256 report ID is computed over a safe report payload, removing non-deterministic elements (UUID, timestamps, paths).
- **Safe Report**: The evaluation engine generates a safe report with a well-defined `PASS`/`FAIL`/`BLOCKED` contract.
- **Deterministic Race Harness**: Concurrency/race conditions are modeled using a deterministic race harness (without timing dependencies).
- **No External Dependencies**: The evaluation requires no Qdrant, no provider calls, and no network access.
- **Lifecycle & Activation**: The current corpus is in a CANDIDATE lifecycle state, and human review is not performed yet. Furthermore, PR-7 integration and runtime activation are deferred to a later phase.

## Status
Accepted

## PR-6.1 verification
PR #204 was squash-merged as `db9620284cac8e1fe6af6c8420a1dfbf8194a557`. On that exact canonical main tree, the candidate corpus digest was `6c5df3e4f152cbc48d3b674a37ed5935a62bd327ca1eda2790d9770d9ad92bda`; two runs passed with byte-identical reports and report IDs. Same-revision support-value tamper is verified through the real graph-read hydration path as a hard integrity failure. This does not activate graph runtime, providers, Qdrant, or production access.
