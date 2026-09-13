import pytest
from ai_engineering.supervisor.production_readiness import (
    ReleaseCandidateManifest,
    ReleaseQualificationRequest,
    ReleaseQualifier,
    GateResult,
)
from ai_engineering.contracts import Status

@pytest.fixture
def base_manifest():
    return ReleaseCandidateManifest(
        schema_version=1,
        release_candidate_id="rc-12345",
        repository="life2boat/hermes",
        canonical_main_ref="refs/remotes/github/main",
        canonical_main_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        git_tree_sha="tree_sha_123",
        build_source_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        container_image_repository="ghcr.io/life2boat/hermes",
        container_image_digest="sha256:d3f3f0...real_digest",
        oci_revision="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        build_workflow_id="wf_123",
        build_run_id="run_123",
        configuration_contract_digest="cfg_sha",
        schema_contract_digest="schema_sha",
        rollback_bundle_digest="rollback_sha",
        required_ci_snapshot_digest="ci_sha",
        qualification_timestamp_utc="2026-09-13T12:00:00Z",
        manifest_digest="manifest_sha_123"
    )

@pytest.fixture
def base_request():
    return ReleaseQualificationRequest(
        schema_version=1,
        qualification_id="qual-123",
        repository="life2boat/hermes",
        canonical_remote="github",
        canonical_main_ref="refs/remotes/github/main",
        canonical_main_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        task8_run_id="run_123",
        required_ci_workflows=("tests", "lint"),
        production_target_id="prod-1",
        requested_at_utc="2026-09-13T12:00:00Z",
        request_digest="req_sha"
    )

@pytest.fixture
def base_gates():
    return [
        GateResult("SOURCE_ATTESTATION", Status.PASS),
        GateResult("EXACT_MAIN_CI", Status.PASS),
        GateResult("BUILD_CONTEXT", Status.PASS),
        GateResult("EXACT_MAIN_BUILD", Status.PASS),
        GateResult("IMAGE_ATTESTATION", Status.PASS),
        GateResult("CONFIG_CONTRACT", Status.PASS),
        GateResult("SECRET_CONTRACT", Status.PASS),
        GateResult("DB_PATH_SAFETY", Status.PASS),
        GateResult("SCHEMA_COMPATIBILITY", Status.PASS),
        GateResult("ROLLBACK_QUALIFIED", Status.PASS),
        GateResult("RUNTIME_PREFLIGHT", Status.PASS),
        GateResult("SECURITY_ISOLATION", Status.PASS),
    ]

def test_task8_qualify_pass(base_request, base_manifest, base_gates):
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(
        request=base_request,
        manifest=base_manifest,
        gates=base_gates,
        has_credential_risk=False,
        current_main_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        timestamp="2026-09-13T12:05:00Z"
    )
    assert receipt.result == Status.PASS
    assert receipt.report.technical_release_candidate_ready is True
    assert receipt.report.production_deployment_authorized is False
    assert receipt.report.credential_risk_blocked is False

def test_task8_qualify_credential_risk_blocked(base_request, base_manifest, base_gates):
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(
        request=base_request,
        manifest=base_manifest,
        gates=base_gates,
        has_credential_risk=True,
        current_main_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        timestamp="2026-09-13T12:05:00Z"
    )
    assert receipt.result == Status.BLOCKED
    assert receipt.report.technical_release_candidate_ready is True
    assert receipt.report.production_deployment_authorized is False
    assert receipt.report.credential_risk_blocked is True

def test_task8_qualify_main_advanced(base_request, base_manifest, base_gates):
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(
        request=base_request,
        manifest=base_manifest,
        gates=base_gates,
        has_credential_risk=False,
        current_main_sha="new_sha",
        timestamp="2026-09-13T12:05:00Z"
    )
    assert receipt.result == Status.FAIL
    assert receipt.report.technical_release_candidate_ready is False
    assert "MAIN_ADVANCED_DURING_RELEASE_QUALIFICATION" in receipt.report.technical_blockers

def test_task8_qualify_dirty_build_context(base_request, base_manifest, base_gates):
    base_gates[2] = GateResult("BUILD_CONTEXT", Status.FAIL)
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(
        request=base_request,
        manifest=base_manifest,
        gates=base_gates,
        has_credential_risk=False,
        current_main_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        timestamp="2026-09-13T12:05:00Z"
    )
    assert receipt.result == Status.FAIL
    assert receipt.report.technical_release_candidate_ready is False
    assert "GATE_FAILED: BUILD_CONTEXT" in receipt.report.technical_blockers

def test_task8_qualify_schema_incompatible(base_request, base_manifest, base_gates):
    base_gates[8] = GateResult("SCHEMA_COMPATIBILITY", Status.FAIL)
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(
        request=base_request,
        manifest=base_manifest,
        gates=base_gates,
        has_credential_risk=False,
        current_main_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        timestamp="2026-09-13T12:05:00Z"
    )
    assert receipt.result == Status.FAIL
    assert receipt.report.technical_release_candidate_ready is False
    assert "GATE_FAILED: SCHEMA_COMPATIBILITY" in receipt.report.technical_blockers
