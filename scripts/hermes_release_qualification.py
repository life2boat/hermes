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

def get_all_gates(expected_sha: str, bundle: dict) -> Tuple[List[GateResult], str, str, str, str, str, str, str, str, str, str, str]:
    # 1. BUNDLE_DIGEST validation
    if bundle:
        provided_digest = bundle.get("bundle_digest")
        bundle_copy = dict(bundle)
        if "bundle_digest" in bundle_copy:
            del bundle_copy["bundle_digest"]
        serialized = json.dumps(bundle_copy, separators=(',', ':'), sort_keys=True)
        computed_digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        if provided_digest != computed_digest:
            return [GateResult("SOURCE_ATTESTATION", Status.FAIL, "Mutated bundle")], "", "", "", "", "", "", "", "", "", "", ""

    # 1. SOURCE_ATTESTATION
    if bundle and bundle.get("target_sha") == expected_sha:
        g1 = GateResult("SOURCE_ATTESTATION", Status.PASS, evidence={"sha": expected_sha})
    else:
        g1 = GateResult("SOURCE_ATTESTATION", Status.FAIL, "target_sha does not match expected_sha or no bundle")
            
    # 2. BUILD_CONTEXT
    g2 = GateResult("BUILD_CONTEXT", Status.PASS) if bundle else GateResult("BUILD_CONTEXT", Status.BLOCKED, "Local env")

    # 3. EXACT_MAIN_CI
    ci_digest = ""
    if bundle:
        ci_evidence = bundle.get("structured_ci_evidence", [])
        required_workflows = [
            "Tests", 
            "Lint (ruff + ty diff)", 
            "Typecheck", 
            "Nix", 
            "Agent Release Gate", 
            "Supply Chain Audit", 
            "History Check"
        ]
        found = {wf: False for wf in required_workflows}
        ci_fail = False
        for run in ci_evidence:
            name = run.get("canonical_name", "")
            if name in found:
                if run.get("head_sha") != expected_sha:
                    ci_fail = True
                    break
                if run.get("status") == "completed" and run.get("conclusion") == "success":
                    found[name] = True
        
        if ci_fail or not all(found.values()):
            g3 = GateResult("EXACT_MAIN_CI", Status.FAIL, "Missing required CI result or foreign SHA")
        else:
            g3 = GateResult("EXACT_MAIN_CI", Status.PASS)
            ci_digest = hashlib.sha256(json.dumps(ci_evidence, sort_keys=True).encode('utf-8')).hexdigest()
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
            g4 = GateResult("EXACT_MAIN_BUILD", Status.FAIL, "Build source SHA mismatch")
        else:
            g4 = GateResult("EXACT_MAIN_BUILD", Status.PASS)
    else:
        g4 = GateResult("EXACT_MAIN_BUILD", Status.BLOCKED, "Docker build error")

    # 5. IMAGE_ATTESTATION
    g5 = GateResult("IMAGE_ATTESTATION", Status.PASS if g4.status == Status.PASS else Status.FAIL, evidence={"revision": revision})
    
    # 6. CONFIG_CONTRACT
    cfg_digest = ""
    if bundle and bundle.get("config_evidence") != "BLOCKED":
        cfg = bundle.get("config_evidence", {})
        g6 = GateResult("CONFIG_CONTRACT", Status.PASS, evidence={"keys": cfg.get("required_keys")})
        cfg_digest = cfg.get("digest", "")
    else:
        g6 = GateResult("CONFIG_CONTRACT", Status.BLOCKED, "Missing config evidence")
        
    # 7. SECRET_CONTRACT
    g7 = GateResult("SECRET_CONTRACT", Status.BLOCKED, "Production secrets unavailable under read-only authority")
        
    # 8. DB_PATH_SAFETY
    g8 = GateResult("DB_PATH_SAFETY", Status.BLOCKED, "Production DB path proof is unavailable under read-only authority")
        
    # 9. SCHEMA_COMPATIBILITY
    sch_digest = ""
    if bundle and bundle.get("schema_evidence") != "BLOCKED":
        sch = bundle.get("schema_evidence", {})
        if "observed_schema" not in sch:
            g9 = GateResult("SCHEMA_COMPATIBILITY", Status.BLOCKED, "Production schema cannot be read under current authority")
        else:
            g9 = GateResult("SCHEMA_COMPATIBILITY", Status.PASS)
            sch_digest = sch.get("digest", "")
    else:
        g9 = GateResult("SCHEMA_COMPATIBILITY", Status.BLOCKED, "Production schema cannot be read under current authority")
        
    # 10. ROLLBACK_QUALIFIED
    rb_digest = ""
    if bundle and bundle.get("rollback_evidence") != "BLOCKED":
        g10 = GateResult("ROLLBACK_QUALIFIED", Status.PASS)
        rb_digest = bundle.get("rollback_evidence_digest", "")
    else:
        g10 = GateResult("ROLLBACK_QUALIFIED", Status.BLOCKED, "Missing rollback rehearsal")
        
    # 11. RUNTIME_PREFLIGHT
    if bundle and bundle.get("runtime_preflight_evidence"):
        preflight = bundle.get("runtime_preflight_evidence", {})
        if preflight.get("cmd", "") == "--help" or preflight.get("cli_help_only") == True:
            g11 = GateResult("RUNTIME_PREFLIGHT", Status.FAIL, "CLI-help-only runtime evidence")
        elif preflight.get("status") == "PASS":
            g11 = GateResult("RUNTIME_PREFLIGHT", Status.PASS)
        else:
            g11 = GateResult("RUNTIME_PREFLIGHT", Status.FAIL, "Runtime preflight failed")
    else:
        g11 = GateResult("RUNTIME_PREFLIGHT", Status.BLOCKED, "Missing runtime preflight evidence")
        
    # 12. SECURITY_ISOLATION
    if bundle and bundle.get("security_evidence") != "BLOCKED":
        sec = bundle.get("security_evidence", {})
        if sec.get("status") == "PASS":
            g12 = GateResult("SECURITY_ISOLATION", Status.PASS)
        else:
            g12 = GateResult("SECURITY_ISOLATION", Status.FAIL, "Security isolation failed")
    else:
        g12 = GateResult("SECURITY_ISOLATION", Status.BLOCKED, "Missing security isolation evidence")
        
    tree_sha = bundle.get("git_tree_sha", "") if bundle else ""
    b_wf_id = bundle.get("build_workflow_id", "local") if bundle else "local"
    b_run_id = bundle.get("build_run_id", "local") if bundle else "local"

    return [g1, g2, g3, g4, g5, g6, g7, g8, g9, g10, g11, g12], digest, revision, arch, entrypoint, tree_sha, cfg_digest, sch_digest, rb_digest, ci_digest, b_wf_id, b_run_id

def qualify_main():
    expected_sha = sys.argv[1] if len(sys.argv) > 1 else "6508e294d234598cbb15c1de214802fc3963e2bd"
    
    bundle = {}
    if os.path.exists("task8-qualification-evidence.json"):
        with open("task8-qualification-evidence.json", "r", encoding="utf-8") as f:
            bundle = json.load(f)

    gates, image_digest, oci_revision, arch, entrypoint, tree_sha, cfg_digest, sch_digest, rb_digest, ci_digest, b_wf_id, b_run_id = get_all_gates(expected_sha, bundle)
    if len(gates) == 1 and gates[0].status == Status.FAIL:
        # Invalid bundle
        print(json.dumps({
            "status": "FAIL",
            "digest": "",
            "image": "",
            "blockers": ["INVALID_BUNDLE_DIGEST"]
        }, indent=2))
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
        entrypoint=entrypoint
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
            "History Check"
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
        timestamp=timestamp
    )
    
    print(json.dumps({
        "status": receipt.result.value,
        "digest": receipt.report.report_digest,
        "image": receipt.image_digest,
        "blockers": receipt.report.technical_blockers
    }, indent=2))

if __name__ == "__main__":
    qualify_main()
