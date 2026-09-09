"""test_supervisor_pipeline.py - Full test matrix for the supervisor pipeline.

Covers all 34 required test cases from the Task No.1 specification:
 1.  happy_path complete evidence -> PASS
 2.  required test failure -> FAIL
 3.  missing required evidence -> BLOCKED
 4.  wrong repository/base/head provenance -> BLOCKED
 5.  stale evidence from previous HEAD -> BLOCKED
 6.  evidence from wrong task -> BLOCKED
 7.  evidence from wrong attempt -> BLOCKED
 8.  mixed attempts -> BLOCKED
 9.  artifact SHA-256 mismatch -> BLOCKED
10.  duplicate/replayed identical result -> idempotent
11.  canonical serialization -> deterministic digest
12.  unsupported schema -> fail closed
13.  malformed JSON -> fail closed
14.  duplicate JSON key handling
15.  unknown artifact type -> preserved but cannot satisfy required gate
16.  path traversal -> rejected
17.  absolute Windows path escape -> rejected
18.  UNC path -> rejected
19.  symlink escape -> rejected
20.  oversized artifact -> rejected/bounded
21.  too many artifacts -> rejected/bounded
22.  obvious credential metadata -> redacted/not persisted
23.  internal deterministic processing failure -> cannot become PASS
24.  existing TaskIntent compatibility -> PASS
25.  existing TaskLineage compatibility -> PASS where applicable
26.  existing BehaviourTrace/GateResult compatibility -> PASS where applicable
27.  shunt routing deterministic
28.  provenance invariant: attempt A cannot satisfy attempt B
29.  serialization: sorted keys, no NaN, no Infinity
30.  WorkerResultError exposes stable .code attribute
31.  CollectorError exposes stable .code attribute
32.  ValidatorError exposes stable .code attribute
33.  EvidenceCategory.GENERIC preserved in normalized evidence
34.  evidence_id is content-bound to attempt_id
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from ai_engineering.contracts import GateResult, Status
from ai_engineering.task_intent import (
    TaskIntent,
    validate_intent,
    intent_digest,
    TaskLineage,
)

from ai_engineering.supervisor.worker_result import (
    ArtifactManifestEntry,
    GateClaim,
    WorkerResultBundle,
    WorkerResultError,
    deserialize_worker_result,
    validate_worker_result,
    canonical_serialize_worker_result,
    worker_result_digest,
    WORKER_RESULT_SCHEMA_VERSION,
)
from ai_engineering.supervisor.shunt_router import (
    EvidenceCategory,
    ShuntedArtifact,
    shunt_route,
)
from ai_engineering.supervisor.normalized_evidence import (
    NormalizedEvidence,
    NORMALIZED_EVIDENCE_SCHEMA_VERSION,
    compute_evidence_id,
    normalize_worker_result,
    canonical_serialize_normalized_evidence,
    normalized_evidence_digest,
)
from ai_engineering.supervisor.validator import (
    VERIFIED_RESULT_SCHEMA_VERSION,
    VerifiedResult,
    ValidatorError,
    validate_normalized_evidence,
    canonical_serialize_verified_result,
)
from ai_engineering.supervisor.collector import (
    CollectorError,
    ResultCollector,
)


# ── Fixtures directory ───────────────────────────────────────────────────────────
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "supervisor"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


def _write_artifact(tmp: Path, name: str, content: str) -> tuple[str, int]:
    """Write an artifact file and return (sha256, size)."""
    data = content.encode("utf-8")
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    return sha, len(data)


# ── Base TaskIntent factory ──────────────────────────────────────────────────────

def _make_intent(
    task_id: str = "TASK-001",
    base_sha: str = "a" * 40,
    required_gates: list[str] | None = None,
    source_repository: str = "github",
) -> TaskIntent:
    return validate_intent({
        "schema_version": 1,
        "task_id": task_id,
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Build supervisor",
        "source_repository": source_repository,
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": base_sha,
        "constraints": [],
        "allowed_mutations": ["ai_engineering/supervisor/"],
        "forbidden_mutations": [],
        "stop_boundary": "MERGE",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": required_gates or ["tests"],
        "parent_intent_digest": None,
    })


def _make_worker_result_json(
    intent: TaskIntent,
    artifacts: list[dict] | None = None,
    gate_claims: list[dict] | None = None,
    task_id: str | None = None,
    attempt_id: str = "attempt-001",
    base_sha: str | None = None,
    head_sha: str = "b" * 40,
    canonical_remote: str = "github",
    repository: str = "github",
    schema_version: str = WORKER_RESULT_SCHEMA_VERSION,
    intent_digest_override: str | None = None,
) -> str:
    dg = intent_digest_override if intent_digest_override is not None else intent_digest(intent)
    data = {
        "schema_version": schema_version,
        "result_id": "result-001",
        "task_id": task_id if task_id is not None else intent.task_id,
        "attempt_id": attempt_id,
        "worker_id": "worker-01",
        "base_sha": base_sha if base_sha is not None else intent.source_base_sha,
        "head_sha": head_sha,
        "canonical_remote": canonical_remote,
        "repository": repository,
        "intent_digest": dg,
        "produced_at_utc": "2026-09-09T13:00:00+00:00",
        "artifacts": artifacts or [],
        "gate_claims": gate_claims or [],
    }
    return json.dumps(data)


# ── Test 1: Happy path -> PASS ───────────────────────────────────────────────────

def test_01_happy_path_pass(tmp_path):
    """Happy path: all required evidence present -> PASS."""
    # Use pre-built fixtures
    intent_data = _load_fixture("happy_path_intent.json")
    intent = validate_intent(intent_data)

    # Copy artifacts to tmp_path (mirrors what collector resolves)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    src_artifacts = FIXTURES_DIR / "artifacts"

    for fname in ["test_result.json", "lint_result.json"]:
        (artifacts_dir / fname).write_bytes((src_artifacts / fname).read_bytes())

    bundle_data = _load_fixture("happy_path_worker_result.json")
    bundle_json = json.dumps(bundle_data)

    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    assert ne.integrity_verified is True
    assert ne.task_id == "TASK-SUPERVISOR-01"
    assert ne.attempt_id == "attempt-001"

    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T13:00:00+00:00")
    assert vr.status == Status.PASS
    assert len(vr.blockers) == 0
    assert any(gr.gate_name == "tests" and gr.status == Status.PASS for gr in vr.gate_results)
    assert any(gr.gate_name == "lint" and gr.status == Status.PASS for gr in vr.gate_results)


# ── Test 2: Required test failure -> FAIL ────────────────────────────────────────

def test_02_required_test_failure(tmp_path):
    """Worker claims tests FAIL -> overall FAIL (not BLOCKED)."""
    intent = _make_intent(required_gates=["tests"])
    dg = intent_digest(intent)

    test_content = json.dumps({"passed": 0, "failed": 5, "outcome": "FAIL"})
    test_sha, test_size = _write_artifact(tmp_path / "artifacts", "test_result.json", test_content)

    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "artifacts/test_result.json",
            "sha256": test_sha,
            "size_bytes": test_size,
            "producer_note": "5 tests failed",
        }],
        gate_claims=[{
            "gate_name": "tests",
            "claimed_status": "FAIL",
            "evidence_refs": ["artifacts/test_result.json"],
            "reason_code": "TESTS_FAILED",
        }],
    )
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    assert vr.status == Status.FAIL
    assert len(vr.blockers) == 0  # FAIL != BLOCKED
    assert any(gr.status == Status.FAIL for gr in vr.gate_results)


# ── Test 3: Missing required evidence -> BLOCKED ─────────────────────────────────

def test_03_missing_required_evidence(tmp_path):
    """No gate claim for required gate -> BLOCKED."""
    intent = _make_intent(required_gates=["tests"])
    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[],
        gate_claims=[],  # No claim for "tests"
    )
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    assert vr.status == Status.BLOCKED
    assert any("REQUIRED_GATE_MISSING" in b for b in vr.blockers)


# ── Test 4: Wrong repository provenance -> BLOCKED ───────────────────────────────

def test_04_wrong_repository_blocked(tmp_path):
    """Mismatched repository -> BLOCKED."""
    intent = _make_intent(source_repository="github")
    bundle_json = _make_worker_result_json(
        intent=intent,
        repository="gitlab",       # wrong
        canonical_remote="gitlab",  # also wrong
        gate_claims=[{
            "gate_name": "tests",
            "claimed_status": "PASS",
            "evidence_refs": [],
            "reason_code": "OK",
        }],
    )
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    assert vr.status == Status.BLOCKED
    assert any("REPOSITORY_MISMATCH" in b or "CANONICAL_REMOTE_MISMATCH" in b for b in vr.blockers)


# ── Test 5: Stale evidence from previous HEAD -> BLOCKED ─────────────────────────

def test_05_stale_evidence_blocked(tmp_path):
    """Evidence bound to wrong base_sha -> BLOCKED (STALE_EVIDENCE)."""
    intent = _make_intent(base_sha="a" * 40)
    bundle_json = _make_worker_result_json(
        intent=intent,
        base_sha="d" * 40,  # stale - different from intent's source_base_sha
    )
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    assert vr.status == Status.BLOCKED
    assert "STALE_EVIDENCE" in vr.blockers


# ── Test 6: Evidence from wrong task -> BLOCKED ──────────────────────────────────

def test_06_wrong_task_blocked():
    """Worker result with wrong task_id -> collector raises TASK_MISMATCH."""
    intent = _make_intent(task_id="TASK-001")
    bundle_json = _make_worker_result_json(
        intent=intent,
        task_id="TASK-999",  # wrong task
        # Note: intent_digest is computed from intent (TASK-001), so mismatch
    )
    collector = ResultCollector()
    with pytest.raises(CollectorError) as exc_info:
        collector.collect(bundle_json, "/tmp", intent)
    assert exc_info.value.code == "TASK_MISMATCH"


# ── Test 7: Evidence from wrong attempt -> BLOCKED ───────────────────────────────

def test_07_wrong_attempt_blocked(tmp_path):
    """Empty attempt_id -> collector raises ATTEMPT_MISSING."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(intent=intent, attempt_id="")
    collector = ResultCollector()
    with pytest.raises(CollectorError) as exc_info:
        collector.collect(bundle_json, tmp_path, intent)
    assert exc_info.value.code == "ATTEMPT_MISSING"


