import pytest
from ai_engineering.memory_graph_activation_readiness import (
    MemoryGraphShadowActivationPreflight,
    MemoryGraphShadowHealthReceipt,
    check_activation_readiness,
)

def test_activation_readiness_pass():
    result = check_activation_readiness(
        subject_main_sha="a"*40,
        candidate_image_revision="rev1",
        expected_candidate_image_revision="rev1",
        db_path_safe=True,
        db_integrity="ok",
        foreign_key_violations=0,
        graph_schema_classification="CURRENT",
        backup_required=True,
        backup_valid=True,
        rollback_proven=True,
        shadow_mode_available=True,
        serve_mode_available=False,
        graph_context_served_to_users=False,
        production_activation_authorized=True,
        expected_subject_main_sha="a"*40,
    )
    assert result.verdict == "PASS"
    assert not result.reason_codes

def test_activation_readiness_blocked_schema():
    result = check_activation_readiness(
        subject_main_sha="a"*40,
        candidate_image_revision="rev1",
        expected_candidate_image_revision="rev1",
        db_path_safe=True,
        db_integrity="ok",
        foreign_key_violations=0,
        graph_schema_classification="INCOMPATIBLE",
        backup_required=True,
        backup_valid=True,
        rollback_proven=True,
        shadow_mode_available=True,
        serve_mode_available=False,
        graph_context_served_to_users=False,
        production_activation_authorized=True,
        expected_subject_main_sha="a"*40,
    )
    assert result.verdict == "BLOCKED"
    assert "GRAPH_SCHEMA_INCOMPATIBLE" in result.reason_codes
