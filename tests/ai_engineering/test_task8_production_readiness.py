import pytest
from ai_engineering.supervisor.production_readiness import (
    ReleaseCandidateManifest,
    ReleaseQualificationRequest,
    ReleaseQualifier,
)
from ai_engineering.contracts import Status

@pytest.fixture
def base_manifest():
    return ReleaseCandidateManifest(
        canonical_main_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        oci_revision="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        image_digest="sha256:dummy",
        is_immutable=True,
        architecture="amd64",
        entrypoint="/bin/sh"
    )

@pytest.fixture
def base_request(base_manifest):
    return ReleaseQualificationRequest(
        candidate_manifest=base_manifest,
        current_canonical_main_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        build_source_sha="f0e1ca8cb7b35fccc31cd066a37942aead2f652c",
        source_attestation_passed=True,
        is_clean_source_tree=True,
        configuration_keys_present=True,
        secret_keys_present=True,
        db_schema_compatible=True,
        has_credential_risk=False,
        previous_known_good_sha="prev_sha",
        mock_startup_passed=True,
        security_isolation_passed=True
    )

def test_task8_qualify_pass(base_request):
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(base_request)
    assert receipt.report.status == Status.PASS
    assert receipt.report.technical_release_candidate_ready is True
    assert receipt.report.production_deployment_authorized is True
    assert receipt.report.credential_risk_blocked is False

def test_task8_qualify_credential_risk_blocked(base_request):
    base_request.has_credential_risk = True
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(base_request)
    assert receipt.report.status == Status.BLOCKED
    assert receipt.report.technical_release_candidate_ready is True
    assert receipt.report.production_deployment_authorized is False
    assert receipt.report.credential_risk_blocked is True
    assert "CREDENTIAL_RISK_BLOCKED" in receipt.report.reasons

def test_task8_qualify_main_advanced(base_request):
    base_request.current_canonical_main_sha = "new_sha"
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(base_request)
    assert receipt.report.status == Status.FAIL
    assert receipt.report.technical_release_candidate_ready is False
    assert receipt.report.main_advanced_during_release_qualification is True
    assert "MAIN_ADVANCED_DURING_RELEASE_QUALIFICATION" in receipt.report.reasons

def test_task8_qualify_dirty_build_context(base_request):
    base_request.is_clean_source_tree = False
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(base_request)
    assert receipt.report.status == Status.FAIL
    assert receipt.report.technical_release_candidate_ready is False
    assert "DIRTY_BUILD_CONTEXT" in receipt.report.reasons

def test_task8_qualify_schema_incompatible(base_request):
    base_request.db_schema_compatible = False
    qualifier = ReleaseQualifier()
    receipt = qualifier.qualify(base_request)
    assert receipt.report.status == Status.FAIL
    assert receipt.report.technical_release_candidate_ready is False
    assert "DB_SCHEMA_INCOMPATIBLE" in receipt.report.reasons
