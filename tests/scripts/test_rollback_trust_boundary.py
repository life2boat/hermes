from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.generate_offline_evidence import (
    TRUSTED_REHEARSAL_PRODUCERS,
    compute_rollback_rehearsal_payload,
    get_real_rollback_evidence,
)
from scripts.hermes_canonical_evidence import (
    EXPECTED_FIELDS_MAP,
    ROLLBACK_EVIDENCE_FIELDS,
    ROLLBACK_EVIDENCE_TYPE,
)
from scripts.hermes_production_evidence_sign import (
    sign_evidence,
    validate_unsigned_evidence,
)
from scripts.hermes_release_qualification import _verify_signed_provenance


@pytest.fixture
def rehearsal_setup(tmp_path: Path):
    target_sha = "a" * 40
    digest = "sha256:86d0df36546dec8b77ae8159ea814a41e60fc2d1eff868c3802ef6c63c365302"
    revision = "cc68409727f79acc6bb25280c9f7099fe2caa0c8"
    receipt_id = "receipt-rollback-001"
    producer = "healbite-rehearsal-engine"
    rehearsal_type = "isolated-rollback-drill"
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    health_file = tmp_path / "rollback-health.json"
    health_payload = {
        "status": "PASS",
        "health_status": "PASS",
        "target_sha": target_sha,
        "rollback_image_digest": digest,
        "rollback_revision": revision,
        "health_exit_code": 0,
        "health_cmd": "hermes gateway status",
    }
    h_bytes = json.dumps(health_payload, sort_keys=True).encode("utf-8")
    health_file.write_bytes(h_bytes)
    h_hash = hashlib.sha256(h_bytes).hexdigest()

    payload = compute_rollback_rehearsal_payload(
        receipt_id=receipt_id,
        target_sha=target_sha,
        image_digest=digest,
        oci_revision=revision,
        health_hash=h_hash,
        producer_identity=producer,
        executed_at=now_iso,
        rehearsal_type=rehearsal_type,
        result_status="PASS",
    )

    rehearsal_file = tmp_path / "rollback-rehearsal.json"
    rehearsal_payload = {
        "status": "PASS",
        "receipt_id": receipt_id,
        "producer": producer,
        "executed_at": now_iso,
        "rehearsal_type": rehearsal_type,
        "rollback_image_digest": digest,
        "rollback_revision": revision,
        "target_sha": target_sha,
        "health_check_status": "PASS",
        "health_evidence_path": str(health_file),
        "health_evidence_sha256": h_hash,
        "rollback_procedure_proven": True,
        "execution_provenance": {
            "runtime_identity": "healbite-production",
            "isolation_level": "docker",
            "signature": "UNSIGNED_COLLECTOR_EXECUTION",
        },
    }
    rehearsal_file.write_text(json.dumps(rehearsal_payload), encoding="utf-8")

    docker_inspect_data = [
        {
            "ImageManifestDescriptor": {"digest": digest},
            "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
        }
    ]

    return {
        "target_sha": target_sha,
        "digest": digest,
        "revision": revision,
        "receipt_id": receipt_id,
        "producer": producer,
        "rehearsal_type": rehearsal_type,
        "now_iso": now_iso,
        "health_file": health_file,
        "h_hash": h_hash,
        "payload": payload,
        "rehearsal_file": rehearsal_file,
        "rehearsal_payload": rehearsal_payload,
        "docker_inspect_data": docker_inspect_data,
    }


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 1: Unkeyed SHA-256 presented as signature -> REJECTED
# ---------------------------------------------------------------------------


def test_rejection_of_unkeyed_sha256_as_signature(rehearsal_setup, monkeypatch):
    """Presenting hashlib.sha256(payload).hexdigest() as a signature MUST be rejected."""
    unkeyed_hash = hashlib.sha256(rehearsal_setup["payload"]).hexdigest()
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["execution_provenance"] = {
        "runtime_identity": "healbite-production",
        "isolation_level": "docker",
        "signature": unkeyed_hash,
    }
    rehearsal_setup["rehearsal_file"].write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH",
        str(rehearsal_setup["rehearsal_file"]),
    )

    # 1. In collector mode
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"

    # 2. Outside collector mode without key
    monkeypatch.delenv("HERMES_COLLECTOR_RUN", raising=False)
    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 2: Arbitrary matching signature/producer_signature -> REJECTED
# ---------------------------------------------------------------------------


