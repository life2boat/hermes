#!/usr/bin/env python3
"""Generate synthetic fixtures for production_readiness_evidence tests.

Run from the repository root:
  wsl python tests/fixtures/production_readiness_evidence/generate.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from ai_engineering.production_runtime_attestation import (
    compare_production_runtime,
    create_attestation,
    create_collector_result,
    create_intended_state,
    deserialize_attestation,
    deserialize_comparison,
    normalize_comparison,
    serialize_comparison,
)

OUT = Path(__file__).parent

TARGET = "synthetic-prod"
CANDIDATE_SHA = "a" * 40
OBSERVED_SHA = "a" * 40
RUNTIME_SHA = "b" * 40

# Base attestation with MATCH conditions
att_match = create_attestation(
    target=TARGET,
    collected_at_utc="2026-01-02T03:04:05Z",
    collectors=[
        create_collector_result("runtime", "AVAILABLE", {"image_revision": "abc123", "running": True}),
        create_collector_result("storage", "AVAILABLE", {"integrity": "ok"}),
    ],
)

# Intended state for MATCH
intended = create_intended_state(
    target=TARGET,
    expected_observations={
        "runtime": {"image_revision": "abc123", "running": True},
        "storage": {"integrity": "ok"},
    },
)

# MATCH comparison
cmp_match = compare_production_runtime(intended, att_match)
(OUT / "comparison_match.json").write_bytes(serialize_comparison(cmp_match))
print(f"comparison_match.json -> {cmp_match.status}")

# Attestation for DRIFT - different image_revision
att_drift = create_attestation(
    target=TARGET,
    collected_at_utc="2026-01-02T03:04:05Z",
    collectors=[
        create_collector_result("runtime", "AVAILABLE", {"image_revision": "drifted", "running": True}),
        create_collector_result("storage", "AVAILABLE", {"integrity": "ok"}),
    ],
)
cmp_drift = compare_production_runtime(intended, att_drift)
(OUT / "attestation_drift.json").write_bytes(
    json.dumps({"attestation_id": att_drift.attestation_id,
                "collected_at_utc": att_drift.collected_at_utc,
                "collectors": [{"collector_id": c.collector_id, "observations": dict(c.observations), "status": c.status.value} for c in att_drift.collectors],
                "schema_version": att_drift.schema_version,
                "target": att_drift.target}).encode()
)
(OUT / "comparison_drift.json").write_bytes(serialize_comparison(cmp_drift))
print(f"comparison_drift.json -> {cmp_drift.status}")

# Attestation for INSUFFICIENT - missing observations
att_insuf = create_attestation(
    target=TARGET,
    collected_at_utc="2026-01-02T03:04:05Z",
    collectors=[
        create_collector_result("runtime", "UNAVAILABLE", {}),
        create_collector_result("storage", "UNAVAILABLE", {}),
    ],
)
cmp_insuf = compare_production_runtime(intended, att_insuf)
(OUT / "attestation_insufficient.json").write_bytes(
    json.dumps({"attestation_id": att_insuf.attestation_id,
                "collected_at_utc": att_insuf.collected_at_utc,
                "collectors": [{"collector_id": c.collector_id, "observations": dict(c.observations), "status": c.status.value} for c in att_insuf.collectors],
                "schema_version": att_insuf.schema_version,
                "target": att_insuf.target}).encode()
)
(OUT / "comparison_insufficient.json").write_bytes(serialize_comparison(cmp_insuf))
print(f"comparison_insufficient.json -> {cmp_insuf.status}")

# Main MATCH attestation
(OUT / "attestation_match.json").write_bytes(
    json.dumps({"attestation_id": att_match.attestation_id,
                "collected_at_utc": att_match.collected_at_utc,
                "collectors": [{"collector_id": c.collector_id, "observations": dict(c.observations), "status": c.status.value} for c in att_match.collectors],
                "schema_version": att_match.schema_version,
                "target": att_match.target}).encode()
)

print("Done.")
print(f"\natt_match.attestation_id = {att_match.attestation_id!r}")
print(f"att_match.collected_at_utc = {att_match.collected_at_utc!r}")
print(f"cmp_match.comparison_id = {cmp_match.comparison_id!r}")
print(f"cmp_match.intended_digest = {cmp_match.intended_digest!r}")
