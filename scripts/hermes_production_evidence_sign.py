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
    if prov.get("COLLECTOR_REPOSITORY") != "life2boat/hermes":
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

    # 2. Rollback evidence validation
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

    # Baseline digest and revision pairing check
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

    # Rehearsal artifact reference
    canonical_artifact = rb.get("canonical_rehearsal_evidence", "")
    if not canonical_artifact or canonical_artifact == "self-asserted":
        raise ValueError(
            f"Invalid canonical rehearsal evidence format: {canonical_artifact}"
        )

    # Strict rejection of unkeyed or self-referential signature attempts in unsigned provenance
    rb_prov = rb.get("execution_provenance", {})
    if isinstance(rb_prov, dict):
        sig = rb_prov.get("signature")
        if sig and isinstance(sig, str):
            if sig.startswith("sha256:") or (
                len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)
            ):
                raise ValueError(
                    "Unkeyed SHA-256 signature rejected in unsigned rollback evidence"
                )
            if "producer_signature" in rb_prov and sig == rb_prov.get(
                "producer_signature"
            ):
                raise ValueError(
                    "Self-referential signature rejected in unsigned rollback evidence"
                )

    # 3. Schema evidence validation
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

    # 4. Credential risk validation
    cred = unsigned_bundle.get("credential_risk_evidence")
    if not isinstance(cred, dict):
        raise ValueError("Missing or invalid credential_risk_evidence")
    if cred.get("credential_risk_status") != "PROVEN_CLEAR":
        raise ValueError(
            f"Credential risk is not PROVEN_CLEAR: {cred.get('credential_risk_status')}"
        )

    # 5. DB & Secret validations
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
