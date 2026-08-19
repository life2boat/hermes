# ADR-0086: Memory Graph Evaluation System

## Problem
We need an offline, deterministic evaluation system to verify Memory Graph v3 (retrieval, convergence, privacy, isolation, etc) without requiring live LLM calls, production data, or Qdrant.

## Decision
We implemented `ai_engineering/memory_graph_eval.py` to run offline, synthetic scenarios defined in `evals/memory_graph/`. This engine uses an ephemeral SQLite database to evaluate exact retrieval semantics, isolation, freshness, transaction bounds, and convergence integrity without side effects.

The corpus is strictly separated from `evals/agent_behaviour/` as it evaluates deterministic API contracts, not LLM behaviour.

## Status
Accepted
