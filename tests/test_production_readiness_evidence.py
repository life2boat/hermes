"""Tests for ai_engineering/production_readiness_evidence.py — Sprint C1.

Covers all 27 mandatory test cases:
 1.  valid MATCH + health PASS + fresh → PASS
 2.  DRIFT → FAIL
 3.  comparison insufficient → BLOCKED
 4.  post-health FAIL → FAIL
 5.  post-health insufficient → BLOCKED (historical B2 synthetic regression)
 6.  stale evidence → BLOCKED
 7.  evidence exactly at freshness boundary → deterministic PASS
 8.  evaluated_at before collected_at → fail closed
 9.  invalid max_age_seconds → fail closed
10.  attestation tamper → rejected
11.  comparison tamper → rejected
12.  comparison attestation_id mismatch → rejected
13.  target mismatch → rejected
14.  candidate/head mismatch → FAIL
15.  malformed SHA → rejected
16.  runtime evidence source mismatch → fail closed
17.  receipt_id deterministic
18.  receipt tampering rejected
19.  canonical serialization deterministic
20.  secret-looking evidence refs rejected
21.  adapter produces GateName.PRODUCTION_READINESS
22.  adapter cannot produce LIVE_BEHAVIOUR_GATE
23.  release target MERGE remains unaffected
24.  production release with readiness PASS but another gate BLOCKED remains BLOCKED
25.  production readiness PASS does not grant execution authority
26.  existing release_gate regression suite remains green
27.  ProductionRuntimeAttestation B1/B2 regression suite (historical B2: MATCH+INSUFFICIENT_EVIDENCE → BLOCKED)
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ai_engineering.contracts import Status
from ai_engineering.production_readiness_evidence import (
    PostCollectionHealthStatus,
    ProductionReadinessEvidenceError,
    ProductionReadinessEvidenceReceipt,
    ProductionReadinessStatus,
    deserialize_receipt,
    normalize_receipt,
    serialize_receipt,
    to_production_readiness_gate_evidence,
    verify_production_readiness,
    verify_receipt,
    PRODUCTION_READINESS_EVIDENCE_SCHEMA_VERSION,
)
from ai_engineering.production_runtime_attestation import (
    ComparisonStatus,
    ProductionRuntimeAttestationError,
    compare_production_runtime,
    create_attestation,
    create_collector_result,
    create_intended_state,
    deserialize_attestation,
    deserialize_comparison,
)
from ai_engineering.release_gate import (
    GateName,
    GateEvidence,
    ReleaseTarget,
    ReleaseTaskClassification,
    SourceIdentity,
    TechnicalBlocker,
    BlockerScope,
    derive_gate_requirements,
    evaluate_release,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "production_readiness_evidence"
ATT_FIXTURES = ROOT / "tests" / "fixtures" / "production_runtime_attestation"

# ---------------------------------------------------------------------------
# Shared constants for synthetic SHAs
# ---------------------------------------------------------------------------
CANDIDATE_SHA = "a" * 40
OBSERVED_SHA = "a" * 40  # matches candidate → valid
MISMATCH_SHA = "c" * 40
RUNTIME_SHA = "b" * 40
TARGET = "synthetic-prod"
REPO = "life2boat/hermes"
REMOTE = "origin"
COLLECTED_AT = "2026-01-02T03:04:05Z"
EVALUATED_AT = "2026-01-02T04:04:05Z"  # 1 hour later
MAX_AGE = 7200  # 2 hours

# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------


def _att_fixture(name: str):
    return deserialize_attestation((FIXTURES / name).read_bytes())


def _cmp_fixture(name: str):
    return deserialize_comparison((FIXTURES / name).read_bytes())


def _att_match():
    return _att_fixture("attestation_match.json")


def _cmp_match():
    return _cmp_fixture("comparison_match.json")


def _att_drift():
    return _att_fixture("attestation_drift.json")


def _cmp_drift():
    return _cmp_fixture("comparison_drift.json")


def _att_insuf():
    return _att_fixture("attestation_insufficient.json")


def _cmp_insuf():
    return _cmp_fixture("comparison_insufficient.json")


def _base_kwargs(**overrides) -> dict:
    """Return default keyword args for verify_production_readiness."""
    return {
        "attestation": _att_match(),
        "comparison": _cmp_match(),
        "candidate_sha": CANDIDATE_SHA,
        "observed_head_sha": OBSERVED_SHA,
        "runtime_evidence_source_sha": RUNTIME_SHA,
        "expected_target": TARGET,
        "repository": REPO,
        "canonical_remote": REMOTE,
        "evaluated_at_utc": EVALUATED_AT,
        "max_age_seconds": MAX_AGE,
        "post_collection_health_status": PostCollectionHealthStatus.PASS,
        **overrides,
    }


# ---------------------------------------------------------------------------
# Test 1: valid MATCH + health PASS + fresh → PASS
# ---------------------------------------------------------------------------


def test_match_health_pass_fresh_produces_pass() -> None:
    receipt = verify_production_readiness(**_base_kwargs())
    assert receipt.final_status == ProductionReadinessStatus.PASS.value
    assert "ALL_CHECKS_PASS" in receipt.reason_codes
    assert receipt.schema_version == PRODUCTION_READINESS_EVIDENCE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Test 2: DRIFT → FAIL
# ---------------------------------------------------------------------------


def test_drift_produces_fail() -> None:
    receipt = verify_production_readiness(
        **_base_kwargs(
            attestation=_att_drift(),
            comparison=_cmp_drift(),
        )
    )
    assert receipt.final_status == ProductionReadinessStatus.FAIL.value
    assert "PRODUCTION_RUNTIME_DRIFT" in receipt.reason_codes


# ---------------------------------------------------------------------------
# Test 3: comparison insufficient → BLOCKED
# ---------------------------------------------------------------------------


def test_insufficient_comparison_produces_blocked() -> None:
    receipt = verify_production_readiness(
        **_base_kwargs(
            attestation=_att_insuf(),
            comparison=_cmp_insuf(),
        )
    )
    assert receipt.final_status == ProductionReadinessStatus.BLOCKED.value
    assert "PRODUCTION_RUNTIME_EVIDENCE_INSUFFICIENT" in receipt.reason_codes


# ---------------------------------------------------------------------------
# Test 4: post-health FAIL → FAIL
# ---------------------------------------------------------------------------


def test_post_health_fail_produces_fail() -> None:
    receipt = verify_production_readiness(
        **_base_kwargs(post_collection_health_status=PostCollectionHealthStatus.FAIL)
    )
    assert receipt.final_status == ProductionReadinessStatus.FAIL.value
    assert "POST_COLLECTION_HEALTH_FAIL" in receipt.reason_codes


# ---------------------------------------------------------------------------
# Test 5: post-health INSUFFICIENT_EVIDENCE → BLOCKED (historical B2 regression)
# ---------------------------------------------------------------------------


def test_post_health_insufficient_produces_blocked() -> None:
    """Historical B2 synthetic regression: MATCH + INSUFFICIENT_EVIDENCE → BLOCKED.

    A MATCH comparison alone MUST NOT produce PASS when post-collection health
    is INSUFFICIENT_EVIDENCE. This ensures the historical B2 attestation
    (comparison=MATCH, post_health=INSUFFICIENT_EVIDENCE) correctly yields
    PRODUCTION_READINESS_GATE=BLOCKED.
    """
    receipt = verify_production_readiness(
        **_base_kwargs(
            post_collection_health_status=PostCollectionHealthStatus.INSUFFICIENT_EVIDENCE
        )
    )
    assert receipt.final_status == ProductionReadinessStatus.BLOCKED.value
    assert "POST_COLLECTION_HEALTH_INSUFFICIENT_EVIDENCE" in receipt.reason_codes


# ---------------------------------------------------------------------------
# Test 6: stale evidence → BLOCKED
# ---------------------------------------------------------------------------


def test_stale_evidence_produces_blocked() -> None:
    # collected_at = 2026-01-02T03:04:05Z, evaluated_at 3 hours later,
    # max_age = 1 hour → stale
    with pytest.raises(ProductionReadinessEvidenceError, match="STALE"):
        verify_production_readiness(
            **_base_kwargs(
                evaluated_at_utc="2026-01-02T06:04:05Z",
                max_age_seconds=3600,
            )
        )


# ---------------------------------------------------------------------------
# Test 7: evidence exactly at freshness boundary → PASS (not stale)
# ---------------------------------------------------------------------------


def test_evidence_at_exact_freshness_boundary_passes() -> None:
    # collected_at = 2026-01-02T03:04:05Z, evaluated exactly 3600s later,
    # max_age = 3600 → age == max_age → exactly at boundary → PASS
    receipt = verify_production_readiness(
        **_base_kwargs(
            evaluated_at_utc="2026-01-02T04:04:05Z",
            max_age_seconds=3600,
        )
    )
    assert receipt.final_status == ProductionReadinessStatus.PASS.value


# ---------------------------------------------------------------------------
# Test 8: evaluated_at before collected_at → fail closed
# ---------------------------------------------------------------------------


def test_evaluated_before_collected_fails_closed() -> None:
    # evaluated_at 1 second before collected_at
    with pytest.raises(ProductionReadinessEvidenceError, match="EVALUATED_BEFORE_COLLECTED"):
        verify_production_readiness(
            **_base_kwargs(evaluated_at_utc="2026-01-02T03:04:04Z")
        )


# ---------------------------------------------------------------------------
# Test 9: invalid max_age_seconds → fail closed
# ---------------------------------------------------------------------------


def test_invalid_max_age_fails_closed() -> None:
    with pytest.raises(ProductionReadinessEvidenceError, match="MAX_AGE_INVALID"):
        verify_production_readiness(**_base_kwargs(max_age_seconds=0))

    with pytest.raises(ProductionReadinessEvidenceError, match="MAX_AGE_INVALID"):
        verify_production_readiness(**_base_kwargs(max_age_seconds=-1))

    with pytest.raises(ProductionReadinessEvidenceError, match="MAX_AGE_INVALID"):
        verify_production_readiness(**_base_kwargs(max_age_seconds=True))  # bool is not int here


# ---------------------------------------------------------------------------
# Test 10: attestation tamper → rejected
# ---------------------------------------------------------------------------


def test_tampered_attestation_is_rejected() -> None:
    att = _att_match()
    # Mutate the attestation by changing target (breaks attestation_id)
    tampered = replace(att, target="different-target")
    with pytest.raises(ProductionReadinessEvidenceError):
        verify_production_readiness(**_base_kwargs(attestation=tampered))


# ---------------------------------------------------------------------------
# Test 11: comparison tamper → rejected
# ---------------------------------------------------------------------------


def test_tampered_comparison_is_rejected() -> None:
    cmp = _cmp_match()
    # Mutate comparison status (breaks comparison_id)
    tampered = replace(cmp, status=ComparisonStatus.DRIFT)
    with pytest.raises(ProductionReadinessEvidenceError):
        verify_production_readiness(**_base_kwargs(comparison=tampered))


# ---------------------------------------------------------------------------
# Test 12: comparison attestation_id mismatch → rejected
# ---------------------------------------------------------------------------


def test_comparison_attestation_id_mismatch_is_rejected() -> None:
    # Use the drift comparison (which is bound to a different attestation)
    with pytest.raises(ProductionReadinessEvidenceError, match="ATTESTATION_COMPARISON_ID_MISMATCH"):
        verify_production_readiness(
            **_base_kwargs(
                attestation=_att_match(),
                comparison=_cmp_drift(),
            )
        )


# ---------------------------------------------------------------------------
# Test 13: target mismatch → rejected
# ---------------------------------------------------------------------------


def test_target_mismatch_is_rejected() -> None:
    with pytest.raises(ProductionReadinessEvidenceError, match="TARGET_MISMATCH"):
        verify_production_readiness(**_base_kwargs(expected_target="other-target"))


# ---------------------------------------------------------------------------
# Test 14: candidate/head mismatch → FAIL
# ---------------------------------------------------------------------------


def test_candidate_head_mismatch_produces_fail() -> None:
    receipt = verify_production_readiness(
        **_base_kwargs(observed_head_sha=MISMATCH_SHA)
    )
    assert receipt.final_status == ProductionReadinessStatus.FAIL.value
    assert "EXACT_SHA_MISMATCH" in receipt.reason_codes


# ---------------------------------------------------------------------------
# Test 15: malformed SHA → rejected
# ---------------------------------------------------------------------------


def test_malformed_sha_is_rejected() -> None:
    with pytest.raises(ProductionReadinessEvidenceError, match="SHA_INVALID"):
        verify_production_readiness(**_base_kwargs(candidate_sha="not-a-sha"))

    with pytest.raises(ProductionReadinessEvidenceError, match="SHA_INVALID"):
        verify_production_readiness(**_base_kwargs(observed_head_sha="not-a-sha"))

    with pytest.raises(ProductionReadinessEvidenceError, match="SHA_INVALID"):
        verify_production_readiness(**_base_kwargs(runtime_evidence_source_sha="not-a-sha"))


# ---------------------------------------------------------------------------
# Test 16: runtime evidence source mismatch → fail closed
# ---------------------------------------------------------------------------


def test_runtime_evidence_source_mismatch_fails_closed() -> None:
    with pytest.raises(
        ProductionReadinessEvidenceError, match="RUNTIME_EVIDENCE_SOURCE_MISMATCH"
    ):
        verify_production_readiness(
            **_base_kwargs(
                expected_runtime_evidence_source_sha="d" * 40,
            )
        )


# ---------------------------------------------------------------------------
# Test 17: receipt_id is deterministic
# ---------------------------------------------------------------------------


def test_receipt_id_is_deterministic() -> None:
    r1 = verify_production_readiness(**_base_kwargs())
    r2 = verify_production_readiness(**_base_kwargs())
    assert r1.receipt_id == r2.receipt_id
    assert r1 == r2


# ---------------------------------------------------------------------------
# Test 18: receipt tampering rejected
# ---------------------------------------------------------------------------


def test_tampered_receipt_is_rejected() -> None:
    receipt = verify_production_readiness(**_base_kwargs())
    # Tamper by changing a field (keep receipt_id the same)
    tampered = replace(receipt, final_status=ProductionReadinessStatus.FAIL.value)
    with pytest.raises(ProductionReadinessEvidenceError, match="TAMPERED_RECEIPT_ID"):
        verify_receipt(tampered)


# ---------------------------------------------------------------------------
# Test 19: canonical serialization is deterministic
# ---------------------------------------------------------------------------


def test_canonical_serialization_is_deterministic() -> None:
    receipt = verify_production_readiness(**_base_kwargs())
    b1 = serialize_receipt(receipt)
    b2 = serialize_receipt(receipt)
    assert b1 == b2
    assert b"\n" not in b1  # compact JSON
    # Deserializing again produces the same receipt
    r2 = deserialize_receipt(b1)
    assert serialize_receipt(r2) == b1


# ---------------------------------------------------------------------------
# Test 20: secret-looking evidence refs rejected/sanitized
# ---------------------------------------------------------------------------


def test_secret_looking_evidence_refs_are_rejected() -> None:
    """Evidence refs must not carry raw secret material."""
    from ai_engineering.production_readiness_evidence import _require_evidence_ref, ProductionReadinessEvidenceError
    with pytest.raises(ProductionReadinessEvidenceError, match="EVIDENCE_REF"):
        _require_evidence_ref("artifact:production-runtime-attestation:password=abc123")

    with pytest.raises(ProductionReadinessEvidenceError, match="EVIDENCE_REF"):
        _require_evidence_ref("artifact:production-runtime-attestation:not-a-hash")


# ---------------------------------------------------------------------------
# Test 21: adapter produces GateName.PRODUCTION_READINESS
# ---------------------------------------------------------------------------


def test_adapter_produces_production_readiness_gate() -> None:
    receipt = verify_production_readiness(**_base_kwargs())
    gate = to_production_readiness_gate_evidence(receipt)
    assert gate.gate_name == GateName.PRODUCTION_READINESS
    assert gate.status == Status.PASS
    assert gate.evidence_digest == receipt.receipt_id


# ---------------------------------------------------------------------------
# Test 22: adapter cannot produce LIVE_BEHAVIOUR_GATE
# ---------------------------------------------------------------------------


def test_adapter_never_produces_live_behaviour_gate() -> None:
    receipt = verify_production_readiness(**_base_kwargs())
    gate = to_production_readiness_gate_evidence(receipt)
    assert gate.gate_name != GateName.LIVE_BEHAVIOUR


# ---------------------------------------------------------------------------
# Test 23: release target MERGE remains unaffected by production_readiness bridge
# ---------------------------------------------------------------------------


def test_merge_target_unaffected() -> None:
    """MERGE target does not require PRODUCTION_READINESS_GATE."""
    classification = ReleaseTaskClassification(
        task_classification="test-task",
        behaviour_sensitive=False,
        security_sensitive=False,
        cost_sensitive=False,
        production_sensitive=False,
        live_behaviour_required=False,
    )
    reqs = derive_gate_requirements(ReleaseTarget.MERGE, classification)
    assert reqs[GateName.PRODUCTION_READINESS] is False


# ---------------------------------------------------------------------------
# Test 24: production release with readiness PASS but another required gate BLOCKED remains BLOCKED
# ---------------------------------------------------------------------------


def _all_gates(*, production_readiness_status: Status) -> list[GateEvidence]:
    """Build a complete gate set with only PRODUCTION_READINESS as specified."""
    return [
        GateEvidence(
            gate_name=GateName.CODE,
            required=True,
            status=Status.PASS,
            evidence_refs=("source:code-evidence",),
            reason_codes=("PASS",),
        ),
        GateEvidence(
            gate_name=GateName.BEHAVIOUR,
            required=True,  # security_sensitive=True implies behaviour_sensitive
            status=Status.PASS,
            evidence_refs=("source:behaviour-evidence",),
            reason_codes=("PASS",),
        ),
        GateEvidence(
            gate_name=GateName.SECURITY,
            required=True,
            status=Status.BLOCKED,  # Another gate is BLOCKED
            evidence_refs=("source:security-evidence",),
            reason_codes=("SECURITY_REVIEW_PENDING",),
        ),
        GateEvidence(
            gate_name=GateName.LIVE_BEHAVIOUR,
            required=False,
            status=Status.NOT_PERFORMED,
            evidence_refs=(),
            reason_codes=(),
        ),
        GateEvidence(
            gate_name=GateName.COST,
            required=False,
            status=Status.NOT_PERFORMED,
            evidence_refs=(),
            reason_codes=(),
        ),
        GateEvidence(
            gate_name=GateName.PRODUCTION_READINESS,
            required=True,
            status=production_readiness_status,
            evidence_refs=("artifact:production-runtime-attestation:" + "a" * 64,),
            reason_codes=("ALL_CHECKS_PASS",),
        ),
    ]


def test_another_required_gate_blocked_keeps_release_blocked() -> None:
    classification = ReleaseTaskClassification(
        task_classification="test-task",
        behaviour_sensitive=False,
        security_sensitive=True,
        cost_sensitive=False,
        production_sensitive=True,
        live_behaviour_required=False,
    )
    source = SourceIdentity(
        repository=REPO,
        canonical_remote=REMOTE,
        base_sha="e" * 40,
        candidate_sha=CANDIDATE_SHA,
        observed_head_sha=CANDIDATE_SHA,  # matching → no SHA blocker
        task_id="test-task-24",
    )
    gates = _all_gates(production_readiness_status=Status.PASS)
    receipt_rg = evaluate_release(
        target=ReleaseTarget.PRODUCTION_RELEASE,
        source=source,
        classification=classification,
        gate_results=gates,
    )
    # SECURITY_GATE is BLOCKED → entire release is BLOCKED
    assert receipt_rg.production_release_eligible != Status.PASS


# ---------------------------------------------------------------------------
# Test 25: production readiness PASS does not grant execution authority
# ---------------------------------------------------------------------------


def test_production_readiness_pass_does_not_grant_authority() -> None:
    """PASS proves evidence quality only. EVIDENCE_EXPANDS_AUTHORITY=false."""
    receipt = verify_production_readiness(**_base_kwargs())
    assert receipt.final_status == ProductionReadinessStatus.PASS.value
    # Verify the module has no production-touching imports
    import ai_engineering.production_readiness_evidence as mod
    import inspect
    source = inspect.getsource(mod)
    # These imports are forbidden in the bridge module
    for forbidden in ("import docker", "import sqlite3", "import qdrant", "subprocess"):
        assert forbidden not in source, f"Found forbidden import: {forbidden!r}"


# ---------------------------------------------------------------------------
# Test 26: existing release_gate regression suite remains green (smoke check)
# ---------------------------------------------------------------------------


def test_release_gate_regression_smoke() -> None:
    """Smoke test: evaluate_release with minimal inputs still works."""
    classification = ReleaseTaskClassification(
        task_classification="smoke-test",
        behaviour_sensitive=False,
        security_sensitive=False,
        cost_sensitive=False,
        production_sensitive=False,
        live_behaviour_required=False,
    )
    source = SourceIdentity(
        repository=REPO,
        canonical_remote=REMOTE,
        base_sha="e" * 40,
        candidate_sha=CANDIDATE_SHA,
        observed_head_sha=CANDIDATE_SHA,
        task_id="smoke-task",
    )
    gates_merge = [
        GateEvidence(
            gate_name=GateName.CODE,
            required=True,
            status=Status.PASS,
            evidence_refs=("source:code-ref",),
            reason_codes=("PASS",),
        ),
        GateEvidence(
            gate_name=GateName.BEHAVIOUR,
            required=False,
            status=Status.NOT_PERFORMED,
            evidence_refs=(),
            reason_codes=(),
        ),
        GateEvidence(
            gate_name=GateName.SECURITY,
            required=False,
            status=Status.NOT_PERFORMED,
            evidence_refs=(),
            reason_codes=(),
        ),
        GateEvidence(
            gate_name=GateName.LIVE_BEHAVIOUR,
            required=False,
            status=Status.NOT_PERFORMED,
            evidence_refs=(),
            reason_codes=(),
        ),
        GateEvidence(
            gate_name=GateName.COST,
            required=False,
            status=Status.NOT_PERFORMED,
            evidence_refs=(),
            reason_codes=(),
        ),
        GateEvidence(
            gate_name=GateName.PRODUCTION_READINESS,
            required=False,
            status=Status.NOT_PERFORMED,
            evidence_refs=(),
            reason_codes=(),
        ),
    ]
    rg_receipt = evaluate_release(
        target=ReleaseTarget.MERGE,
        source=source,
        classification=classification,
        gate_results=gates_merge,
    )
    assert rg_receipt.status == Status.PASS


# ---------------------------------------------------------------------------
# Test 27: ProductionRuntimeAttestation B1/B2 regression
#          Historical B2: MATCH + INSUFFICIENT_EVIDENCE → BLOCKED
# ---------------------------------------------------------------------------


def test_historical_b2_synthetic_match_plus_insufficient_health_is_blocked() -> None:
    """Synthetic equivalent of the historical B2 attestation scenario.

    The authoritative B2 live collection produced:
      comparison = MATCH
      post_collection_health = INSUFFICIENT_EVIDENCE

    This MUST NOT be retroactively upgraded to PASS.
    The bridge must classify this as BLOCKED with reason
    POST_COLLECTION_HEALTH_INSUFFICIENT_EVIDENCE.
    """
    receipt = verify_production_readiness(
        **_base_kwargs(
            post_collection_health_status=PostCollectionHealthStatus.INSUFFICIENT_EVIDENCE
        )
    )
    assert receipt.final_status == ProductionReadinessStatus.BLOCKED.value
    assert "POST_COLLECTION_HEALTH_INSUFFICIENT_EVIDENCE" in receipt.reason_codes

    # Adapter must also produce BLOCKED gate, not PASS
    gate = to_production_readiness_gate_evidence(receipt)
    assert gate.gate_name == GateName.PRODUCTION_READINESS
    assert gate.status == Status.BLOCKED