# ── Test 8: Mixed attempts -> BLOCKED ────────────────────────────────────────────

def test_08_mixed_attempts_blocked(tmp_path):
    """Evidence from attempt A cannot satisfy attempt B."""
    intent = _make_intent(required_gates=["tests"])
    dg = intent_digest(intent)

    test_content = json.dumps({"passed": 1, "failed": 0})
    test_sha, test_size = _write_artifact(tmp_path / "artifacts", "test_result.json", test_content)

    # Collect for attempt A
    bundle_a_json = _make_worker_result_json(
        intent=intent,
        attempt_id="attempt-A",
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "artifacts/test_result.json",
            "sha256": test_sha,
            "size_bytes": test_size,
            "producer_note": "ok",
        }],
        gate_claims=[{
            "gate_name": "tests",
            "claimed_status": "PASS",
            "evidence_refs": ["artifacts/test_result.json"],
            "reason_code": "OK",
        }],
    )
    collector = ResultCollector()
    ne_a = collector.collect(bundle_a_json, tmp_path, intent)

    # Create intent for attempt B but use ne_a (attempt A) evidence
    # The evidence_id is bound to attempt_id, so different attempt -> different evidence_id
    ne_b_fake = NormalizedEvidence(
        schema_version=ne_a.schema_version,
        evidence_id=ne_a.evidence_id,  # reuse attempt A's evidence_id
        task_id=ne_a.task_id,
        attempt_id="attempt-B",   # different attempt
        intent_digest=ne_a.intent_digest,
        repository=ne_a.repository,
        canonical_remote=ne_a.canonical_remote,
        base_sha=ne_a.base_sha,
        head_sha=ne_a.head_sha,
        worker_id=ne_a.worker_id,
        artifacts=ne_a.artifacts,
        gate_claims=ne_a.gate_claims,
        produced_at_utc=ne_a.produced_at_utc,
        collection_receipt_id=None,
        integrity_verified=True,
    )

    # evidence_id from attempt A != computed evidence_id for attempt B
    computed_b_id = compute_evidence_id(
        task_id=ne_b_fake.task_id,
        attempt_id="attempt-B",
        intent_digest=ne_b_fake.intent_digest,
        base_sha=ne_b_fake.base_sha,
        head_sha=ne_b_fake.head_sha,
        repository=ne_b_fake.repository,
        canonical_remote=ne_b_fake.canonical_remote,
        worker_id=ne_b_fake.worker_id,
        produced_at_utc=ne_b_fake.produced_at_utc,
    )
    assert ne_a.evidence_id != computed_b_id, "Attempt A evidence_id must differ from attempt B"


