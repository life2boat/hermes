"""
Production Readiness Qualification & Exact-Main Release Candidate Attestation.
Task 8 layer.
"""

import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Any
from ai_engineering.contracts import Status

def _compute_digest(data: Dict[str, Any], omit_key: str) -> str:
    cleaned = {k: v for k, v in data.items() if k != omit_key}
    # Sort keys for deterministic output
    serialized = json.dumps(cleaned, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

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
    architecture: str
    entrypoint: str
    manifest_digest: str

    @classmethod
    def create(cls, **kwargs) -> "ReleaseCandidateManifest":
        if 'manifest_digest' in kwargs:
            del kwargs['manifest_digest']
        digest = _compute_digest(kwargs, "")
        return cls(manifest_digest=digest, **kwargs)

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

    @classmethod
    def create(cls, **kwargs) -> "ReleaseQualificationRequest":
        if 'request_digest' in kwargs:
            del kwargs['request_digest']
        digest = _compute_digest(kwargs, "")
        return cls(request_digest=digest, **kwargs)

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

    @classmethod
    def create(cls, **kwargs) -> "ProductionReadinessReport":
        if 'report_digest' in kwargs:
            del kwargs['report_digest']
        
        # Serialize nested enum carefully
        data_to_hash = dict(kwargs)
        if 'gate_results' in data_to_hash:
            data_to_hash['gate_results'] = [
                {"gate_name": g.gate_name, "status": g.status.value, "reason": g.reason, "evidence": g.evidence} 
                for g in data_to_hash['gate_results']
            ]
            
        digest = _compute_digest(data_to_hash, "")
        return cls(report_digest=digest, **kwargs)

@dataclass(frozen=True, slots=True)
class ReleaseCandidateReceipt:
    schema_version: int
    receipt_id: str
    qualification_id: str
    canonical_main_sha: str
    image_digest: str
    manifest_digest: str
    readiness_report_digest: str
    report: ProductionReadinessReport
    result: Status
    created_at_utc: str
    receipt_digest: str

    @classmethod
    def create(cls, **kwargs) -> "ReleaseCandidateReceipt":
        if 'receipt_digest' in kwargs:
            del kwargs['receipt_digest']
        
        data_to_hash = dict(kwargs)
        if 'result' in data_to_hash:
            data_to_hash['result'] = data_to_hash['result'].value
        if 'report' in data_to_hash:
            # report is omitted from hash to prevent double hashing, we rely on readiness_report_digest
            del data_to_hash['report']
            
        digest = _compute_digest(data_to_hash, "")
        return cls(receipt_digest=digest, **kwargs)

class ReleaseQualifier:
    """
    Validates a release candidate for production readiness without deploying.
    """
    REQUIRED_GATES = {
        "SOURCE_ATTESTATION",
        "EXACT_MAIN_CI",
        "BUILD_CONTEXT",
        "EXACT_MAIN_BUILD",
        "IMAGE_ATTESTATION",
        "CONFIG_CONTRACT",
        "SECRET_CONTRACT",
        "DB_PATH_SAFETY",
        "SCHEMA_COMPATIBILITY",
        "ROLLBACK_QUALIFIED",
        "RUNTIME_PREFLIGHT",
        "SECURITY_ISOLATION"
    }

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
            
        if manifest.build_workflow_id == "local" or manifest.build_run_id == "local":
            blockers.append("LOCAL_BUILD_ID_NOT_ALLOWED")
            
        # Check mandatory gate completeness
        provided_gates = {g.gate_name for g in gates}
        missing_gates = self.REQUIRED_GATES - provided_gates
        if missing_gates:
            blockers.append(f"MISSING_MANDATORY_GATES: {sorted(list(missing_gates))}")
        
        # Check for duplicated gates
        if len(provided_gates) != len(gates):
            blockers.append("DUPLICATED_MANDATORY_GATES")
            
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
        
        report = ProductionReadinessReport.create(
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
        )
        
        return ReleaseCandidateReceipt.create(
            schema_version=1,
            receipt_id=f"receipt-{request.qualification_id}",
            qualification_id=request.qualification_id,
            canonical_main_sha=request.canonical_main_sha,
            image_digest=manifest.container_image_digest,
            manifest_digest=manifest.manifest_digest,
            readiness_report_digest=report.report_digest,
            report=report,
            result=report_status,
            created_at_utc=timestamp,
        )
