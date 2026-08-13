import pytest
import json
from ai_engineering.task_intent import (
    TaskIntent,
    IntentUnknown,
    AcceptanceCriterion,
    IntentStatus,
    TaskIntentValidationError,
    validate_intent,
    serialize_intent,
    deserialize_intent,
    intent_digest,
    validate_intent_revision,
)
from ai_engineering.contracts import TaskClass, StopBoundary

def test_valid_minimal_intent():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    intent = validate_intent(payload)
    assert intent.task_id == "TASK-001"
    assert intent.status == IntentStatus.DRAFT

def test_valid_full_intent():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": ["No new dependencies"],
        "allowed_mutations": ["src/app.py"],
        "forbidden_mutations": ["src/db.py"],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "Works"}],
        "unknowns": [],
        "applicable_invariants": ["No downtime"],
        "required_gates": ["tests"],
        "parent_intent_digest": None,
    }
    intent = validate_intent(payload)
    assert intent.task_id == "TASK-001"

def test_unknown_schema_version_fails():
    payload = {
        "schema_version": 2,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent(payload)
    assert excinfo.value.code == "SCHEMA_VERSION_UNSUPPORTED"

def test_missing_task_id_fails():
    payload = {
        "schema_version": 1,
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent(payload)
    assert excinfo.value.code == "REQUIRED_FIELD_MISSING"

def test_empty_desired_outcome_fails():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "   ",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent(payload)
    assert excinfo.value.code == "DESIRED_OUTCOME_EMPTY"

def test_duplicate_acceptance_criterion_id_fails():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [
            {"criterion_id": "AC-1", "statement": "A"},
            {"criterion_id": "AC-1", "statement": "B"}
        ],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent(payload)
    assert excinfo.value.code == "DUPLICATE_ACCEPTANCE_CRITERION_ID"

def test_ready_with_blocking_unknown_fails():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [
            {"unknown_id": "UNK-1", "description": "Needs answer", "blocking": True}
        ],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent(payload)
    assert excinfo.value.code == "READY_WITH_BLOCKING_UNKNOWN"

def test_valid_draft_with_blocking_unknown():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [
            {"unknown_id": "UNK-1", "description": "Needs answer", "blocking": True}
        ],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    validate_intent(payload)

def test_valid_needs_clarification():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "NEEDS_CLARIFICATION",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [
            {"unknown_id": "UNK-1", "description": "Needs answer", "blocking": True}
        ],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    validate_intent(payload)

def test_valid_ready_intent():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    validate_intent(payload)

def test_canonical_serialization_deterministic():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    s1 = serialize_intent(payload)
    s2 = serialize_intent(payload)
    assert s1 == s2

def test_key_order_does_not_change_digest():
    payload1 = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    payload2 = {
        "task_id": "TASK-001",
        "schema_version": 1,
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    assert intent_digest(payload1) == intent_digest(payload2)

def test_formatting_does_not_change_digest():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    s1 = json.dumps(payload, indent=4)
    s2 = json.dumps(payload, separators=(',', ':'))
    assert intent_digest(deserialize_intent(s1)) == intent_digest(deserialize_intent(s2))

def test_semantic_change_changes_digest():
    payload1 = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    payload2 = payload1.copy()
    payload2["desired_outcome"] = "Fix a bug and refactor"
    assert intent_digest(payload1) != intent_digest(payload2)

def test_initial_revision_valid():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    validate_intent(payload)

def test_next_revision_parent_link_valid():
    parent_payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    parent = validate_intent(parent_payload)
    digest = intent_digest(parent)

    child_payload = parent_payload.copy()
    child_payload["intent_revision"] = 2
    child_payload["parent_intent_digest"] = digest
    child = validate_intent(child_payload)

    validate_intent_revision(parent, child)

def test_broken_parent_identity_fails():
    parent_payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    parent = validate_intent(parent_payload)
    digest = intent_digest(parent)

    child_payload = parent_payload.copy()
    child_payload["intent_revision"] = 2
    child_payload["parent_intent_digest"] = "b" * 64
    child = validate_intent(child_payload)

    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent_revision(parent, child)
    assert excinfo.value.code == "BROKEN_PARENT_IDENTITY"

def test_self_supersession_fails():
    parent_payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "DRAFT",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    parent = validate_intent(parent_payload)
    digest = intent_digest(parent)
    
    # Fake self supersession - parent claims it supersedes itself
    bad_child_payload = parent_payload.copy()
    bad_child_payload["intent_revision"] = 2
    bad_child_payload["parent_intent_digest"] = digest
    bad_child = validate_intent(bad_child_payload)
    # The digest of bad_child is calculated to see if it equals parent_digest.
    # The test is that if the new intent has the exact same digest as the parent, it fails.
    # But intent_digest(child) != intent_digest(parent) normally because intent_revision changed.
    # We construct an invalid state manually for the validator function:
    
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent_revision(bad_child, bad_child)
    assert excinfo.value.code == "SELF_SUPERSESSION"

def test_path_traversal_fails():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": ["../outside/file"],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent(payload)
    assert excinfo.value.code == "PATH_TRAVERSAL_FORBIDDEN"

def test_absolute_escape_path_fails():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": ["/absolute/file"],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent(payload)
    assert excinfo.value.code == "ABSOLUTE_PATH_FORBIDDEN"

def test_unsafe_windows_drive_path_fails():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": [r"C:\Windows\System32"],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    with pytest.raises(TaskIntentValidationError) as excinfo:
        validate_intent(payload)
    assert excinfo.value.code == "DRIVE_PATH_FORBIDDEN"

def test_safe_repo_relative_path():
    payload = {
        "schema_version": 1,
        "task_id": "TASK-001",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Fix a bug",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": ["src/app.py", "scripts/test.sh"],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None,
    }
    validate_intent(payload)
