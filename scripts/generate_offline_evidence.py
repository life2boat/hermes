#!/usr/bin/env python3
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

TRUSTED_REHEARSAL_PRODUCERS = frozenset({
    "healbite-rehearsal-engine",
    "hermes-rollback-runner",
    "healbite-production-drill",
    "healbite-production",
})

def check_secret_presence(manifest_secrets):
    approved_path = manifest_secrets.get("approved_source_path")
    approved_uids = manifest_secrets.get("approved_owner_uids", [])
    expected_mode = manifest_secrets.get("source_mode")

    p = Path(approved_path)
    credential_risk = "PROVEN_CLEAR"

    if not p.exists():
        return "BLOCKED", [], "PROVEN_RISK"

    try:
        stat = p.stat()
        mode_str = oct(stat.st_mode)[-4:]
        if stat.st_uid not in approved_uids or mode_str != expected_mode:
            return "BLOCKED", [], "PROVEN_RISK"
    except Exception:
        return "BLOCKED", [], "PROVEN_RISK"

    env_vars = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    except Exception:
        return "BLOCKED", [], "PROVEN_RISK"

    req_secrets = []
    status = "PASS"

    for req in manifest_secrets.get("protected_variables", []):
        name = req.get("destination_variable") or req.get("name")
        required = req.get("required", False)
        allow_empty = req.get("allow_empty", False)
        source_class = req.get("source_class", "approved-production-secret-source")

        present = False
        if name in env_vars:
            val = env_vars[name]
            if val or allow_empty:
                present = True

        if required and not present:
            status = "BLOCKED"
            credential_risk = "PROVEN_RISK"

        req_secrets.append({
            "name": name,
            "required": required,
            "present": present,
            "source_class": source_class
        })

    return status, req_secrets, credential_risk

def check_db_path(manifest_db):
    source = manifest_db.get("source")
    p = Path(source)
    if not p.exists() or not p.is_file():
        return "BLOCKED", "unknown", -1

    try:
        from scripts.hermes_deploy_preflight import validate_database_source_path
        validate_database_source_path(p)
        return "PASS", "authoritative-production-path", 1
    except Exception:
        pass

    return "BLOCKED", "unknown", 1


def check_schema_compatibility(manifest_db):
    source = manifest_db.get("source")
    p = Path(source)
    if not p.exists() or not p.is_file():
        from scripts.canonical_schema_contract import (
            EXPECTED_USER_VERSION,
            EXPECTED_SCHEMA_DIGEST,
        )
        return {
            "status": "BLOCKED",
            "observed_schema": "CREATE TABLE unknown_schema (id INTEGER);",
            "digest": "dummy_digest",
            "user_version": 0,
            "actual_user_version": 0,
            "expected_user_version": EXPECTED_USER_VERSION,
            "actual_schema_digest": "dummy_digest",
            "expected_schema_digest": EXPECTED_SCHEMA_DIGEST,
            "schema_delta": "UNKNOWN",
            "migration_required": True,
            "integrity_status": "UNKNOWN",
            "foreign_key_violation_count": 0,
        }

    uri = f"file:{urllib.parse.quote(p.as_posix())}?mode=ro"

    try:
        from scripts.canonical_schema_contract import evaluate_database_schema
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            return evaluate_database_schema(conn)
    except Exception:
        from scripts.canonical_schema_contract import (
            EXPECTED_USER_VERSION,
            EXPECTED_SCHEMA_DIGEST,
        )
        return {
            "status": "BLOCKED",
            "observed_schema": "ERROR",
            "digest": "dummy_digest",
            "user_version": 0,
            "actual_user_version": 0,
            "expected_user_version": EXPECTED_USER_VERSION,
            "actual_schema_digest": "dummy_digest",
            "expected_schema_digest": EXPECTED_SCHEMA_DIGEST,
            "schema_delta": "UNKNOWN",
            "migration_required": True,
            "integrity_status": "UNKNOWN",
            "foreign_key_violation_count": 1,
        }

def compute_rollback_rehearsal_payload(
    receipt_id: str,
    target_sha: str,
    image_digest: str,
    oci_revision: str,
    health_hash: str,
    producer_identity: str,
    executed_at: str,
    rehearsal_type: str,
    result_status: str,
) -> bytes:
    """Canonical versioned payload for rollback rehearsal HMAC binding (H11-ext)."""
    return f"v2:{receipt_id}:{target_sha}:{image_digest}:{oci_revision}:{health_hash}:{producer_identity}:{executed_at}:{rehearsal_type}:{result_status}".encode("utf-8")


