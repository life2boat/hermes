"""
Production Readiness Qualification & Exact-Main Release Candidate Attestation.
Task 8 layer.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from ai_engineering.contracts import Status

@dataclass
class ReleaseCandidateManifest:
    canonical_main_sha: str
    oci_revision: str
    image_digest: str
    is_immutable: bool
    architecture: str
    entrypoint: str

@dataclass
class ReleaseQualificationRequest:
    candidate_manifest: ReleaseCandidateManifest
    current_canonical_main_sha: str
    build_source_sha: str
    source_attestation_passed: bool
    is_clean_source_tree: bool
    configuration_keys_present: bool
    secret_keys_present: bool
    db_schema_compatible: bool
    has_credential_risk: bool
    previous_known_good_sha: Optional[str] = None
    mock_startup_passed: bool = False
    security_isolation_passed: bool = False

@dataclass
class ProductionReadinessReport:
    status: Status
    technical_release_candidate_ready: bool
    production_deployment_authorized: bool
    credential_risk_blocked: bool
    main_advanced_during_release_qualification: bool
    reasons: List[str]

@dataclass
class ReleaseCandidateReceipt:
    sha: str
    report: ProductionReadinessReport

class ReleaseQualifier:
    def qualify(self, request: ReleaseQualificationRequest) -> ReleaseCandidateReceipt:
        reasons = []
        status = Status.PASS
        technical_ready = True
        deployment_authorized = True
        credential_blocked = False
        main_advanced = False

        if request.current_canonical_main_sha != request.candidate_manifest.canonical_main_sha:
            reasons.append("MAIN_ADVANCED_DURING_RELEASE_QUALIFICATION")
            main_advanced = True
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if request.build_source_sha != request.candidate_manifest.canonical_main_sha:
            reasons.append("EXACT_SHA_MISMATCH: build source != canonical main")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False
            
        if request.candidate_manifest.oci_revision != request.candidate_manifest.canonical_main_sha:
            reasons.append("EXACT_SHA_MISMATCH: oci revision != canonical main")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if not request.source_attestation_passed:
            reasons.append("SOURCE_ATTESTATION_FAILED")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if not request.is_clean_source_tree:
            reasons.append("DIRTY_BUILD_CONTEXT")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if not request.candidate_manifest.image_digest or not request.candidate_manifest.is_immutable:
            reasons.append("IMAGE_ATTESTATION_FAILED: missing digest or not immutable")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if not request.configuration_keys_present:
            reasons.append("CONFIGURATION_CONTRACT_FAILED")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if not request.secret_keys_present:
            reasons.append("SECRET_CONTRACT_FAILED")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if not request.db_schema_compatible:
            reasons.append("DB_SCHEMA_INCOMPATIBLE")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if not request.mock_startup_passed:
            reasons.append("RUNTIME_PREFLIGHT_FAILED")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if not request.security_isolation_passed:
            reasons.append("SECURITY_ISOLATION_FAILED")
            status = Status.FAIL
            technical_ready = False
            deployment_authorized = False

        if request.has_credential_risk:
            reasons.append("CREDENTIAL_RISK_BLOCKED")
            credential_blocked = True
            deployment_authorized = False
            if status == Status.PASS:
                status = Status.BLOCKED

        return ReleaseCandidateReceipt(
            sha=request.candidate_manifest.canonical_main_sha,
            report=ProductionReadinessReport(
                status=status,
                technical_release_candidate_ready=technical_ready,
                production_deployment_authorized=deployment_authorized,
                credential_risk_blocked=credential_blocked,
                main_advanced_during_release_qualification=main_advanced,
                reasons=reasons
            )
        )
