"""
Production Readiness Qualification & Exact-Main Release Candidate Attestation.
Task 8 layer.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from ai_engineering.contracts import Status

@dataclass(frozen=True, slots=True)
class GateResult:
    gate_name: str
    status: Status
    reason: Optional[str] = None
    evidence: Optional[Dict[str, str]] = None

@dataclass(frozen=True, slots=True)
class ReleaseCandidateManifest:
    schema_version: int
    release_candidate_id: str
    repository: str
    canonical_main_ref: str
    canonical_main_sha: str
    git_tree_sha: str
    build_source_sha: str
    container_image_repository: str
    container_image_digest: str
    oci_revision: str
    build_workflow_id: str
    build_run_id: str
    configuration_contract_digest: str
    schema_contract_digest: str
    rollback_bundle_digest: str
    required_ci_snapshot_digest: str
    qualification_timestamp_utc: str
    manifest_digest: str

@dataclass(frozen=True, slots=True)
class ReleaseQualificationRequest:
    schema_version: int
    qualification_id: str
    repository: str
    canonical_remote: str
    canonical_main_ref: str
    canonical_main_sha: str
    task8_run_id: str
    required_ci_workflows: tuple[str, ...]
    production_target_id: str
    requested_at_utc: str
    request_digest: str

@dataclass(frozen=True, slots=True)
class ProductionReadinessReport:
    schema_version: int
    report_id: str
    release_candidate_id: str
    canonical_main_sha: str
    image_digest: str
    gate_results: tuple[GateResult, ...]
    technical_blockers: tuple[str, ...]
    governance_observations: tuple[str, ...]
    migration_required: bool
    rollback_verified: bool
    credential_risk_blocked: bool
    technical_release_candidate_ready: bool
    production_deployment_authorized: bool
    generated_at_utc: str
    report_digest: str

@dataclass(frozen=True, slots=True)
class ReleaseCandidateReceipt:
    qualification_id: str
    canonical_main_sha: str
    image_digest: str
    manifest_digest: str
    report: ProductionReadinessReport
    result: Status
    created_at_utc: str

class ReleaseQualifier:
    """
    Validates a release candidate for production readiness without deploying.
    """
    def qualify(
        self,
        request: ReleaseQualificationRequest,
        manifest: ReleaseCandidateManifest,
        gates: List[GateResult],
        has_credential_risk: bool,
        current_main_sha: str,
        timestamp: str
    ) -> ReleaseCandidateReceipt:
        
        blockers = []
        main_advanced = False
        
        if current_main_sha != request.canonical_main_sha:
            main_advanced = True
            blockers.append("MAIN_ADVANCED_DURING_RELEASE_QUALIFICATION")

        if manifest.build_source_sha != request.canonical_main_sha:
            blockers.append("BUILD_SOURCE_SHA_MISMATCH")
            
        if manifest.oci_revision != request.canonical_main_sha:
            blockers.append("IMAGE_REVISION_MISMATCH")

        if manifest.canonical_main_sha != request.canonical_main_sha:
            blockers.append("MANIFEST_SHA_MISMATCH")
            
        for g in gates:
            if g.status in (Status.FAIL, Status.BLOCKED):
                blockers.append(f"GATE_FAILED: {g.gate_name}")

        technical_ready = len(blockers) == 0
        
        # NEVER authorized automatically by Task 8
        production_authorized = False
        
        credential_blocked = has_credential_risk
        
        report_status = Status.PASS if technical_ready else Status.FAIL
        if technical_ready and credential_blocked:
            report_status = Status.BLOCKED
            
        report_id = f"report-{manifest.release_candidate_id}"
        
        report = ProductionReadinessReport(
            schema_version=1,
            report_id=report_id,
            release_candidate_id=manifest.release_candidate_id,
            canonical_main_sha=request.canonical_main_sha,
            image_digest=manifest.container_image_digest,
            gate_results=tuple(gates),
            technical_blockers=tuple(blockers),
            governance_observations=(),
            migration_required=any(g.gate_name == "SCHEMA_COMPATIBILITY" and g.evidence and g.evidence.get("migration_required") == "true" for g in gates),
            rollback_verified=any(g.gate_name == "ROLLBACK_QUALIFIED" and g.status == Status.PASS for g in gates),
            credential_risk_blocked=credential_blocked,
            technical_release_candidate_ready=technical_ready,
            production_deployment_authorized=production_authorized,
            generated_at_utc=timestamp,
            report_digest=hashlib.sha256(report_id.encode()).hexdigest()
        )
        
        return ReleaseCandidateReceipt(
            qualification_id=request.qualification_id,
            canonical_main_sha=request.canonical_main_sha,
            image_digest=manifest.container_image_digest,
            manifest_digest=manifest.manifest_digest,
            report=report,
            result=report_status,
            created_at_utc=timestamp
        )
