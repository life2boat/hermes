import os
import subprocess
import json
import hashlib
import sys
from typing import List, Tuple
from ai_engineering.contracts import Status
from ai_engineering.supervisor.production_readiness import (
    GateResult,
    ReleaseCandidateManifest,
    ReleaseQualificationRequest,
    ReleaseQualifier,
)
from datetime import datetime, timezone


def run_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.returncode, res.stdout, res.stderr
    except FileNotFoundError as e:
        return 1, "", str(e)


def compute_file_sha256(filepath: str) -> str:
    if not os.path.exists(filepath):
        return ""
    with open(filepath, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


from scripts.compute_bundle_digest import compute_canonical_digest_from_dict



MAX_EVIDENCE_AGE_SECONDS = 7200
MAX_FUTURE_CLOCK_SKEW_SECONDS = 300

def _verify_signed_provenance(ev: dict, expected_sha: str, expected_type: str, expected_fields: list, key: str, current_time_utc=None) -> str:
    import re
    import datetime
    from scripts.compute_bundle_digest import compute_canonical_digest_from_dict

    missing = [k for k in expected_fields if k not in ev]
    if missing:
        return f"Missing fields: {missing}"
    if set(ev.keys()) != set(expected_fields):
        return "Unknown fields present in evidence"

    if expected_type and ev.get("evidence_type") != expected_type:
        return "Invalid evidence_type"
    if expected_sha and ev.get("target_sha") != expected_sha:
        return "target_sha mismatch"
    if ev.get("status") != "PASS":
        return "status not PASS"

    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", str(ev.get("collected_at_utc", ""))):
        return "Invalid collected_at_utc timestamp"

    try:
        dt = datetime.datetime.strptime(str(ev["collected_at_utc"]), "%Y-%m-%dT%H:%M:%SZ")
        if current_time_utc is None:
            current_time_utc = datetime.datetime.utcnow()
        delta = (current_time_utc - dt).total_seconds()
        if delta < -MAX_FUTURE_CLOCK_SKEW_SECONDS:
            return "collected_at_utc is too far in the future"
        if delta > MAX_EVIDENCE_AGE_SECONDS:
            return f"collected_at_utc is stale (older than {MAX_EVIDENCE_AGE_SECONDS}s)"
    except ValueError:
        return "Invalid collected_at_utc timestamp format"

    prov = ev.get("execution_provenance")
    if not isinstance(prov, dict):
        return "execution_provenance must be a dictionary"
    sig = prov.get("signature")
    if not isinstance(sig, str):
        return "Missing or invalid signature"

    # Cryptographic check
    payload = {
        k: v for k, v in ev.items() if k not in ("evidence_digest", "execution_provenance")
    }
    prov_copy = {k: v for k, v in prov.items() if k != "signature"}
    payload["execution_provenance"] = prov_copy
    
    import json, hashlib, hmac
    payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_digest = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
    expected_sig = hmac.new(
        key.encode("utf-8"),
        payload_digest.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        return "Cryptographic provenance verification failed"

    ev_copy = dict(ev)
    provided_digest = ev_copy.pop("evidence_digest")
    computed_digest = compute_canonical_digest_from_dict(ev_copy)
    if provided_digest != computed_digest:
        return "evidence_digest mismatch"

    return None

def get_all_gates(

    expected_sha: str, bundle: dict
) -> Tuple[List[GateResult], str, str, str, str, str, str, str, str, str, str, str]:
    key = __import__('os').environ.get('HERMES_PROVENANCE_KEY', '')
    # 1. BUNDLE_DIGEST validation
    if bundle:
        provided_digest = bundle.get("bundle_digest")
        computed_digest = compute_canonical_digest_from_dict(bundle)
        if provided_digest != computed_digest:
            return (
                [GateResult("SOURCE_ATTESTATION", Status.FAIL, "Mutated bundle")],
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            )

    # 1. SOURCE_ATTESTATION
    if bundle and bundle.get("target_sha") == expected_sha:
        g1 = GateResult(
            "SOURCE_ATTESTATION", Status.PASS, evidence={"sha": expected_sha}
        )
    else:
        g1 = GateResult(
            "SOURCE_ATTESTATION",
            Status.FAIL,
            "target_sha does not match expected_sha or no bundle",
        )

    # 2. BUILD_CONTEXT
    g2 = (
        GateResult("BUILD_CONTEXT", Status.PASS)
        if bundle
        else GateResult("BUILD_CONTEXT", Status.BLOCKED, "Local env")
    )

    # 3. EXACT_MAIN_CI
    ci_digest = ""
    if bundle and bundle.get("structured_ci_evidence"):
        ci_evidence = bundle.get("structured_ci_evidence", {})
        if isinstance(ci_evidence, dict):
            required_workflows = [
                "Tests",
                "Lint (ruff + ty)",
                "Typecheck",
                "Nix",
                "Agent Release Gate",
                "Supply Chain Audit",
                "History Check",
            ]
            pr_merge_sha = ci_evidence.get("pr_merge_sha", "")
            pr_head_tree_sha = ci_evidence.get("pr_head_tree_sha", "")
            final_main_tree_sha = ci_evidence.get("final_main_tree_sha", "")
            pr_head_sha = ci_evidence.get("pr_head_sha", "")
            pr_merged_at = ci_evidence.get("pr_merged_at", "")

            if pr_merge_sha != expected_sha:
                g3 = GateResult(
                    "EXACT_MAIN_CI",
                    Status.FAIL,
                    f"PR merge SHA {pr_merge_sha} != {expected_sha}",
                )
            elif pr_head_tree_sha != final_main_tree_sha:
                g3 = GateResult(
                    "EXACT_MAIN_CI",
                    Status.FAIL,
                    "PR head tree SHA != final main tree SHA",
                )
            else:
                found = {wf: False for wf in required_workflows}
                ci_fail = False
                for run in ci_evidence.get("workflow_runs", []):
                    name = run.get("workflow_name", "")
                    if name in found:
                        if run.get("head_sha") != pr_head_sha:
                            ci_fail = True
                            break

                        if (
                            run.get("status") == "completed"
                            and run.get("conclusion") == "success"
                        ):
                            completed_at = run.get("completed_at", "")
                            if (
                                completed_at
                                and pr_merged_at
                                and completed_at <= pr_merged_at
                            ):
                                found[name] = True

                if ci_fail or not all(found.values()):
                    g3 = GateResult(
                        "EXACT_MAIN_CI",
                        Status.FAIL,
                        "Missing required CI result or failed or late completion",
                    )
                else:
                    g3 = GateResult("EXACT_MAIN_CI", Status.PASS)
                    ci_digest = hashlib.sha256(
                        json.dumps(ci_evidence, sort_keys=True).encode("utf-8")
                    ).hexdigest()
        else:
            g3 = GateResult(
                "EXACT_MAIN_CI", Status.FAIL, "structured_ci_evidence must be dict"
            )
    else:
        g3 = GateResult("EXACT_MAIN_CI", Status.BLOCKED, "Missing evidence")

    # 4. EXACT_MAIN_BUILD
    digest = revision = arch = entrypoint = ""
    if bundle and bundle.get("image_digest"):
        digest = bundle.get("image_digest", "")
        revision = bundle.get("oci_revision", "")
        arch = bundle.get("architecture", "")
        entrypoint = bundle.get("entrypoint", "")
        build_source_sha = bundle.get("build_source_sha", "")
        if isinstance(entrypoint, list):
            entrypoint = json.dumps(entrypoint)

        if not digest.startswith("sha256:") or len(digest) != 71 or "0000000" in digest:
            g4 = GateResult("EXACT_MAIN_BUILD", Status.FAIL, "Invalid image digest")
        elif revision != expected_sha:
            g4 = GateResult("EXACT_MAIN_BUILD", Status.FAIL, "OCI revision mismatch")
        elif build_source_sha != expected_sha:
            g4 = GateResult(
                "EXACT_MAIN_BUILD", Status.FAIL, "Build source SHA mismatch"
            )
        else:
            g4 = GateResult("EXACT_MAIN_BUILD", Status.PASS)
    else:
        g4 = GateResult("EXACT_MAIN_BUILD", Status.BLOCKED, "Docker build error")

    # 5. IMAGE_ATTESTATION
    g5 = GateResult(
        "IMAGE_ATTESTATION",
        Status.PASS if g4.status == Status.PASS else Status.FAIL,
        evidence={"revision": revision},
    )

    # 6. CONFIG_CONTRACT
    cfg_digest = ""
    if bundle and bundle.get("config_evidence") != "BLOCKED":
        cfg = bundle.get("config_evidence", {})
        g6 = GateResult(
            "CONFIG_CONTRACT", Status.PASS, evidence={"keys": cfg.get("required_keys")}
        )
        cfg_digest = cfg.get("digest", "")
    else:
        g6 = GateResult("CONFIG_CONTRACT", Status.BLOCKED, "Missing config evidence")

    # 7. SECRET_CONTRACT
    expected_secret_set = None
    try:
        with open("deploy/hermes-production.json", "r", encoding="utf-8") as f:
            manifest = __import__("json").load(f)
            secrets = manifest.get("secrets", {}).get("protected_variables", [])
            expected_secret_set = {
                s["name"] for s in secrets if s.get("required") is True
            }
    except Exception:
        pass

    if expected_secret_set is None:
        g7 = GateResult("SECRET_CONTRACT", Status.BLOCKED, "Cannot derive expected secret set")
    elif not bundle or "secret_evidence" not in bundle:
        g7 = GateResult("SECRET_CONTRACT", Status.BLOCKED, "Missing secret evidence")
    else:
        ev = bundle.get("secret_evidence")
        if ev == "BLOCKED":
            g7 = GateResult("SECRET_CONTRACT", Status.BLOCKED, "Secret evidence explicitly blocked")
        elif isinstance(ev, dict) and ev.get("status") == "BLOCKED":
            g7 = GateResult("SECRET_CONTRACT", Status.BLOCKED, "Secret evidence status blocked")
        elif isinstance(ev, dict) and ev.get("status") == "FAIL":
            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "Secret evidence explicitly failed")
        elif not isinstance(ev, dict):
            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "secret_evidence must be a dictionary")
        else:
            required = [
                "schema_version",
                "evidence_type",
                "target_sha",
                "status",
                "source_class",
                "collected_at_utc",
                "required_secrets",
                "evidence_digest",
                "execution_provenance",
            ]
            
            if not key:
                g7 = GateResult("SECRET_CONTRACT", Status.BLOCKED, "Cannot verify execution provenance: No approved trust anchor exists")
            else:
                required = ["schema_version", "evidence_type", "target_sha", "status", "source_class", "collected_at_utc", "required_secrets", "evidence_digest", "execution_provenance"]
                err = _verify_signed_provenance(ev, expected_sha, "production_secret_presence", required, key)
                if err:
                    g7 = GateResult("SECRET_CONTRACT", Status.FAIL, err)
                elif not isinstance(ev.get("required_secrets"), list) or not ev["required_secrets"]:
                    g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "required_secrets must be a non-empty list")
                else:
                    secret_fail = False
                    observed_secret_set = set()
                    allowed_keys = {"name", "required", "present", "source_class"}
                    for sec in ev["required_secrets"]:
                        if not isinstance(sec, dict):
                            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "Each secret must be a dict")
                            secret_fail = True
                            break
                        if set(sec.keys()) != allowed_keys:
                            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "Secret record has missing or unknown fields")
                            secret_fail = True
                            break
                        if not isinstance(sec["name"], str) or not sec["name"]:
                            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "name must be a non-empty string")
                            secret_fail = True
                            break
                        if not isinstance(sec["required"], bool) or not isinstance(sec["present"], bool):
                            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "required and present must be bools")
                            secret_fail = True
                            break
                        if not isinstance(sec["source_class"], str) or not sec["source_class"]:
                            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "source_class must be a non-empty string")
                            secret_fail = True
                            break
                        if sec["required"] and not sec["present"]:
                            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "Required secret is not present")
                            secret_fail = True
                            break
                        observed_secret_set.add(sec["name"])

                    if not secret_fail:
                        if expected_secret_set != observed_secret_set:
                            g7 = GateResult("SECRET_CONTRACT", Status.FAIL, "Observed secrets do not match expected mandatory secrets exactly")
                        else:
                            g7 = GateResult("SECRET_CONTRACT", Status.PASS)

    # 8. DB_PATH_SAFETY
    if not bundle or "db_evidence" not in bundle:
        g8 = GateResult("DB_PATH_SAFETY", Status.BLOCKED, "Missing DB evidence")
    else:
        ev = bundle.get("db_evidence")
        if ev == "BLOCKED":
            g8 = GateResult("DB_PATH_SAFETY", Status.BLOCKED, "DB evidence explicitly blocked")
        elif isinstance(ev, dict) and ev.get("status") == "BLOCKED":
            g8 = GateResult("DB_PATH_SAFETY", Status.BLOCKED, "DB evidence status blocked")
        elif isinstance(ev, dict) and ev.get("status") == "FAIL":
            g8 = GateResult("DB_PATH_SAFETY", Status.FAIL, "DB evidence explicitly failed")
        elif not isinstance(ev, dict):
            g8 = GateResult("DB_PATH_SAFETY", Status.FAIL, "db_evidence must be a dictionary")
        else:
            required = [
                "schema_version",
                "evidence_type",
                "target_sha",
                "status",
                "validator_id",
                "validator_version",
                "path_classification",
                "collected_at_utc",
                "evidence_digest",
                "execution_provenance",
            ]
            
            if not key:
                g8 = GateResult("DB_PATH_SAFETY", Status.BLOCKED, "Cannot verify execution provenance: No approved trust anchor exists")
            else:
                err = _verify_signed_provenance(ev, expected_sha, "production_db_path_safety", required, key)
                if err:
                    g8 = GateResult("DB_PATH_SAFETY", Status.FAIL, err)
                elif ev.get("validator_id") != "_validate_database_source_path":
                    g8 = GateResult("DB_PATH_SAFETY", Status.FAIL, "validator_id is not canonical")
                elif str(ev.get("validator_version")) != "1":
                    g8 = GateResult("DB_PATH_SAFETY", Status.FAIL, "foreign validator revision")
                elif ev.get("path_classification") not in ("authoritative-production-path", "approved-production-path", "canonical-production-path"):
                    g8 = GateResult("DB_PATH_SAFETY", Status.FAIL, "path_classification is not authoritative")
                else:
                    g8 = GateResult("DB_PATH_SAFETY", Status.PASS)

    # 9. SCHEMA_COMPATIBILITY
    sch_digest = ""
    if bundle and bundle.get("schema_evidence") != "BLOCKED":
        sch = bundle.get("schema_evidence", {})
        if "observed_schema" not in sch:
            g9 = GateResult(
                "SCHEMA_COMPATIBILITY",
                Status.BLOCKED,
                "Production schema cannot be read under current authority",
            )
        else:
            sch_digest = sch.get("digest", "")
            
            expected_fields = [
                "schema_version",
                "evidence_type",
                "target_sha",
                "status",
                "observed_schema",
                "digest",
                "collected_at_utc",
                "evidence_digest",
                "execution_provenance",
            ]
            if not key:
                g9 = GateResult("SCHEMA_COMPATIBILITY", Status.BLOCKED, "Cannot verify execution provenance: No approved trust anchor exists")
            else:
                err = _verify_signed_provenance(sch, expected_sha, "production_schema_compatibility", expected_fields, key)
                if err:
                    g9 = GateResult("SCHEMA_COMPATIBILITY", Status.FAIL, err)
                else:
                    g9 = GateResult("SCHEMA_COMPATIBILITY", Status.PASS)
    else:
        g9 = GateResult(
            "SCHEMA_COMPATIBILITY",
            Status.BLOCKED,
            "Production schema cannot be read under current authority",
        )

    # 10. ROLLBACK_QUALIFIED
    rb_digest = ""
    if bundle and bundle.get("rollback_evidence") != "BLOCKED":
        rb_digest = bundle.get("rollback_evidence_digest", "")
        
        rb = bundle.get("rollback_evidence")
        if not isinstance(rb, dict):
            g10 = GateResult("ROLLBACK_QUALIFIED", Status.FAIL, "rollback_evidence must be a dictionary")
        else:
            expected_fields = [
                "schema_version",
                "evidence_type",
                "target_sha",
                "status",
                "collected_at_utc",
                "evidence_digest",
                "execution_provenance",
            ]
            if not key:
                g10 = GateResult("ROLLBACK_QUALIFIED", Status.BLOCKED, "Cannot verify execution provenance: No approved trust anchor exists")
            else:
                err = _verify_signed_provenance(rb, expected_sha, "rollback_ready", expected_fields, key)
                if err:
                    g10 = GateResult("ROLLBACK_QUALIFIED", Status.FAIL, err)
                else:
                    g10 = GateResult("ROLLBACK_QUALIFIED", Status.PASS)
    else:
        g10 = GateResult(
            "ROLLBACK_QUALIFIED", Status.BLOCKED, "Missing rollback rehearsal"
        )

    # 11. RUNTIME_PREFLIGHT
    if bundle and bundle.get("runtime_preflight_evidence"):
        preflight = bundle.get("runtime_preflight_evidence", {})
        if (
            preflight.get("cmd", "") == "--help"
            or preflight.get("cli_help_only") == True
        ):
            g11 = GateResult(
                "RUNTIME_PREFLIGHT", Status.FAIL, "CLI-help-only runtime evidence"
            )
        elif preflight.get("status") == "PASS":
            g11 = GateResult("RUNTIME_PREFLIGHT", Status.PASS)
        else:
            g11 = GateResult(
                "RUNTIME_PREFLIGHT", Status.FAIL, "Runtime preflight failed"
            )
    else:
        g11 = GateResult(
            "RUNTIME_PREFLIGHT", Status.BLOCKED, "Missing runtime preflight evidence"
        )

    # 12. SECURITY_ISOLATION
    if bundle and bundle.get("security_evidence") != "BLOCKED":
        sec = bundle.get("security_evidence", {})
        if sec.get("status") == "PASS":
            g12 = GateResult("SECURITY_ISOLATION", Status.PASS)
        else:
            g12 = GateResult(
                "SECURITY_ISOLATION", Status.FAIL, "Security isolation failed"
            )
    else:
        g12 = GateResult(
            "SECURITY_ISOLATION", Status.BLOCKED, "Missing security isolation evidence"
        )

    tree_sha = bundle.get("git_tree_sha", "") if bundle else ""
    b_wf_id = bundle.get("build_workflow_id", "local") if bundle else "local"
    b_run_id = bundle.get("build_run_id", "local") if bundle else "local"

    return (
        [g1, g2, g3, g4, g5, g6, g7, g8, g9, g10, g11, g12],
        digest,
        revision,
        arch,
        entrypoint,
        tree_sha,
        cfg_digest,
        sch_digest,
        rb_digest,
        ci_digest,
        b_wf_id,
        b_run_id,
    )