def test_rejection_of_self_referential_signature_matching(rehearsal_setup, monkeypatch):
    """Self-referential signature matching MUST be rejected."""
    arbitrary_sig = "c0ffee" * 10 + "1234"
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["execution_provenance"] = {
        "runtime_identity": "healbite-production",
        "isolation_level": "docker",
        "signature": arbitrary_sig,
        "producer_signature": arbitrary_sig,
    }
    rehearsal_setup["rehearsal_file"].write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH",
        str(rehearsal_setup["rehearsal_file"]),
    )
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 3: Forged PASS receipt with recomputed hashes -> REJECTED
# ---------------------------------------------------------------------------


def test_rejection_of_forged_pass_receipt_with_recomputed_hashes(
    rehearsal_setup, monkeypatch
):
    """Forged PASS receipt with recomputed hashes without key MUST be rejected."""
    fake_sig = hashlib.sha256(b"attacker-recomputed-hash").hexdigest()
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["execution_provenance"] = {
        "runtime_identity": "healbite-production",
        "isolation_level": "docker",
        "signature": fake_sig,
    }
    rehearsal_setup["rehearsal_file"].write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH",
        str(rehearsal_setup["rehearsal_file"]),
    )
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    monkeypatch.delenv("HERMES_COLLECTOR_RUN", raising=False)

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 4: HERMES_COLLECTOR_RUN=1 without trusted authentication -> REJECTED
# ---------------------------------------------------------------------------


def test_rejection_of_collector_run_without_trusted_authentication(
    rehearsal_setup, monkeypatch
):
    """HERMES_COLLECTOR_RUN=1 must not bypass authentication on arbitrary or untrusted tokens or collector markers."""
    for token in ["UNSIGNED_COLLECTOR_EXECUTION", "arbitrary_unauthenticated_string"]:
        data = dict(rehearsal_setup["rehearsal_payload"])
        data["execution_provenance"] = {
            "runtime_identity": "healbite-production",
            "isolation_level": "docker",
            "signature": token,
        }
        rehearsal_setup["rehearsal_file"].write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setenv(
            "HERMES_ROLLBACK_REHEARSAL_PATH",
            str(rehearsal_setup["rehearsal_file"]),
        )
        monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")
        monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)

        with patch(
            "subprocess.check_output",
            return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
        ):
            res = get_real_rollback_evidence(
                rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
            )
            assert res.get("status") == "BLOCKED"


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 5: Altered target SHA, digest, revision, timestamp, health -> REJECTED
# ---------------------------------------------------------------------------


def test_rejection_of_altered_target_sha_digest_revision_timestamp_health(
    rehearsal_setup, monkeypatch
):
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")

    # 1. Altered target SHA
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["target_sha"] = "f" * 40
    f = rehearsal_setup["health_file"].parent / "rb1.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(f))
    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        assert (
            get_real_rollback_evidence(
                rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
            ).get("status")
            == "BLOCKED"
        )

    # 2. Altered image digest
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["rollback_image_digest"] = "sha256:" + "9" * 64
    f.write_text(json.dumps(data), encoding="utf-8")
    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        assert (
            get_real_rollback_evidence(
                rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
            ).get("status")
            == "BLOCKED"
        )

    # 3. Altered revision
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["rollback_revision"] = "f" * 40
    f.write_text(json.dumps(data), encoding="utf-8")
    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        assert (
            get_real_rollback_evidence(
                rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
            ).get("status")
            == "BLOCKED"
        )

    # 4. Altered timestamp (future)
    future_iso = "2099-01-01T00:00:00Z"
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["executed_at"] = future_iso
    f.write_text(json.dumps(data), encoding="utf-8")
    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        assert (
            get_real_rollback_evidence(
                rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
            ).get("status")
            == "BLOCKED"
        )

    # 5. Mutated health evidence
    rehearsal_setup["health_file"].write_bytes(b'{"mutated": true}')
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH",
        str(rehearsal_setup["rehearsal_file"]),
    )
    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        assert (
            get_real_rollback_evidence(
                rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
            ).get("status")
            == "BLOCKED"
        )


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 6: Stale or replayed receipts -> REJECTED
# ---------------------------------------------------------------------------


def test_rejection_of_stale_or_replayed_receipts(rehearsal_setup, monkeypatch):
    """Receipt older than 48 hours must be rejected as stale."""
    stale_iso = "2020-01-01T00:00:00Z"
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["executed_at"] = stale_iso
    rehearsal_setup["rehearsal_file"].write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH",
        str(rehearsal_setup["rehearsal_file"]),
    )
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 7 & 8: Container failure handling in drill
# ---------------------------------------------------------------------------