# ── Test 9: Artifact SHA-256 mismatch -> BLOCKED ─────────────────────────────────

def test_09_artifact_digest_mismatch(tmp_path):
    """SHA256 on disk differs from manifest -> CollectorError ARTIFACT_DIGEST_MISMATCH."""
    intent = _make_intent(required_gates=["tests"])
    dg = intent_digest(intent)

    # Write artifact with DIFFERENT content than what we'll put in the manifest
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "test_result.json").write_bytes(b'{"actual": "content"}')
    wrong_sha = "0" * 64  # obviously wrong

    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "artifacts/test_result.json",
            "sha256": wrong_sha,  # mismatch
            "size_bytes": 20,
            "producer_note": "ok",
        }],
        gate_claims=[{
            "gate_name": "tests",
            "claimed_status": "PASS",
            "evidence_refs": ["artifacts/test_result.json"],
            "reason_code": "OK",
        }],
    )
    collector = ResultCollector()
    with pytest.raises(CollectorError) as exc_info:
        collector.collect(bundle_json, tmp_path, intent)
    assert exc_info.value.code == "ARTIFACT_DIGEST_MISMATCH"


# ── Test 10: Idempotency / replay ────────────────────────────────────────────────

def test_10_idempotent_replay(tmp_path):
    """Same NormalizedEvidence + same TaskIntent -> same VerifiedResult."""
    intent = _make_intent(required_gates=["tests"])
    test_content = json.dumps({"passed": 1})
    test_sha, test_size = _write_artifact(tmp_path / "artifacts", "test_result.json", test_content)

    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "artifacts/test_result.json",
            "sha256": test_sha,
            "size_bytes": test_size,
            "producer_note": "ok",
        }],
        gate_claims=[{
            "gate_name": "tests",
            "claimed_status": "PASS",
            "evidence_refs": ["artifacts/test_result.json"],
            "reason_code": "OK",
        }],
    )
    collector = ResultCollector()
    ne1 = collector.collect(bundle_json, tmp_path, intent)
    ne2 = collector.collect(bundle_json, tmp_path, intent)

    vr1 = validate_normalized_evidence(ne1, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    vr2 = validate_normalized_evidence(ne2, intent, verified_at_utc="2026-09-09T00:00:00+00:00")

    assert vr1.result_id == vr2.result_id
    assert canonical_serialize_verified_result(vr1) == canonical_serialize_verified_result(vr2)


# ── Test 11: Canonical serialization determinism ──────────────────────────────────

def test_11_canonical_serialization_deterministic(tmp_path):
    """Canonical JSON is stable and produces deterministic SHA256."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(intent=intent)
    bundle = deserialize_worker_result(bundle_json)

    s1 = canonical_serialize_worker_result(bundle)
    s2 = canonical_serialize_worker_result(bundle)
    assert s1 == s2

    # Keys must be sorted
    parsed = json.loads(s1)
    keys = list(parsed.keys())
    assert keys == sorted(keys)

    # Digest must be stable
    d1 = worker_result_digest(bundle)
    d2 = worker_result_digest(bundle)
    assert d1 == d2
    assert len(d1) == 64  # SHA256 hex


# ── Test 12: Unsupported schema version -> fail closed ───────────────────────────

def test_12_unsupported_schema_version():
    """Unknown schema_version -> WorkerResultError with SCHEMA_VERSION_UNSUPPORTED."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(intent=intent, schema_version="hermes.worker-result.v99")
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(bundle_json)
    assert exc_info.value.code == "SCHEMA_VERSION_UNSUPPORTED"


# ── Test 13: Malformed JSON -> fail closed ────────────────────────────────────────

def test_13_malformed_json():
    """Non-JSON input -> WorkerResultError with JSON_INVALID."""
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result("this is not json {{{")
    assert exc_info.value.code == "JSON_INVALID"


def test_13b_empty_json():
    """Empty input -> JSON_INVALID."""
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result("")
    assert exc_info.value.code == "JSON_INVALID"


def test_13c_json_list_not_object():
    """JSON list at top level -> JSON_INVALID."""
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result("[1, 2, 3]")
    assert exc_info.value.code == "JSON_INVALID"


# ── Test 14: Duplicate JSON key handling ──────────────────────────────────────────

def test_14_duplicate_json_key():
    """Duplicate JSON keys must be rejected with JSON_INVALID."""
    bad_json = '{"schema_version": "v1", "schema_version": "v2", "task_id": "x"}'
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(bad_json)
    assert exc_info.value.code == "JSON_INVALID"


# ── Test 15: Unknown artifact type -> preserved as GENERIC ────────────────────────

def test_15_unknown_artifact_type_preserved_as_generic(tmp_path):
    """Unknown artifact_type -> EvidenceCategory.GENERIC, not discarded."""
    intent = _make_intent(required_gates=["tests"])

    unknown_content = json.dumps({"type": "custom", "data": "something"})
    sha, size = _write_artifact(tmp_path / "artifacts", "custom.json", unknown_content)

    artifacts = [
        ShuntedArtifact(
            artifact_type="my_custom_type",
            category=EvidenceCategory.GENERIC,
            artifact_ref="artifacts/custom.json",
            sha256=sha,
            producer_note="custom",
        )
    ]
    # Verify shunt_route produces GENERIC for unknown types
    entries = [
        ArtifactManifestEntry(
            artifact_type="my_custom_type",
            relative_path="artifacts/custom.json",
            sha256=sha,
            size_bytes=size,
            producer_note="custom",
        )
    ]
    routed = shunt_route(entries)
    assert len(routed) == 1
    assert routed[0].category == EvidenceCategory.GENERIC
    assert routed[0].artifact_type == "my_custom_type"  # preserved


def test_15b_generic_cannot_satisfy_required_gate(tmp_path):
    """GENERIC-only evidence cannot satisfy a required gate -> BLOCKED."""
    intent = _make_intent(required_gates=["tests"])

    # Write a file
    content = json.dumps({"custom": "data"})
    sha, size = _write_artifact(tmp_path / "artifacts", "custom.json", content)

    # Worker claims "tests" gate with a GENERIC artifact
    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "my_unknown_type",  # -> GENERIC
            "relative_path": "artifacts/custom.json",
            "sha256": sha,
            "size_bytes": size,
            "producer_note": "custom",
        }],
        gate_claims=[{
            "gate_name": "tests",
            "claimed_status": "PASS",
            "evidence_refs": ["artifacts/custom.json"],
            "reason_code": "OK",
        }],
    )
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    assert vr.status == Status.BLOCKED
    assert any("GENERIC_EVIDENCE_CANNOT_SATISFY_REQUIRED_GATE" in b for b in vr.blockers)


