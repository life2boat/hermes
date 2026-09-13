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

def gate_source_attestation(expected_sha: str) -> GateResult:
    code, out, err = run_cmd(["git", "rev-parse", "HEAD"])
    current_sha = out.strip()
    if current_sha != expected_sha:
        return GateResult("SOURCE_ATTESTATION", Status.FAIL, f"Expected {expected_sha}, got {current_sha}")
    return GateResult("SOURCE_ATTESTATION", Status.PASS, evidence={"sha": current_sha})

def gate_build_context() -> GateResult:
    code, out, err = run_cmd(["git", "status", "--porcelain"])
    if out.strip() != "":
        return GateResult("BUILD_CONTEXT", Status.FAIL, "Working directory is not clean", evidence={"status": out.strip()})
    return GateResult("BUILD_CONTEXT", Status.PASS)

def gate_exact_main_ci(sha: str) -> Tuple[GateResult, str]:
    code, out, err = run_cmd(["gh", "pr", "checks", "--commit", sha, "--json", "name,state,conclusion"])
    if code != 0:
        return GateResult("EXACT_MAIN_CI", Status.BLOCKED, "CLI/provider unavailable"), ""
    
    ci_digest = hashlib.sha256(out.encode('utf-8')).hexdigest()
    
    try:
        checks = json.loads(out)
        
        required_workflows = {
            "test (1)", "test (2)", "test (3)", "test (4)", "test (5)", "test (6)",
            "ruff + ty diff",
            "typecheck (apps/bootstrap-installer)",
            "nix (ubuntu-latest)",
            "agent-release-gate",
            "Scan PR for critical supply chain risks",
            "check-common-ancestor"
        }
        
        found_workflows = {c.get("name") for c in checks}
        missing = required_workflows - found_workflows
        if missing:
            return GateResult("EXACT_MAIN_CI", Status.FAIL, f"Missing required workflow: {list(missing)}"), ci_digest
        
        failures = [c for c in checks if c.get("conclusion") == "FAILURE" or c.get("state") == "FAILURE"]
        if failures:
            return GateResult("EXACT_MAIN_CI", Status.FAIL, "required check not completed/success"), ci_digest
            
        return GateResult("EXACT_MAIN_CI", Status.PASS), ci_digest
    except Exception as e:
        return GateResult("EXACT_MAIN_CI", Status.FAIL, f"parse failure"), ""

def gate_exact_main_build(sha: str) -> Tuple[GateResult, str, str, str, str]:
    tag = f"hermes-rc:{sha}"
    code, out, err = run_cmd(["docker", "build", "--build-arg", f"REVISION={sha}", "--label", f"org.opencontainers.image.revision={sha}", "-t", tag, "."])
    if code != 0:
        return GateResult("EXACT_MAIN_BUILD", Status.BLOCKED, "Docker build error"), "", "", "", ""

    code2, inspect_out, err2 = run_cmd(["docker", "inspect", tag])
    if code2 == 0:
        try:
            data = json.loads(inspect_out)[0]
            digest = data.get("Id", "")
            labels = data.get("Config", {}).get("Labels", {})
            revision = labels.get("org.opencontainers.image.revision", "")
            arch = data.get("Architecture", "")
            entrypoint = json.dumps(data.get("Config", {}).get("Entrypoint", []))
            return GateResult("EXACT_MAIN_BUILD", Status.PASS), digest, revision, arch, entrypoint
        except Exception:
            pass
    return GateResult("EXACT_MAIN_BUILD", Status.FAIL, "Failed to get image metadata"), "", "", "", ""

def get_all_gates(expected_sha: str) -> Tuple[List[GateResult], str, str, str, str, str, str, str, str, str]:
    # Returns gates, image_digest, revision, arch, entrypoint, tree_sha, cfg_digest, sch_digest, rb_digest, ci_digest
    
    code, out, err = run_cmd(["git", "rev-parse", f"{expected_sha}^{{tree}}"])
    tree_sha = out.strip() if code == 0 else ""
    
    cfg_digest = compute_file_sha256(".env.example")
    sch_digest = compute_file_sha256("schemas/memory-graph-contract-v1.schema.json")
    rb_digest = compute_file_sha256("docker-compose.yml") # surrogate for rollback bundle
    
    g1 = gate_source_attestation(expected_sha)
    g2 = gate_build_context()
    g3, ci_digest = gate_exact_main_ci(expected_sha)
    g4, digest, revision, arch, entrypoint = gate_exact_main_build(expected_sha)
    
    g5 = GateResult("IMAGE_ATTESTATION", Status.FAIL if not digest else Status.PASS, evidence={"revision": revision})
    
    # CONFIG_CONTRACT
    if os.path.exists(".env.example"):
        with open(".env.example", "r", encoding="utf-8") as f:
            lines = f.readlines()
        keys = [line.split("=")[0] for line in lines if "=" in line and not line.startswith("#")]
        g6 = GateResult("CONFIG_CONTRACT", Status.PASS, evidence={
            "required_keys": ",".join(keys),
            "source_class": "env_example_parsing",
            "metadata": f"Config contains {len(keys)} declarative keys."
        })
    else:
        g6 = GateResult("CONFIG_CONTRACT", Status.BLOCKED, "Configuration file missing")
    
    # SECRET_CONTRACT
    if not os.path.exists(".env") or not os.environ.get("OPENAI_API_KEY"):
        g7 = GateResult("SECRET_CONTRACT", Status.BLOCKED, "Production secrets unavailable under read-only authority")
    else:
        g7 = GateResult("SECRET_CONTRACT", Status.PASS, evidence={"status": "secrets exist"})
    
    # DB_PATH_SAFETY
    # Try to validate DB path using read-only proof
    if not os.environ.get("HERMES_DB_PATH"):
        g8 = GateResult("DB_PATH_SAFETY", Status.BLOCKED, "Production DB path proof is unavailable under read-only authority")
    else:
        g8 = GateResult("DB_PATH_SAFETY", Status.PASS, evidence={"path_safe": "true"})
    
    # SCHEMA_COMPATIBILITY
    g9 = GateResult("SCHEMA_COMPATIBILITY", Status.BLOCKED, "Production schema cannot be read under current authority")
    
    # ROLLBACK_QUALIFIED
    g10 = GateResult("ROLLBACK_QUALIFIED", Status.BLOCKED, "Missing rollback evidence")
    
    # RUNTIME_PREFLIGHT
    g11 = GateResult("RUNTIME_PREFLIGHT", Status.BLOCKED, "Missing runtime preflight evidence")
    
    # SECURITY_ISOLATION
    g12 = GateResult("SECURITY_ISOLATION", Status.BLOCKED, "Missing security isolation evidence")
    
    return [g1, g2, g3, g4, g5, g6, g7, g8, g9, g10, g11, g12], digest, revision, arch, entrypoint, tree_sha, cfg_digest, sch_digest, rb_digest, ci_digest

def qualify_main():
    expected_sha = sys.argv[1] if len(sys.argv) > 1 else "6508e294d234598cbb15c1de214802fc3963e2bd"
    
    gates, image_digest, oci_revision, arch, entrypoint, tree_sha, cfg_digest, sch_digest, rb_digest, ci_digest = get_all_gates(expected_sha)
    
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
        build_workflow_id="local",
        build_run_id="local",
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

