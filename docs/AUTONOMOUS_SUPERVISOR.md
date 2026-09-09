# Autonomous Supervisor Pipeline (Task No.1)

## Core Security Invariant

> **RAW WORKER CLAIM ≠ VERIFIED RESULT**

A `WorkerResultBundle` is raw, untrusted input from an autonomous worker agent.
A `VerifiedResult` is the **supervisor's deterministic adjudication** — produced
only after all validation, path-checking, digest verification, and gate evaluation
has passed.  No raw LLM text, no log string, and no GENERIC-category artifact can
bypass this pipeline to become a `PASS`.

---

## Pipeline Architecture

```
Worker (untrusted)
       │
       │  result_bundle.json  (WorkerResultBundle schema v1)
       ▼
┌─────────────────────────────────────────────────┐
│  ResultCollector  (ai_engineering/supervisor/   │
│                    collector.py)                │
│                                                 │
│  1. Fail-closed JSON parse (dup-key rejection)  │
│  2. Schema version check                        │
│  3. task_id + intent_digest binding             │
│  4. attempt_id presence check                   │
│  5. Resolve all artifact paths via _safe_resolve│
│  6. SHA-256 verify each artifact on disk        │
│  7. Resource bounds (artifacts, size, manifest) │
│  8. shunt_route() – type -> EvidenceCategory   │
│  9. normalize_worker_result()                   │
└─────────────────┬───────────────────────────────┘
                  │  NormalizedEvidence  (schema v1)
                  ▼
┌─────────────────────────────────────────────────┐
│  validate_normalized_evidence()                 │
│  (ai_engineering/supervisor/validator.py)       │
│                                                 │
│  Deterministic checks:                          │
│  1. intent_digest match                         │
│  2. task_id match                               │
│  3. attempt_id non-empty                        │
│  4. base_sha match  (stale evidence guard)      │
│  5. repository match                            │
│  6. canonical_remote match                      │
│  7. per required gate:                          │
│     a. GateClaim present                        │
│     b. claimed_status                           │
│     c. GENERIC-only artifacts cannot satisfy    │
│                                                 │
│  Status outcomes:                               │
│    PASS    – all required gates pass, no        │
│              blockers                           │
│    FAIL    – required evidence present but      │
│              one+ required gate FAIL            │
│    BLOCKED – any structural blocker             │
└─────────────────┬───────────────────────────────┘
                  │  VerifiedResult  (schema v1)
                  ▼
              Output / Audit Log
```

---

## Modules

| Module | Purpose |
|--------|---------|
| `ai_engineering/supervisor/__init__.py` | Package marker |
| `ai_engineering/supervisor/worker_result.py` | `WorkerResultBundle` + fail-closed deserialization |
| `ai_engineering/supervisor/shunt_router.py` | `EvidenceCategory` routing for artifact types |
| `ai_engineering/supervisor/normalized_evidence.py` | `NormalizedEvidence` – attempt-bound, content-bound |
| `ai_engineering/supervisor/validator.py` | `VerifiedResult` – deterministic adjudication |
| `ai_engineering/supervisor/collector.py` | `ResultCollector` – trusted security boundary |
| `scripts/run_supervisor_pipeline.py` | CLI entry point (exit 0=PASS, 1=FAIL, 2=BLOCKED) |

---

## Schema Versions

| Schema | Version String |
|--------|---------------|
| Worker Result | `hermes.worker-result.v1` |
| Normalized Evidence | `hermes.normalized-evidence.v1` |
| Verified Result | `hermes.verified-result.v1` |

---

## Key Invariants

### Provenance Guard
An `evidence_id` is a SHA256 hash bound to `(task_id, attempt_id, intent_digest, base_sha, head_sha, ...)`.
Evidence from attempt A **MUST NOT** satisfy attempt B because the `evidence_id` will differ.

### Stale Head Guard
Evidence bound to a `base_sha` that doesn't match the `TaskIntent.source_base_sha` is
rejected with `STALE_EVIDENCE`.

### GENERIC Evidence Cannot Satisfy Required Gates
If all artifacts supporting a gate claim have `EvidenceCategory.GENERIC`, the gate
is blocked with `GENERIC_EVIDENCE_CANNOT_SATISFY_REQUIRED_GATE`. This prevents
arbitrary files from bypassing deterministic validation gates.

### Determinism / Idempotency
Same `NormalizedEvidence` + same `TaskIntent` → same `VerifiedResult` (content-bound
`result_id` ensures this).

### Path Safety
All artifact `relative_path` values are checked before any filesystem access:
- No absolute paths (`/`, `C:\`, etc.)
- No UNC paths (`\\server\share`, `//server/share`)
- No traversal components (`..`)
- Symlinks rejected
- Resolved path must remain inside `evidence_root`

---

## Resource Bounds

| Limit | Value |
|-------|-------|
| Max artifacts per result | 50 |
| Max artifact size | 100 MB |
| Max total artifact size | 500 MB |
| Max manifest JSON size | 512 KB |

---

## CLI Usage

```bash
python scripts/run_supervisor_pipeline.py \
    --result path/to/worker_result.json \
    --evidence-root path/to/evidence/ \
    --intent path/to/intent.json \
    [--output path/to/verified_result.json]
```

Exit codes: `0` = PASS · `1` = FAIL · `2` = BLOCKED or error

---

## What Task No.2 Will Build

Task No.2 will implement the **Supervisor State Machine**:
- `SupervisorRun` lifecycle (PENDING → RUNNING → COLLECTING → VERIFYING → DONE)
- Attempt tracking and retry logic
- Multi-worker evidence aggregation
- Supervisor authority receipts
- Integration with the existing `ControlPlane` and `RunRegistry`
- Durable state persistence and replay

The current pipeline (Task No.1) is a pure, stateless function — it operates on
a single worker result and produces a single `VerifiedResult`.  The state machine
will coordinate multiple results across time.