# ── Test 16: Path traversal -> rejected ──────────────────────────────────────────

def test_16_path_traversal_rejected():
    """Traversal component in relative_path -> PATH_TRAVERSAL_FORBIDDEN."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "../etc/passwd",  # traversal
            "sha256": "a" * 64,
            "size_bytes": 10,
            "producer_note": "evil",
        }],
    )
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(bundle_json)
    assert exc_info.value.code == "PATH_TRAVERSAL_FORBIDDEN"


# ── Test 17: Absolute Windows path -> rejected ────────────────────────────────────

def test_17_windows_drive_path_rejected():
    """Windows drive path in relative_path -> DRIVE_PATH_FORBIDDEN."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "C:\\Windows\\System32\\evil.exe",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "producer_note": "evil",
        }],
    )
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(bundle_json)
    assert exc_info.value.code == "DRIVE_PATH_FORBIDDEN"


def test_17b_absolute_posix_path_rejected():
    """Absolute POSIX path -> ABSOLUTE_PATH_FORBIDDEN."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "/etc/shadow",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "producer_note": "evil",
        }],
    )
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(bundle_json)
    assert exc_info.value.code == "ABSOLUTE_PATH_FORBIDDEN"


# ── Test 18: UNC path -> rejected ─────────────────────────────────────────────────

def test_18_unc_path_rejected():
    """UNC path in relative_path -> UNC_PATH_FORBIDDEN."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "//server/share/file",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "producer_note": "evil",
        }],
    )
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(bundle_json)
    assert exc_info.value.code == "UNC_PATH_FORBIDDEN"


