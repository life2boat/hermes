import argparse
import datetime
import hashlib
import hmac
import json
import os
import sys

from scripts.hermes_canonical_evidence import EXPECTED_FIELDS_MAP, TYPES_MAP


def _verify_signed_provenance(
    ev, expected_sha, evidence_type, expected_fields, key_str
):
    from scripts.hermes_release_qualification import (
        _verify_signed_provenance as verify_inner,
    )

    return verify_inner(ev, expected_sha, evidence_type, expected_fields, key_str)


def compute_canonical_digest_from_dict(d: dict) -> str:
    s = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sign_evidence(ev: dict, key: str) -> dict:
    payload = {
        k: v
        for k, v in ev.items()
        if k not in ("evidence_digest", "execution_provenance")
    }
    prov = ev.get("execution_provenance", {})
    prov_copy = {k: v for k, v in prov.items() if k != "signature"}
    payload["execution_provenance"] = prov_copy

    payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_digest = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

    sig = hmac.new(
        key.encode("utf-8"),
        payload_digest.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    final_evidence = dict(ev)
    prov_out = dict(prov_copy)
    prov_out["signature"] = sig
    final_evidence["execution_provenance"] = prov_out

    if "evidence_digest" in final_evidence:
        del final_evidence["evidence_digest"]

    final_evidence["evidence_digest"] = compute_canonical_digest_from_dict(
        final_evidence
    )
    return final_evidence


def validate_unsigned_evidence(
    unsigned_bundle: dict, target_sha: str, expected_nonce: str = ""
) -> None:
    """Independently validate remote unsigned evidence before signing."""
    # Check all required fields first
    for key_name in TYPES_MAP:
        ev = unsigned_bundle.get(key_name)
        if not isinstance(ev, dict):
            raise ValueError(f"Missing required field {key_name}")
        for f in EXPECTED_FIELDS_MAP[key_name]:
            if f not in ("evidence_digest", "execution_provenance") and f not in ev:
                raise ValueError(f"Missing required field {f} in {key_name}")

    # 1. Collector provenance validation
    prov = unsigned_bundle.get("collector_provenance", {})
    if not isinstance(prov, dict):
        raise ValueError("Missing collector_provenance")

    valid_repos = {
        "life2boat/hermes",
        "https://github.com/life2boat/hermes",
        "git@github.com:life2boat/hermes",
        "git@github-healbite:life2boat/hermes",
    }
    col_repo = str(prov.get("COLLECTOR_REPOSITORY", "")).strip()
    if col_repo.endswith(".git"):
        col_repo = col_repo[:-4]
    if col_repo not in valid_repos:
        raise ValueError(
            f"Collector repository mismatch: {prov.get('COLLECTOR_REPOSITORY')}"
        )

    if prov.get("COLLECTOR_HEAD_SHA") != target_sha:
        raise ValueError(
            f"Collector head SHA mismatch: {prov.get('COLLECTOR_HEAD_SHA')} != {target_sha}"
        )
    if prov.get("COLLECTOR_WORKTREE_CLEAN") is not True:
        raise ValueError("Collector worktree was not clean")

    if expected_nonce:
        collector_nonce = prov.get("EXECUTION_NONCE", "")
        if not collector_nonce or collector_nonce != expected_nonce:
            raise ValueError(
                f"Execution nonce mismatch: expected {expected_nonce}, got {collector_nonce}"
            )

    # 2. Raw rehearsal receipt and raw health evidence validation
    raw_receipt = unsigned_bundle.get("raw_rehearsal_receipt")
    raw_health = unsigned_bundle.get("raw_rehearsal_health")
    if not isinstance(raw_receipt, dict):
        raise ValueError("Missing or invalid raw_rehearsal_receipt in unsigned bundle")
    if not isinstance(raw_health, dict):
        raise ValueError("Missing or invalid raw_rehearsal_health in unsigned bundle")

    if raw_receipt.get("execution_nonce") != raw_health.get("execution_nonce"):
        raise ValueError(
            f"Nonce mismatch between raw receipt ({raw_receipt.get('execution_nonce')}) and raw health ({raw_health.get('execution_nonce')})"
        )
    if prov.get("EXECUTION_NONCE") and raw_receipt.get("execution_nonce") != prov.get(
        "EXECUTION_NONCE"
    ):
        raise ValueError(
            f"Nonce mismatch between raw receipt ({raw_receipt.get('execution_nonce')}) and collector provenance ({prov.get('EXECUTION_NONCE')})"
        )

    if expected_nonce:
        if raw_receipt.get("execution_nonce") != expected_nonce:
            raise ValueError(
                f"Raw receipt execution nonce mismatch: expected {expected_nonce}, got {raw_receipt.get('execution_nonce')}"
            )
        if raw_health.get("execution_nonce") != expected_nonce:
            raise ValueError(
                f"Raw health execution nonce mismatch: expected {expected_nonce}, got {raw_health.get('execution_nonce')}"
            )
        receipt_id = raw_receipt.get("receipt_id", "")
        if (
            not receipt_id.startswith("receipt-rollback-")
            or expected_nonce[:16] not in receipt_id
        ):
            raise ValueError(f"Receipt ID is not properly nonce-bound: {receipt_id}")

    if raw_receipt.get("target_sha") != target_sha:
        raise ValueError(
            f"Raw receipt target SHA mismatch: {raw_receipt.get('target_sha')} != {target_sha}"
        )
    if raw_health.get("target_sha") != target_sha:
        raise ValueError(
            f"Raw health target SHA mismatch: {raw_health.get('target_sha')} != {target_sha}"
        )

    # Recompute raw health evidence SHA-256 and assert match with receipt
    computed_health_hash = hashlib.sha256(
        json.dumps(raw_health, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    receipt_health_hash = raw_receipt.get("health_evidence_sha256", "")

    raw_health_raw = unsigned_bundle.get("raw_rehearsal_health_raw")
    if raw_health_raw:
        computed_raw_str_hash = hashlib.sha256(
            raw_health_raw.encode("utf-8")
        ).hexdigest()
        if (
            computed_raw_str_hash != receipt_health_hash
            and computed_health_hash != receipt_health_hash
        ):
            raise ValueError("Raw health evidence SHA-256 mismatch with receipt")
    elif computed_health_hash != receipt_health_hash:
        compact_hash = hashlib.sha256(
            json.dumps(raw_health, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if compact_hash != receipt_health_hash:
            raise ValueError("Raw health evidence SHA-256 mismatch with receipt")

    from scripts.generate_offline_evidence import TRUSTED_REHEARSAL_PRODUCERS

    producer = raw_receipt.get("producer") or raw_receipt.get("rehearsal_producer", "")
    if not producer or producer not in TRUSTED_REHEARSAL_PRODUCERS:
        raise ValueError(f"Untrusted or missing rehearsal producer: {producer}")

    executed_at_str = raw_receipt.get("executed_at", "")
    if not executed_at_str:
        raise ValueError("Missing executed_at in raw receipt")
    try:
        executed_dt = datetime.datetime.fromisoformat(
            executed_at_str.replace("Z", "+00:00")
        )
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        delta_sec = (now_dt - executed_dt).total_seconds()
        if delta_sec < -60 or delta_sec > 900:  # Fresh within last 15 minutes
            raise ValueError(
                f"Stale or future rehearsal receipt timestamp: {executed_at_str} (delta: {delta_sec}s)"
            )
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Invalid executed_at timestamp in receipt: {e}")

    # Real observed container and procedure results
    if raw_receipt.get("forward_container_started") is not True:
        raise ValueError("Forward container was not observed running")
    if raw_receipt.get("rollback_container_started") is not True:
        raise ValueError("Rollback container was not observed running")
    if raw_receipt.get("forward_exit_code") != 0:
        raise ValueError(
            f"Forward container exited with non-zero code: {raw_receipt.get('forward_exit_code')}"
        )
    if raw_receipt.get("rollback_exit_code") != 0:
        raise ValueError(
            f"Rollback container exited with non-zero code: {raw_receipt.get('rollback_exit_code')}"
        )
    if raw_receipt.get("rollback_procedure_proven") is not True:
        raise ValueError("Rollback procedure was not proven in receipt")
    if (
        raw_receipt.get("status") != "PASS"
        or raw_receipt.get("health_check_status") != "PASS"
    ):
        raise ValueError("Receipt does not report PASS")

    if raw_health.get("health_status") != "PASS":
        raise ValueError(
            f"Rollback health status is not PASS: {raw_health.get('health_status')}"
        )
    if raw_health.get("health_cmd") != "hermes gateway status":
        raise ValueError(f"Unexpected health command: {raw_health.get('health_cmd')}")
    if raw_health.get("health_exit_code") != 0:
        raise ValueError(
            f"Health check exited with code: {raw_health.get('health_exit_code')}"
        )
    if "Gateway is running" not in str(raw_health.get("health_stdout", "")):
        raise ValueError("Health command stdout does not confirm running gateway")

    if (
        raw_health.get("sqlite_pre_integrity") != "ok"
        or raw_health.get("sqlite_post_integrity") != "ok"
    ):
        raise ValueError("SQLite integrity check failed during drill")
    if (
        raw_health.get("sqlite_pre_fk_violations") != 0
        or raw_health.get("sqlite_post_fk_violations") != 0
    ):
        raise ValueError("SQLite foreign key violations detected during drill")
    if raw_health.get("qdrant_unmodified") is not True:
        raise ValueError("Qdrant mutation detected during drill")
    if raw_health.get("canonical_mount_target") != "/home/hermes/healbite.db":
        raise ValueError(
            f"Invalid canonical mount target: {raw_health.get('canonical_mount_target')}"
        )
    if raw_health.get("database_restoration_proven") is not True:
        raise ValueError("Database restoration was not proven in drill")
    if raw_health.get("incompatible_schema_rollback_proven") is not True:
        raise ValueError("Incompatible schema rollback was not proven in drill")
    if raw_health.get("compose_configuration_verified") is not True:
        raise ValueError("Compose configuration was not verified in drill")

    # Strict rejection of unkeyed or self-referential or collector markers
    receipt_prov = raw_receipt.get("execution_provenance", {})
    if isinstance(receipt_prov, dict):
        sig = receipt_prov.get("signature")
        if sig and isinstance(sig, str):
            if sig == "UNSIGNED_COLLECTOR_EXECUTION":
                raise ValueError(
                    "Unauthenticated collector marker rejected in raw receipt"
                )
            if sig.startswith("sha256:") or (
                len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)
            ):
                raise ValueError("Unkeyed SHA-256 signature rejected in raw receipt")

    # 3. Rollback evidence validation
    rb = unsigned_bundle.get("rollback_evidence")
    if not isinstance(rb, dict):
        raise ValueError("Missing or invalid rollback_evidence")
    if rb.get("status") != "PASS":
        raise ValueError(
            f"Verification failed for rollback_evidence: status is {rb.get('status')}"
        )
    if rb.get("target_sha") != target_sha:
        raise ValueError(
            f"Rollback evidence target SHA mismatch: {rb.get('target_sha')} != {target_sha}"
        )
    if rb.get("rollback_procedure_proven") is not True:
        raise ValueError("Rollback procedure was not proven")

    # Baseline digest and revision pairing check across receipt, health, and rollback evidence
    roll_digest = str(rb.get("rollback_image_digest", ""))
    curr_digest = str(rb.get("current_production_image_digest", ""))
    roll_rev = str(rb.get("rollback_revision", ""))
    curr_rev = str(rb.get("current_production_oci_revision", ""))

    if not roll_digest.startswith("sha256:") or not curr_digest.startswith("sha256:"):
        raise ValueError(f"Invalid rollback digest format: {roll_digest}")
    if not roll_rev or roll_rev != curr_rev:
        raise ValueError(f"Baseline revision mismatch: {roll_rev} != {curr_rev}")
    if roll_digest != curr_digest:
        raise ValueError(f"Rollback digest mismatch: {roll_digest} != {curr_digest}")

    if raw_receipt.get("rollback_image_digest") != roll_digest:
        raise ValueError("Raw receipt image digest mismatch with rollback evidence")
    if raw_receipt.get("rollback_revision") != roll_rev:
        raise ValueError("Raw receipt revision mismatch with rollback evidence")

    # 4. Schema evidence validation
    sch = unsigned_bundle.get("schema_evidence")
    if not isinstance(sch, dict):
        raise ValueError("Missing or invalid schema_evidence")
    if sch.get("status") != "PASS":
        raise ValueError(
            f"Verification failed for schema_evidence: status is {sch.get('status')}"
        )
    if sch.get("target_sha") != target_sha:
        raise ValueError("Schema evidence target SHA mismatch")
    if sch.get("actual_schema_digest") != sch.get("expected_schema_digest"):
        raise ValueError("Schema evidence digest mismatch with canonical contract")
    if str(sch.get("schema_delta", "")).upper() not in ("NONE", ""):
        raise ValueError(f"Schema delta detected: {sch.get('schema_delta')}")
    if sch.get("integrity_status") != "ok":
        raise ValueError(
            f"Schema integrity check failed: {sch.get('integrity_status')}"
        )
    if sch.get("foreign_key_violation_count") != 0:
        raise ValueError(
            f"Foreign key violations found: {sch.get('foreign_key_violation_count')}"
        )
    if sch.get("migration_required") is not False:
        raise ValueError("Migration required in schema evidence")

    # 5. Credential risk validation
    cred = unsigned_bundle.get("credential_risk_evidence")
    if not isinstance(cred, dict):
        raise ValueError("Missing or invalid credential_risk_evidence")
    if cred.get("credential_risk_status") != "PROVEN_CLEAR":
        raise ValueError(
            f"Credential risk is not PROVEN_CLEAR: {cred.get('credential_risk_status')}"
        )

    # 6. DB & Secret validations
    db = unsigned_bundle.get("db_evidence")
    if not isinstance(db, dict) or db.get("status") != "PASS":
        raise ValueError("Verification failed for db_evidence")

    sec = unsigned_bundle.get("secret_evidence")
    if not isinstance(sec, dict) or sec.get("status") != "PASS":
        raise ValueError("Verification failed for secret_evidence")


def main():
    parser = argparse.ArgumentParser(
        description="Hermes canonical production evidence signer"
    )
    parser.add_argument("target_sha", help="Target canonical SHA")
    parser.add_argument(
        "--nonce",
        default="",
        help="Expected execution nonce for replay protection",
    )
    args = parser.parse_args()

    target_sha = args.target_sha
    if (
        not target_sha
        or not all(c in "0123456789abcdef" for c in target_sha)
        or len(target_sha) != 40
    ):
        print(json.dumps({"error": "Invalid target SHA"}))
        sys.exit(1)

    key = os.environ.get("HERMES_PROVENANCE_KEY", "")
    if not key:
        print(json.dumps({"error": "Missing provenance key"}))
        sys.exit(1)

    expected_nonce = args.nonce or os.environ.get("EXECUTION_NONCE", "")

    try:
        input_data = sys.stdin.read().strip()
        if not input_data:
            print(json.dumps({"error": "Missing unsigned evidence"}))
            sys.exit(1)

        unsigned_bundle = json.loads(input_data)
        if "error" in unsigned_bundle:
            print(json.dumps({"error": unsigned_bundle["error"]}))
            sys.exit(1)

        # Fail-closed independent verification of remote unsigned evidence
        validate_unsigned_evidence(unsigned_bundle, target_sha, expected_nonce)

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    signed_bundle = {}

    try:
        # Sign rehearsal receipt with CI provenance key and bind canonical reference
        raw_receipt = unsigned_bundle.get("raw_rehearsal_receipt")
        if raw_receipt:
            from scripts.generate_offline_evidence import (
                compute_rollback_rehearsal_payload,
            )

            receipt_payload = compute_rollback_rehearsal_payload(
                receipt_id=raw_receipt["receipt_id"],
                target_sha=target_sha,
                image_digest=raw_receipt["rollback_image_digest"],
                oci_revision=raw_receipt["rollback_revision"],
                health_hash=raw_receipt["health_evidence_sha256"],
                producer_identity=raw_receipt["producer"],
                executed_at=raw_receipt["executed_at"],
                rehearsal_type=raw_receipt["rehearsal_type"],
                result_status=raw_receipt["status"],
            )
            rehearsal_sig = hmac.new(
                key.encode("utf-8"), receipt_payload, hashlib.sha256
            ).hexdigest()
            raw_receipt["execution_provenance"]["signature"] = rehearsal_sig
            receipt_sha256 = hashlib.sha256(
                json.dumps(raw_receipt, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if "rollback_evidence" in unsigned_bundle:
                unsigned_bundle["rollback_evidence"]["canonical_rehearsal_evidence"] = (
                    f"artifact:rollback-rehearsal:{raw_receipt['receipt_id']}:{receipt_sha256[:16]}:pass"
                )

        for key_name, ev_type in TYPES_MAP.items():
            ev = unsigned_bundle.get(key_name)
            if not ev:
                raise Exception(f"Missing {key_name}")

            signed_ev = sign_evidence(ev, key)

            required_fields = EXPECTED_FIELDS_MAP[key_name]
            for f in required_fields:
                if f != "evidence_digest" and f != "execution_provenance":
                    if f not in ev:
                        raise Exception(f"Missing required field {f} in {key_name}")

            err = _verify_signed_provenance(
                signed_ev, target_sha, ev_type, required_fields, key
            )
            if err:
                raise Exception(f"Verification failed for {key_name}: {err}")

            signed_bundle[key_name] = signed_ev

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(json.dumps(signed_bundle, separators=(",", ":")))


if __name__ == "__main__":
    main()