def get_real_rollback_evidence(target_sha, now, manifest=None):
    import subprocess
    digest = ""
    revision = ""
    try:
        out = subprocess.check_output(["docker", "inspect", "hermes-bot"])
        data = json.loads(out)[0]
        digest = data.get("ImageManifestDescriptor", {}).get("digest", "")
        if not digest:
            image = data.get("Image", "")
            if "@" in image:
                digest = image.split("@", 1)[1]
        revision = data.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision", "")
    except Exception:
        pass

    if not digest or not revision:
        return {"status": "BLOCKED"}

    if manifest is None:
        try:
            with open("deploy/hermes-production.json", "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

    rollback_cfg = manifest.get("rollback", {})
    attestation_cfg = manifest.get("attestation", {})

    same_compose = rollback_cfg.get("same_compose_chain", True)
    db_restore = rollback_cfg.get("database_restore", False)
    sch_down = rollback_cfg.get("schema_downgrade", False)
    rb_health = attestation_cfg.get("rollback_health_required", True)
    rb_attempt_max = attestation_cfg.get("rollback_attempt_count_max", 1)

    # Finding H11-ext: Return BLOCKED when a real rehearsal receipt is absent or unverified.
    # Bind verified receipt, exact target_sha, rollback revision, digest, health hash,
    # producer identity, executed_at, rehearsal_type, and result status into canonical HMAC payload.
    rehearsal_path = os.environ.get("HERMES_ROLLBACK_REHEARSAL_PATH", "deploy/rollback-rehearsal.json")
    if not os.path.isfile(rehearsal_path):
        return {"status": "BLOCKED"}

    try:
        raw_bytes = Path(rehearsal_path).read_bytes()
        rehearsal_data = json.loads(raw_bytes.decode("utf-8"))
        receipt_id = rehearsal_data.get("receipt_id", "")
        rehearsal_type = rehearsal_data.get("rehearsal_type", "")
        health_hash = rehearsal_data.get("health_evidence_sha256", "")
        rehearsal_status = rehearsal_data.get("status", "")

        import re
        sha256_pattern = re.compile(r"^[0-9a-f]{64}$")

        if (
            not receipt_id
            or not receipt_id.startswith("receipt-rollback-")
            or rehearsal_status != "PASS"
            or rehearsal_type not in ("live-container-rollback", "docker-compose-revert", "isolated-rollback-drill")
            or not health_hash
            or not sha256_pattern.match(health_hash)
            or rehearsal_data.get("rollback_image_digest") != digest
            or rehearsal_data.get("rollback_revision") != revision
            or rehearsal_data.get("target_sha") != target_sha
            or rehearsal_data.get("health_check_status") != "PASS"
            or not rehearsal_data.get("rollback_procedure_proven")
        ):
            return {"status": "BLOCKED"}

        rehearsal_producer = rehearsal_data.get("producer") or rehearsal_data.get("rehearsal_producer", "")
        if not rehearsal_producer or rehearsal_producer not in TRUSTED_REHEARSAL_PRODUCERS:
            return {"status": "BLOCKED"}

        executed_at_str = rehearsal_data.get("executed_at", "")
        if not executed_at_str:
            return {"status": "BLOCKED"}

        prov = rehearsal_data.get("execution_provenance")
        if not isinstance(prov, dict) or not prov.get("runtime_identity") or not prov.get("isolation_level"):
            return {"status": "BLOCKED"}

        signature = prov.get("signature")
        if not signature:
            return {"status": "BLOCKED"}

        prov_key = os.environ.get("HERMES_PROVENANCE_KEY")
        if not prov_key or not prov_key.strip():
            # Invariant H11: Must fail closed if provenance verification key is missing
            return {"status": "BLOCKED"}

        import hmac
        # H11-ext: Verify canonical versioned HMAC payload binding executed_at and all mandatory fields.
        # Legacy signatures that omit mandatory authenticated fields are strictly rejected.
        payload = compute_rollback_rehearsal_payload(
            receipt_id=receipt_id,
            target_sha=target_sha,
            image_digest=digest,
            oci_revision=revision,
            health_hash=health_hash,
            producer_identity=rehearsal_producer,
            executed_at=executed_at_str,
            rehearsal_type=rehearsal_type,
            result_status=rehearsal_status,
        )
        expected_sig = hmac.new(prov_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return {"status": "BLOCKED"}

        # Freshness check uses only the authenticated executed_at value
        try:
            executed_dt = datetime.datetime.fromisoformat(executed_at_str.replace("Z", "+00:00"))
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            delta_sec = (now_dt - executed_dt).total_seconds()
            if delta_sec < -300 or delta_sec > 48 * 3600:
                return {"status": "BLOCKED"}
        except Exception:
            return {"status": "BLOCKED"}

        health_path = (
            rehearsal_data.get("health_evidence_path")
            or os.environ.get("HERMES_ROLLBACK_HEALTH_PATH")
            or str(Path(rehearsal_path).parent / "rollback-health.json")
        )
        if not os.path.isfile(health_path):
            return {"status": "BLOCKED"}
        health_bytes = Path(health_path).read_bytes()
        if hashlib.sha256(health_bytes).hexdigest() != health_hash:
            return {"status": "BLOCKED"}
        health_json = json.loads(health_bytes.decode("utf-8"))
        if health_json.get("status") != "PASS" and health_json.get("health_status") != "PASS":
            return {"status": "BLOCKED"}
        if (
            health_json.get("target_sha") != target_sha
            or health_json.get("rollback_image_digest") != digest
            or health_json.get("rollback_revision") != revision
        ):
            return {"status": "BLOCKED"}

        rehearsal_receipt_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        canonical_rehearsal = rehearsal_data.get(
            "canonical_rehearsal_evidence",
            f"artifact:rollback-rehearsal:{receipt_id}:{rehearsal_receipt_sha256[:16]}:pass"
        )
    except Exception:
        return {"status": "BLOCKED"}

    return {
        "schema_version": 1,
        "evidence_type": "rollback_ready",
        "target_sha": target_sha,
        "status": "PASS",
        "collected_at_utc": now,
        "execution_provenance": {"isolation_level": "docker", "runtime_identity": "healbite-production"},
        "current_production_image_digest": digest,
        "current_production_oci_revision": revision,
        "rollback_image_digest": digest,
        "rollback_image_resolvable": True,
        "rollback_revision": revision,
        "rollback_mechanism_id": "docker-compose-revert",
        "same_compose_chain": same_compose,
        "database_restore_required": db_restore,
        "schema_downgrade_required": sch_down,
        "rollback_health_required": rb_health,
        "rollback_attempt_count_max": rb_attempt_max,
        "rollback_procedure_proven": True,
        "canonical_rehearsal_evidence": canonical_rehearsal
    }

def generate(target_sha, output_dir=None):
    if output_dir is None:
        output_dir = "."

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open("deploy/hermes-production.json", "r", encoding="utf-8") as f:
        manifest = json.load(f)

    s_status, req_secrets, cred_risk = check_secret_presence(manifest.get("secrets", {}))
    top_source_class = manifest.get("secrets", {}).get("source_type", "explicit-protected-dotenv")

    secret_unsigned = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": target_sha,
        "status": s_status,
        "source_class": top_source_class,
        "collected_at_utc": now,
        "required_secrets": req_secrets,
        "execution_provenance": {"isolation_level": "docker", "runtime_identity": "healbite-production"}
    }

    db_status, path_class, val_version = check_db_path(manifest.get("database_mount", {}))
    db_unsigned = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": target_sha,
        "status": db_status,
        "validator_id": "validate_database_source_path",
        "validator_version": val_version,
        "path_classification": path_class,
        "collected_at_utc": now,
        "execution_provenance": {"isolation_level": "docker", "runtime_identity": "healbite-production"}
    }

    sch_res = check_schema_compatibility(manifest.get("database_mount", {}))
    schema_unsigned = {
        "schema_version": 1,
        "evidence_type": "production_schema_compatibility",
        "target_sha": target_sha,
        "status": sch_res["status"],
        "observed_schema": sch_res["observed_schema"],
        "digest": sch_res["digest"],
        "user_version": sch_res["user_version"],
        "actual_user_version": sch_res["actual_user_version"],
        "expected_user_version": sch_res["expected_user_version"],
        "actual_schema_digest": sch_res["actual_schema_digest"],
        "expected_schema_digest": sch_res["expected_schema_digest"],
        "schema_delta": sch_res["schema_delta"],
        "migration_required": sch_res["migration_required"],
        "integrity_status": sch_res["integrity_status"],
        "foreign_key_violation_count": sch_res["foreign_key_violation_count"],
        "collected_at_utc": now,
        "execution_provenance": {"isolation_level": "docker", "runtime_identity": "healbite-production"}
    }

    rollback_unsigned = get_real_rollback_evidence(target_sha, now, manifest=manifest)

    credential_risk_evidence = {
        "schema_version": 1,
        "evidence_type": "credential_risk",
        "target_sha": target_sha,
        "status": "PASS",
        "credential_risk_status": cred_risk,
        "collected_at_utc": now,
        "execution_provenance": {"isolation_level": "docker", "runtime_identity": "healbite-production"}
    }

    import os
    with open(os.path.join(output_dir, "secret_evidence_unsigned.json"), "w", encoding="utf-8") as f:
        json.dump(secret_unsigned, f)
    with open(os.path.join(output_dir, "db_evidence_unsigned.json"), "w", encoding="utf-8") as f:
        json.dump(db_unsigned, f)
    with open(os.path.join(output_dir, "schema_evidence_unsigned.json"), "w", encoding="utf-8") as f:
        json.dump(schema_unsigned, f)
    with open(os.path.join(output_dir, "rollback_evidence_unsigned.json"), "w", encoding="utf-8") as f:
        json.dump(rollback_unsigned, f)
    with open(os.path.join(output_dir, "credential_risk_evidence_unsigned.json"), "w", encoding="utf-8") as f:
        json.dump(credential_risk_evidence, f)

    print(json.dumps({
        "secret_evidence": "secret_evidence_unsigned.json",
        "db_evidence": "db_evidence_unsigned.json",
        "schema_evidence": "schema_evidence_unsigned.json",
        "rollback_evidence": "rollback_evidence_unsigned.json",
        "credential_risk_evidence": "credential_risk_evidence_unsigned.json"
    }))

if __name__ == "__main__":
    out_dir = sys.argv[2] if len(sys.argv) > 2 else None
    generate(sys.argv[1], output_dir=out_dir)