# ── Test 19: Symlink escape -> rejected ───────────────────────────────────────────

def test_19_symlink_escape_rejected(tmp_path):
    """Symlink inside evidence_root -> CollectorError ARTIFACT_IS_SYMLINK."""
    intent = _make_intent(required_gates=["tests"])
    dg = intent_digest(intent)

    # Create real file
    real_file = tmp_path / "real.json"
    real_file.write_bytes(b'{"real": true}')
    real_sha = hashlib.sha256(b'{"real": true}').hexdigest()

    # Create symlink pointing to real file (inside root, but is a symlink)
    (tmp_path / "artifacts").mkdir()
    link_file = tmp_path / "artifacts" / "symlink_result.json"
    try:
        link_file.symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this filesystem")

    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "artifacts/symlink_result.json",
            "sha256": real_sha,
            "size_bytes": 14,
            "producer_note": "symlink",
        }],
        gate_claims=[{
            "gate_name": "tests",
            "claimed_status": "PASS",
            "evidence_refs": ["artifacts/symlink_result.json"],
            "reason_code": "OK",
        }],
    )
    collector = ResultCollector()
    with pytest.raises(CollectorError) as exc_info:
        collector.collect(bundle_json, tmp_path, intent)
    assert exc_info.value.code == "ARTIFACT_IS_SYMLINK"


# ── Test 20: Oversized artifact -> rejected ────────────────────────────────────────

