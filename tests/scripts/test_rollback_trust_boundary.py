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
from scripts.hermes_release_qualification import _verify_signed_provenance


@pytest.fixture
def rehearsal_setup(tmp_path: Path):
    target_sha = "a" * 40
    digest = "sha256:" + "1" * 64
    revision = "b" * 40
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
    producer_sig = hashlib.sha256(payload).hexdigest()

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
            "signature": producer_sig,
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
        "producer_sig": producer_sig,
        "rehearsal_file": rehearsal_file,
        "rehearsal_payload": rehearsal_payload,
        "docker_inspect_data": docker_inspect_data,
    }


def test_missing_key_and_no_collector_fails_closed(rehearsal_setup, monkeypatch):
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    monkeypatch.delenv("HERMES_COLLECTOR_RUN", raising=False)
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_setup["rehearsal_file"])
    )

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


def test_collector_run_with_valid_producer_hash_passes(rehearsal_setup, monkeypatch):
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_setup["rehearsal_file"])
    )

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "PASS"
        assert res["schema_version"] == 1
        assert res["evidence_type"] == "rollback_ready"
        assert res["target_sha"] == rehearsal_setup["target_sha"]
        assert res["rollback_image_digest"] == rehearsal_setup["digest"]
        assert res["rollback_revision"] == rehearsal_setup["revision"]
        assert res["rollback_procedure_proven"] is True
        assert res["canonical_rehearsal_evidence"].startswith(
            f"artifact:rollback-rehearsal:{rehearsal_setup['receipt_id']}:"
        )


def test_collector_run_rejects_tampered_executed_at(rehearsal_setup, monkeypatch):
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")

    data = dict(rehearsal_setup["rehearsal_payload"])
    data["executed_at"] = "2026-10-14T00:00:00Z"  # Tampered timestamp
    rehearsal_file = rehearsal_setup["rehearsal_file"]
    rehearsal_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


def test_collector_run_rejects_tampered_target_sha(rehearsal_setup, monkeypatch):
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")

    data = dict(rehearsal_setup["rehearsal_payload"])
    data["target_sha"] = "c" * 40  # Tampered SHA
    rehearsal_file = rehearsal_setup["rehearsal_file"]
    rehearsal_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


def test_collector_run_rejects_health_hash_mismatch(rehearsal_setup, monkeypatch):
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")

    rehearsal_setup["health_file"].write_bytes(b'{"mutated": true}')
    monkeypatch.setenv(
        "HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_setup["rehearsal_file"])
    )

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


def test_collector_run_rejects_untrusted_producer(rehearsal_setup, monkeypatch):
    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)
    monkeypatch.setenv("HERMES_COLLECTOR_RUN", "1")

    data = dict(rehearsal_setup["rehearsal_payload"])
    data["producer"] = "untrusted-attacker"
    rehearsal_file = rehearsal_setup["rehearsal_file"]
    rehearsal_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


def test_key_verification_passes_with_hmac(rehearsal_setup, monkeypatch):
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
    rehearsal_file = rehearsal_setup["rehearsal_file"]
    rehearsal_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "PASS"


def test_key_verification_fails_with_invalid_hmac(rehearsal_setup, monkeypatch):
    key = "super-secret-ci-provenance-key"
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", key)

    data = dict(rehearsal_setup["rehearsal_payload"])
    data["execution_provenance"] = {
        "runtime_identity": "healbite-production",
        "isolation_level": "docker",
        "signature": "f" * 64,  # Invalid HMAC
    }
    rehearsal_file = rehearsal_setup["rehearsal_file"]
    rehearsal_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))

    with patch(
        "subprocess.check_output",
        return_value=json.dumps(rehearsal_setup["docker_inspect_data"]).encode(),
    ):
        res = get_real_rollback_evidence(
            rehearsal_setup["target_sha"], rehearsal_setup["now_iso"]
        )
        assert res.get("status") == "BLOCKED"


def test_production_signer_signs_and_verifies_rollback_evidence(rehearsal_setup):
    from scripts.hermes_production_evidence_sign import sign_evidence

    key = "test-ci-anchor-key"
    target_sha = rehearsal_setup["target_sha"]
    digest = rehearsal_setup["digest"]
    revision = rehearsal_setup["revision"]

    unsigned_rollback = {
        "schema_version": 1,
        "evidence_type": "rollback_ready",
        "target_sha": target_sha,
        "status": "PASS",
        "collected_at_utc": rehearsal_setup["now_iso"],
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "healbite-production",
        },
        "current_production_image_digest": digest,
        "current_production_oci_revision": revision,
        "rollback_image_digest": digest,
        "rollback_image_resolvable": True,
        "rollback_revision": revision,
        "rollback_mechanism_id": "docker-compose-revert",
        "same_compose_chain": True,
        "database_restore_required": False,
        "schema_downgrade_required": False,
        "rollback_health_required": True,
        "rollback_attempt_count_max": 1,
        "rollback_procedure_proven": True,
        "canonical_rehearsal_evidence": f"artifact:rollback-rehearsal:{rehearsal_setup['receipt_id']}:1234567890abcdef:pass",
    }

    signed = sign_evidence(unsigned_rollback, key)
    assert "signature" in signed["execution_provenance"]
    assert "evidence_digest" in signed

    err = _verify_signed_provenance(
        signed, target_sha, ROLLBACK_EVIDENCE_TYPE, ROLLBACK_EVIDENCE_FIELDS, key
    )
    assert err is None