def qualify_main():
    expected_sha = (
        sys.argv[1] if len(sys.argv) > 1 else "6508e294d234598cbb15c1de214802fc3963e2bd"
    )

    bundle = {}
    if os.path.exists("task8-qualification-evidence.json"):
        with open("task8-qualification-evidence.json", "r", encoding="utf-8") as f:
            bundle = json.load(f)

    (
        gates,
        image_digest,
        oci_revision,
        arch,
        entrypoint,
        tree_sha,
        cfg_digest,
        sch_digest,
        rb_digest,
        ci_digest,
        b_wf_id,
        b_run_id,
    ) = get_all_gates(expected_sha, bundle)
    if len(gates) == 1 and gates[0].status == Status.FAIL:
        # Invalid bundle
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "digest": "",
                    "image": "",
                    "blockers": ["INVALID_BUNDLE_DIGEST"],
                },
                indent=2,
            )
        )
        sys.exit(0)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = ReleaseCandidateManifest.create(
        schema_version=1,
        release_candidate_id=f"rc-{expected_sha[:8]}",
        repository="life2boat/hermes",
        canonical_main_ref="refs/remotes/github/main",
        canonical_main_sha=expected_sha,
        git_tree_sha=tree_sha if tree_sha else "",
        build_source_sha=expected_sha,
        container_image_repository="ghcr.io/life2boat/hermes",
        container_image_digest=image_digest,
        oci_revision=oci_revision,
        build_workflow_id=b_wf_id,
        build_run_id=b_run_id,
        configuration_contract_digest=cfg_digest if cfg_digest else "",
        schema_contract_digest=sch_digest if sch_digest else "",
        rollback_bundle_digest=rb_digest if rb_digest else "",
        required_ci_snapshot_digest=ci_digest if ci_digest else "",
        qualification_timestamp_utc=timestamp,
        architecture=arch,
        entrypoint=entrypoint,
    )

    request = ReleaseQualificationRequest.create(
        schema_version=1,
        qualification_id=f"q-{expected_sha[:8]}",
        repository="life2boat/hermes",
        canonical_remote="github",
        canonical_main_ref="refs/remotes/github/main",
        canonical_main_sha=expected_sha,
        task8_run_id="run-local",
        required_ci_workflows=(
            "Tests",
            "Lint (ruff + ty diff)",
            "Typecheck",
            "Nix",
            "Agent Release Gate",
            "Supply Chain Audit",
            "History Check",
        ),
        production_target_id="prod",
        requested_at_utc=timestamp,
    )

    qualifier = ReleaseQualifier()
    # Always credential_risk=True as per user rule 21
    receipt = qualifier.qualify(
        request=request,
        manifest=manifest,
        gates=gates,
        has_credential_risk=True,
        current_main_sha=expected_sha,
        timestamp=timestamp,
    )

    print(
        json.dumps(
            {
                "status": receipt.result.value,
                "digest": receipt.report.report_digest,
                "image": receipt.image_digest,
                "blockers": receipt.report.technical_blockers,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    qualify_main()