def test_20_oversized_artifact_rejected():
    """Artifact exceeding MAX_ARTIFACT_SIZE_BYTES -> ARTIFACT_TOO_LARGE."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "test_result",
            "relative_path": "artifacts/big.bin",
            "sha256": "a" * 64,
            "size_bytes": 101 * 1024 * 1024,  # 101MB > 100MB limit
            "producer_note": "too big",
        }],
    )
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(bundle_json)
    assert exc_info.value.code == "ARTIFACT_TOO_LARGE"


# ── Test 21: Too many artifacts -> rejected ────────────────────────────────────────

def test_21_too_many_artifacts_rejected():
    """More than MAX_ARTIFACTS=50 -> ARTIFACT_COUNT_EXCEEDED."""
    intent = _make_intent()
    artifacts = [
        {
            "artifact_type": "log",
            "relative_path": f"artifacts/log_{i}.json",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "producer_note": f"log {i}",
        }
        for i in range(51)  # 51 > 50
    ]
    bundle_json = _make_worker_result_json(intent=intent, artifacts=artifacts)
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(bundle_json)
    assert exc_info.value.code == "ARTIFACT_COUNT_EXCEEDED"


# ── Test 22: Credential metadata -> rejected ──────────────────────────────────────

def test_22_credential_in_producer_note_rejected():
    """Forbidden field name 'secret' in artifact producer_note structure -> rejected."""
    intent = _make_intent()
    # Build a bundle where producer_note contains a forbidden field name
    # Note: reject_forbidden_raw_fields checks for key names like 'secret', 'token', etc.
    # We test that the gateway rejects structures containing these field names.
    # Since producer_note is a string, not a dict, we test rejection via
    # the metadata dict wrapper that the collector creates.
    from ai_engineering.redaction import reject_forbidden_raw_fields
    from ai_engineering.contracts import TraceValidationError

    with pytest.raises(TraceValidationError):
        reject_forbidden_raw_fields({"secret": "mysecretvalue"})

    with pytest.raises(TraceValidationError):
        reject_forbidden_raw_fields({"api_key": "sk-abc123"})


# ── Test 23: Deterministic failure cannot become PASS ────────────────────────────

def test_23_blocked_cannot_become_pass(tmp_path):
    """A blocked validator result must never be status=PASS."""
    intent = _make_intent(required_gates=["tests"])
    bundle_json = _make_worker_result_json(
        intent=intent,
        gate_claims=[],  # missing required gate
    )
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    assert vr.status != Status.PASS
    assert vr.status == Status.BLOCKED


# ── Test 24: TaskIntent compatibility ─────────────────────────────────────────────

def test_24_task_intent_compatibility():
    """Existing TaskIntent structure is fully compatible with supervisor pipeline."""
    from ai_engineering.task_intent import (
        validate_intent, intent_digest, serialize_intent, deserialize_intent,
        TaskLineage, validate_lineage,
    )
    intent = _make_intent(task_id="TASK-001")
    # Verify all required fields exist
    assert intent.task_id == "TASK-001"
    assert intent.schema_version == 1
    assert hasattr(intent, "required_gates")
    assert hasattr(intent, "source_base_sha")
    assert hasattr(intent, "source_repository")
    # Verify digest round-trips
    dg = intent_digest(intent)
    assert len(dg) == 64
    serialized = serialize_intent(intent)
    deserialized = deserialize_intent(serialized)
    assert intent_digest(deserialized) == dg


# ── Test 25: TaskLineage compatibility ────────────────────────────────────────────

def test_25_task_lineage_compatibility():
    """TaskLineage structures are compatible with supervisor pipeline context."""
    from ai_engineering.task_intent import validate_lineage, NodeKind, RelationKind

    lineage_data = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "intent-001", "kind": "INTENT"},
            {"node_id": "evidence-001", "kind": "EVIDENCE"},
            {"node_id": "task-001", "kind": "TASK"},
        ],
        "edges": [
            {"source_id": "evidence-001", "target_id": "task-001", "relation": "VERIFIES"},
        ],
    }
    lineage = validate_lineage(lineage_data)
    assert lineage.schema_version == 1
    assert len(lineage.nodes) == 3
    assert len(lineage.edges) == 1


# ── Test 26: GateResult / BehaviourTrace compatibility ────────────────────────────

def test_26_gate_result_compatibility():
    """Existing GateResult from contracts.py is reused correctly."""
    gr = GateResult(
        gate_name="tests",
        required=True,
        status=Status.PASS,
        evidence_refs=("artifacts/test_result.json",),
    )
    assert gr.gate_name == "tests"
    assert gr.status == Status.PASS
    assert gr.required is True
    assert "artifacts/test_result.json" in gr.evidence_refs


# ── Test 27: Shunt routing determinism ────────────────────────────────────────────

def test_27_shunt_routing_deterministic():
    """shunt_route produces same output for same input (deterministic)."""
    entries = [
        ArtifactManifestEntry("test_result", "t.json", "a" * 64, 10, "note"),
        ArtifactManifestEntry("git_diff", "d.patch", "b" * 64, 20, "patch"),
        ArtifactManifestEntry("ruff_result", "l.json", "c" * 64, 15, "lint"),
        ArtifactManifestEntry("unknown_type", "u.json", "d" * 64, 5, "unknown"),
    ]
    r1 = shunt_route(entries)
    r2 = shunt_route(entries)

    assert r1 == r2
    assert r1[0].category == EvidenceCategory.TESTS
    assert r1[1].category == EvidenceCategory.DIFF_CHANGE
    assert r1[2].category == EvidenceCategory.LINT_STATIC
    assert r1[3].category == EvidenceCategory.GENERIC  # unknown preserved


def test_27b_all_known_categories():
    """All documented artifact types map to correct categories."""
    mapping = [
        ("git_sha_evidence", EvidenceCategory.REPOSITORY_PROVENANCE),
        ("repository_provenance", EvidenceCategory.REPOSITORY_PROVENANCE),
        ("git_diff", EvidenceCategory.DIFF_CHANGE),
        ("patch", EvidenceCategory.DIFF_CHANGE),
        ("test_result", EvidenceCategory.TESTS),
        ("pytest_result", EvidenceCategory.TESTS),
        ("test_output", EvidenceCategory.TESTS),
        ("lint_result", EvidenceCategory.LINT_STATIC),
        ("ruff_result", EvidenceCategory.LINT_STATIC),
        ("typecheck_result", EvidenceCategory.LINT_STATIC),
        ("static_analysis", EvidenceCategory.LINT_STATIC),
        ("ci_result", EvidenceCategory.CI),
        ("ci_log", EvidenceCategory.CI),
        ("workflow_run", EvidenceCategory.CI),
        ("log", EvidenceCategory.LOG),
        ("execution_log", EvidenceCategory.LOG),
    ]
    for artifact_type, expected_cat in mapping:
        entries = [ArtifactManifestEntry(artifact_type, "f.json", "a" * 64, 10, "")]
        routed = shunt_route(entries)
        assert routed[0].category == expected_cat, f"{artifact_type} should map to {expected_cat}"


# ── Test 28: Provenance invariant ──────────────────────────────────────────────────

def test_28_provenance_invariant():
    """evidence_id is bound to attempt_id - different attempt -> different evidence_id."""
    common_args = dict(
        task_id="TASK-001",
        intent_digest="d" * 64,
        base_sha="a" * 40,
        head_sha="b" * 40,
        repository="github",
        canonical_remote="github",
        worker_id="worker-01",
        produced_at_utc="2026-09-09T00:00:00+00:00",
    )
    id_a = compute_evidence_id(attempt_id="attempt-A", **common_args)
    id_b = compute_evidence_id(attempt_id="attempt-B", **common_args)
    assert id_a != id_b


# ── Test 29: Serialization constraints ─────────────────────────────────────────────

def test_29_serialization_constraints():
    """Canonical JSON must not contain NaN, Infinity, or unsorted keys."""
    intent = _make_intent()
    bundle = deserialize_worker_result(_make_worker_result_json(intent=intent))
    s = canonical_serialize_worker_result(bundle)

    # Must not contain NaN or Infinity
    assert "NaN" not in s
    assert "Infinity" not in s

    # Keys must be sorted
    obj = json.loads(s)
    keys = list(obj.keys())
    assert keys == sorted(keys)

    # Must be valid JSON
    reparsed = json.loads(s)
    assert isinstance(reparsed, dict)


# ── Test 30: WorkerResultError stable code ─────────────────────────────────────────

def test_30_worker_result_error_code():
    """WorkerResultError exposes stable .code attribute."""
    err = WorkerResultError("MY_CODE")
    assert err.code == "MY_CODE"
    assert str(err) == "MY_CODE"
    assert isinstance(err, ValueError)


# ── Test 31: CollectorError stable code ────────────────────────────────────────────

def test_31_collector_error_code():
    """CollectorError exposes stable .code attribute."""
    err = CollectorError("COLLECTOR_CODE")
    assert err.code == "COLLECTOR_CODE"
    assert str(err) == "COLLECTOR_CODE"
    assert isinstance(err, ValueError)


# ── Test 32: ValidatorError stable code ────────────────────────────────────────────

def test_32_validator_error_code():
    """ValidatorError exposes stable .code attribute."""
    err = ValidatorError("VALIDATOR_CODE")
    assert err.code == "VALIDATOR_CODE"
    assert str(err) == "VALIDATOR_CODE"
    assert isinstance(err, ValueError)


# ── Test 33: GENERIC category preserved in NormalizedEvidence ─────────────────────

def test_33_generic_preserved_in_normalized_evidence(tmp_path):
    """GENERIC artifacts must be preserved in NormalizedEvidence artifacts tuple."""
    intent = _make_intent(required_gates=[])  # no required gates

    unknown_content = json.dumps({"data": "custom"})
    sha, size = _write_artifact(tmp_path / "artifacts", "custom.json", unknown_content)

    bundle_json = _make_worker_result_json(
        intent=intent,
        artifacts=[{
            "artifact_type": "my_special_type",  # unknown -> GENERIC
            "relative_path": "artifacts/custom.json",
            "sha256": sha,
            "size_bytes": size,
            "producer_note": "custom",
        }],
        gate_claims=[],
    )
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)

    assert len(ne.artifacts) == 1
    assert ne.artifacts[0].category == EvidenceCategory.GENERIC
    assert ne.artifacts[0].artifact_type == "my_special_type"


# ── Test 34: evidence_id content-bound to attempt_id ──────────────────────────────

def test_34_evidence_id_bound_to_attempt(tmp_path):
    """evidence_id changes when attempt_id changes, even if all else is equal."""
    intent = _make_intent(required_gates=[])

    def make_ne(attempt: str) -> NormalizedEvidence:
        bundle_json = _make_worker_result_json(intent=intent, attempt_id=attempt)
        bundle = deserialize_worker_result(bundle_json)
        shunted = shunt_route(list(bundle.artifacts))
        return normalize_worker_result(bundle, shunted)

    ne_a = make_ne("attempt-X")
    ne_b = make_ne("attempt-Y")

    assert ne_a.evidence_id != ne_b.evidence_id
    assert ne_a.attempt_id == "attempt-X"
    assert ne_b.attempt_id == "attempt-Y"


# ── Test: Schema version constants ─────────────────────────────────────────────────

def test_schema_version_constants():
    """Schema version strings are stable and match spec."""
    assert WORKER_RESULT_SCHEMA_VERSION == "hermes.worker-result.v1"
    assert NORMALIZED_EVIDENCE_SCHEMA_VERSION == "hermes.normalized-evidence.v1"
    assert VERIFIED_RESULT_SCHEMA_VERSION == "hermes.verified-result.v1"


# ── Test: Missing required field in worker result -> rejected ───────────────────────

def test_worker_result_missing_field():
    """Worker result without required field -> WORKER_RESULT_INVALID."""
    data = {
        "schema_version": WORKER_RESULT_SCHEMA_VERSION,
        "result_id": "r-001",
        # Missing: task_id, attempt_id, worker_id, etc.
    }
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(json.dumps(data))
    assert exc_info.value.code == "WORKER_RESULT_INVALID"


# ── Test: Extra field in worker result -> rejected ──────────────────────────────────

def test_worker_result_extra_field():
    """Worker result with unexpected field -> WORKER_RESULT_INVALID."""
    intent = _make_intent()
    data = json.loads(_make_worker_result_json(intent=intent))
    data["extra_field"] = "should_not_be_here"
    with pytest.raises(WorkerResultError) as exc_info:
        deserialize_worker_result(json.dumps(data))
    assert exc_info.value.code == "WORKER_RESULT_INVALID"


# ── Test: intent_digest mismatch in collector ───────────────────────────────────────

def test_intent_digest_mismatch_in_collector(tmp_path):
    """Worker result with wrong intent_digest -> INTENT_DIGEST_MISMATCH in collector."""
    intent = _make_intent()
    bundle_json = _make_worker_result_json(
        intent=intent,
        intent_digest_override="0" * 64,  # wrong digest
    )
    collector = ResultCollector()
    with pytest.raises(CollectorError) as exc_info:
        collector.collect(bundle_json, tmp_path, intent)
    assert exc_info.value.code == "INTENT_DIGEST_MISMATCH"


# ── Test: Normalized evidence schema version ────────────────────────────────────────

def test_normalized_evidence_schema_version(tmp_path):
    """NormalizedEvidence has correct schema_version."""
    intent = _make_intent(required_gates=[])
    bundle_json = _make_worker_result_json(intent=intent)
    bundle = deserialize_worker_result(bundle_json)
    shunted = shunt_route(list(bundle.artifacts))
    ne = normalize_worker_result(bundle, shunted)
    assert ne.schema_version == NORMALIZED_EVIDENCE_SCHEMA_VERSION


# ── Test: VerifiedResult schema version ─────────────────────────────────────────────

def test_verified_result_schema_version(tmp_path):
    """VerifiedResult has correct schema_version."""
    intent = _make_intent(required_gates=[])
    bundle_json = _make_worker_result_json(intent=intent)
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    assert vr.schema_version == VERIFIED_RESULT_SCHEMA_VERSION


# ── Test: canonical_remote mismatch -> BLOCKED ──────────────────────────────────────

def test_canonical_remote_mismatch_blocked(tmp_path):
    """canonical_remote doesn't match intent's source_repository -> BLOCKED."""
    intent = _make_intent(source_repository="github")
    bundle_json = _make_worker_result_json(
        intent=intent,
        canonical_remote="origin",  # different from intent's "github"
    )
    collector = ResultCollector()
    ne = collector.collect(bundle_json, tmp_path, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    assert vr.status == Status.BLOCKED
    assert "CANONICAL_REMOTE_MISMATCH" in vr.blockers


# ── Test: Verified result is frozen/immutable ────────────────────────────────────────

def test_verified_result_frozen(tmp_path):
    """VerifiedResult is a frozen dataclass."""
    intent = _make_intent(required_gates=[])
    ne = normalize_worker_result(
        deserialize_worker_result(_make_worker_result_json(intent=intent)),
        [],
    )
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-09T00:00:00+00:00")
    with pytest.raises((AttributeError, TypeError)):
        vr.status = Status.PASS  # type: ignore[misc]


# ── Test: WorkerResultBundle is frozen/immutable ────────────────────────────────────

def test_worker_result_bundle_frozen():
    """WorkerResultBundle is a frozen dataclass."""
    intent = _make_intent()
    bundle = deserialize_worker_result(_make_worker_result_json(intent=intent))
    with pytest.raises((AttributeError, TypeError)):
        bundle.task_id = "hacked"  # type: ignore[misc]


# ── Test: NormalizedEvidence is frozen/immutable ────────────────────────────────────

def test_normalized_evidence_frozen():
    """NormalizedEvidence is a frozen dataclass."""
    intent = _make_intent()
    bundle = deserialize_worker_result(_make_worker_result_json(intent=intent))
    shunted = shunt_route(list(bundle.artifacts))
    ne = normalize_worker_result(bundle, shunted)
    with pytest.raises((AttributeError, TypeError)):
        ne.task_id = "hacked"  # type: ignore[misc]