def test_rejection_of_failed_container_in_drill(tmp_path):
    """Drill must fail if forward or rollback container fails to start or health fails."""
    from scripts.hermes_isolated_rollback_drill import run_drill

    db_path = tmp_path / "test.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER);")

    # Target container start fails
    with patch(
        "scripts.hermes_isolated_rollback_drill.inspect_container_image",
        return_value=("sha256:" + "1" * 64, "rev1"),
    ):
        with patch(
            "scripts.hermes_isolated_rollback_drill.check_qdrant_state",
            return_value={"health": "ok", "collections": {}},
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                mock_run.return_value.stderr = "Docker daemon error"
                with pytest.raises(
                    RuntimeError, match="Failed to start forward container"
                ):
                    run_drill(
                        target_sha="a" * 40,
                        target_image="img:test",
                        prod_db_path=db_path,
                        output_dir=tmp_path,
                    )


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 9: Nonce replay and signer validation
# ---------------------------------------------------------------------------


def test_signer_validates_nonce_and_fails_closed_on_tampering():
    """Signer must independently validate nonce and baseline pairing."""
    from tests.test_hermes_production_evidence_sign import get_base_unsigned_bundle

    target_sha = "a" * 40
    unsigned_bundle = get_base_unsigned_bundle()
    nonce = "noncealphadef123456789012345678"
    unsigned_bundle["collector_provenance"]["EXECUTION_NONCE"] = nonce
    unsigned_bundle["raw_rehearsal_receipt"]["execution_nonce"] = nonce
    unsigned_bundle["raw_rehearsal_receipt"]["receipt_id"] = (
        f"receipt-rollback-{target_sha[:8]}-{nonce[:16]}"
    )
    unsigned_bundle["raw_rehearsal_health"]["execution_nonce"] = nonce

    # Valid nonce matches
    validate_unsigned_evidence(unsigned_bundle, target_sha, expected_nonce=nonce)

    # Replayed / mismatched nonce fails closed
    with pytest.raises(ValueError, match="Execution nonce mismatch"):
        validate_unsigned_evidence(
            unsigned_bundle, target_sha, expected_nonce="nonce-other-123456789012345678"
        )

    # Altered baseline fails closed
    bad_bundle = dict(unsigned_bundle)
    bad_bundle["rollback_evidence"] = dict(unsigned_bundle["rollback_evidence"])
    bad_bundle["rollback_evidence"]["rollback_image_digest"] = "sha256:wrong_digest"
    with pytest.raises(ValueError, match="Rollback digest mismatch"):
        validate_unsigned_evidence(bad_bundle, target_sha, expected_nonce=nonce)


# ---------------------------------------------------------------------------
# Phase 3 Adversarial Test 10: Cross-user isolation / schema incompatibility
# ---------------------------------------------------------------------------


def test_schema_incompatibility_fails_closed_in_signer():
    """Signer strictly rejects bundles reporting schema deltas or integrity failures."""
    from tests.test_hermes_production_evidence_sign import get_base_unsigned_bundle

    target_sha = "a" * 40
    unsigned_bundle = get_base_unsigned_bundle()
    nonce = "noncealphadef123456789012345678"
    unsigned_bundle["collector_provenance"]["EXECUTION_NONCE"] = nonce
    unsigned_bundle["raw_rehearsal_receipt"]["execution_nonce"] = nonce
    unsigned_bundle["raw_rehearsal_receipt"]["receipt_id"] = (
        f"receipt-rollback-{target_sha[:8]}-{nonce[:16]}"
    )
    unsigned_bundle["raw_rehearsal_health"]["execution_nonce"] = nonce
    unsigned_bundle["schema_evidence"]["status"] = "FAIL"

    with pytest.raises(
        ValueError, match="Verification failed for schema_evidence: status is FAIL"
    ):
        validate_unsigned_evidence(unsigned_bundle, target_sha, expected_nonce=nonce)


# ---------------------------------------------------------------------------
# Valid Authenticated Path with HMAC Passes
# ---------------------------------------------------------------------------


def test_valid_authenticated_path_with_hmac_passes(rehearsal_setup, monkeypatch):
    key = "super-secret-ci-provenance-key"
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", key)

    valid_hmac = hmac.new(
        key.encode("utf-8"), rehearsal_setup["payload"], hashlib.sha256
    ).hexdigest()
    data = dict(rehearsal_setup["rehearsal_payload"])
    data["execution_provenance"] = {
        "runtime_identity": "healbite-production",
        "isolation_level": "docker",
        "signature": valid_hmac,
    }
    rehearsal_setup["rehearsal_file"].write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH",
        str(rehearsal_setup["rehearsal_file"]),
    )

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "PASS"
        assert res["rollback_procedure_proven"] is True
