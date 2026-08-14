"""Tests for the Clarification and Requirements Quality Gate layer."""

import os
import json
from pathlib import Path
from unittest import mock

import pytest

from ai_engineering.contracts import StopBoundary, TaskClass
from ai_engineering.task_intent import (
    AcceptanceCriterion,
    IntentStatus,
    IntentUnknown,
    TaskIntent,
    TaskIntentValidationError,
    intent_digest,
    validate_intent,
)
from ai_engineering.requirements_gate import (
    ClarificationQuestion,
    ClarificationReport,
    CriterionDimension,
    CriterionReview,
    GateBlockingReason,
    GateStatus,
    GlobalDimension,
    RequirementsGateError,
    RequirementsGateReport,
    RequirementsQualityReview,
    ReviewStatus,
    compute_clarification_id,
    compute_requirements_review_id,
    create_requirements_quality_review,
    deserialize_clarification,
    deserialize_review,
    evaluate_requirements_gate,
    generate_clarification_report,
    serialize_clarification,
    serialize_gate,
    serialize_review,
    validate_clarification,
    validate_review,
)
from scripts._cli_utils import (
    OutputAliasError,
    SafeReadError,
    check_output_alias,
    safe_read,
)


def make_valid_intent(
    status: IntentStatus = IntentStatus.READY,
    unknowns: tuple[IntentUnknown, ...] = (),
    revision: int = 1,
) -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id="TASK-123",
        intent_revision=revision,
        status=status,
        task_class=TaskClass.SMALL_PRECISE_FIX,
        desired_outcome="Do a thing.",
        source_repository="life2boat/hermes",
        source_main_ref="main",
        source_base_sha="032281fcb73eb38b1da75886cfe96fd09b8da704",
        constraints=(),
        allowed_mutations=(),
        forbidden_mutations=(),
        stop_boundary=StopBoundary.DRAFT_PR,
        acceptance_criteria=(
            AcceptanceCriterion(criterion_id="AC-001", statement="Test AC 1"),
            AcceptanceCriterion(criterion_id="AC-002", statement="Test AC 2"),
        ),
        unknowns=unknowns,
        applicable_invariants=(),
        required_gates=(),
    )


def make_valid_review(
    intent: TaskIntent, reviewer_id: str = "test-reviewer"
) -> RequirementsQualityReview:
    c_reviews = [
        CriterionReview(
            criterion_id=c.criterion_id,
            dimensions={
                CriterionDimension.CLEAR: ReviewStatus.PASS,
                CriterionDimension.TESTABLE: ReviewStatus.PASS,
                CriterionDimension.BOUNDED: ReviewStatus.PASS,
            },
        )
        for c in intent.acceptance_criteria
    ]
    g_reviews = {
        GlobalDimension.DESIRED_OUTCOME_CLEAR: ReviewStatus.PASS,
        GlobalDimension.SCOPE_BOUNDARIES_CLEAR: ReviewStatus.PASS,
        GlobalDimension.CONSTRAINTS_REVIEWED: ReviewStatus.PASS,
        GlobalDimension.UNKNOWN_RESOLUTION_REVIEWED: ReviewStatus.PASS,
        GlobalDimension.INTERNAL_CONFLICT_REVIEWED: ReviewStatus.PASS,
    }
    return create_requirements_quality_review(
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        reviewer_id=reviewer_id,
        criterion_reviews=c_reviews,
        global_reviews=g_reviews,
    )


# --- Clarification Tests ---


def test_valid_clarification_report_v1():
    intent = make_valid_intent(
        status=IntentStatus.NEEDS_CLARIFICATION,
        unknowns=(IntentUnknown("unk-1", "desc", blocking=True),),
    )
    report = generate_clarification_report(intent)

    assert report.schema_version == 1
    assert report.task_id == intent.task_id
    assert report.intent_digest == intent_digest(intent)
    assert len(report.questions) == 1
    assert report.blocking_question_count == 1
    assert report.ready_for_quality_review is False

    # Test round-trip serialization
    serialized = serialize_clarification(report)
    deserialized = deserialize_clarification(serialized)
    assert deserialized == report


def test_unknown_clarification_schema_version():
    intent = make_valid_intent(status=IntentStatus.DRAFT)
    report = generate_clarification_report(intent)

    data = json.loads(serialize_clarification(report))
    data["schema_version"] = 2

    with pytest.raises(RequirementsGateError) as exc:
        deserialize_clarification(json.dumps(data))
    assert exc.value.code == "SCHEMA_VERSION_UNSUPPORTED"


def test_same_intent_same_clarification_id():
    intent1 = make_valid_intent(status=IntentStatus.DRAFT)
    intent2 = make_valid_intent(status=IntentStatus.DRAFT)

    rep1 = generate_clarification_report(intent1)
    rep2 = generate_clarification_report(intent2)

    assert rep1.clarification_id == rep2.clarification_id


def test_changed_intent_changed_clarification_id():
    intent1 = make_valid_intent(status=IntentStatus.DRAFT)
    intent2 = make_valid_intent(status=IntentStatus.READY)

    rep1 = generate_clarification_report(intent1)
    rep2 = generate_clarification_report(intent2)

    assert rep1.clarification_id != rep2.clarification_id


def test_blocking_nonblocking_question_generation():
    intent = make_valid_intent(
        status=IntentStatus.NEEDS_CLARIFICATION,
        unknowns=(
            IntentUnknown("unk-1", "desc1", blocking=True),
            IntentUnknown("unk-2", "desc2", blocking=False),
        ),
    )
    report = generate_clarification_report(intent)

    assert len(report.questions) == 2
    assert report.questions[0].blocking is True
    assert report.questions[1].blocking is False
    assert report.blocking_question_count == 1

    # QUESTION_ID_STABLE
    assert (
        report.questions[0].question_id
        == f"{intent.task_id}::{intent.intent_revision}::unk-1"
    )
    assert (
        report.questions[1].question_id
        == f"{intent.task_id}::{intent.intent_revision}::unk-2"
    )


def test_clarify_mutates_task_intent():
    intent = make_valid_intent(status=IntentStatus.NEEDS_CLARIFICATION)
    dgst_before = intent_digest(intent)
    generate_clarification_report(intent)
    dgst_after = intent_digest(intent)

    assert dgst_before == dgst_after


def test_clarification_tampered_id_fails_validation():
    intent = make_valid_intent(
        status=IntentStatus.NEEDS_CLARIFICATION,
        unknowns=(IntentUnknown("unk-1", "desc", blocking=True),),
    )
    report = generate_clarification_report(intent)
    data = json.loads(serialize_clarification(report))
    data["clarification_id"] = "f" * 64

    with pytest.raises(RequirementsGateError) as exc:
        deserialize_clarification(json.dumps(data))
    assert exc.value.code == "TAMPERED_CLARIFICATION_ID"


def test_clarification_inconsistent_blocking_count_fails_validation():
    intent = make_valid_intent(
        status=IntentStatus.NEEDS_CLARIFICATION,
        unknowns=(IntentUnknown("unk-1", "desc", blocking=True),),
    )
    report = generate_clarification_report(intent)
    data = json.loads(serialize_clarification(report))
    data["blocking_question_count"] = 0

    with pytest.raises(RequirementsGateError) as exc:
        deserialize_clarification(json.dumps(data))
    assert exc.value.code == "VALUE_INVALID"


def test_clarification_inconsistent_ready_flag_fails_validation():
    intent = make_valid_intent(status=IntentStatus.READY)
    report = generate_clarification_report(intent)
    data = json.loads(serialize_clarification(report))
    data["ready_for_quality_review"] = False

    with pytest.raises(RequirementsGateError) as exc:
        deserialize_clarification(json.dumps(data))
    assert exc.value.code == "VALUE_INVALID"


# --- Quality Review Tests ---


def test_valid_quality_review_v1():
    intent = make_valid_intent()
    review = make_valid_review(intent)
    serialized = serialize_review(review)
    deserialized = deserialize_review(serialized)
    assert deserialized == review
    assert deserialized.schema_version == 1


def test_quality_review_deterministic_id():
    intent = make_valid_intent()
    r1 = make_valid_review(intent)
    r2 = make_valid_review(intent)
    assert r1.review_id == r2.review_id


def test_quality_review_tampered_outcome_fails():
    intent = make_valid_intent()
    review = make_valid_review(intent)
    data = json.loads(serialize_review(review))
    data["criterion_reviews"][0]["dimensions"]["CLEAR"] = "FAIL"
    # Keeping stale review_id
    with pytest.raises(RequirementsGateError) as exc:
        deserialize_review(json.dumps(data))
    assert exc.value.code == "TAMPERED_REVIEW_ID"


def test_quality_review_tampered_reviewer_fails():
    intent = make_valid_intent()
    review = make_valid_review(intent)
    data = json.loads(serialize_review(review))
    data["reviewer_id"] = "impostor"
    with pytest.raises(RequirementsGateError) as exc:
        deserialize_review(json.dumps(data))
    assert exc.value.code == "TAMPERED_REVIEW_ID"


def test_quality_review_arbitrary_64hex_id_fails():
    intent = make_valid_intent()
    review = make_valid_review(intent)
    data = json.loads(serialize_review(review))
    data["review_id"] = "e" * 64
    with pytest.raises(RequirementsGateError) as exc:
        deserialize_review(json.dumps(data))
    assert exc.value.code == "TAMPERED_REVIEW_ID"


def test_unknown_quality_review_schema_version():
    intent = make_valid_intent()
    review = make_valid_review(intent)
    data = json.loads(serialize_review(review))
    data["schema_version"] = 2

    with pytest.raises(RequirementsGateError) as exc:
        deserialize_review(json.dumps(data))
    assert exc.value.code == "SCHEMA_VERSION_UNSUPPORTED"


def test_duplicate_criterion_review():
    intent = make_valid_intent()
    review = make_valid_review(intent)
    data = json.loads(serialize_review(review))
    data["criterion_reviews"].append(data["criterion_reviews"][0])

    with pytest.raises(RequirementsGateError) as exc:
        deserialize_review(json.dumps(data))
    assert exc.value.code == "DUPLICATE_CRITERION_ID"


# --- Gate Tests ---


def _prepare_gate_inputs(status=IntentStatus.READY, unknowns=()):
    intent = make_valid_intent(status=status, unknowns=unknowns)
    clarification = generate_clarification_report(intent)
    valid_review = make_valid_review(intent)
    return intent, clarification, valid_review


def test_ready_complete_review_gate():
    intent, clarification, review = _prepare_gate_inputs(status=IntentStatus.READY)
    gate = evaluate_requirements_gate(intent, clarification, review)

    assert gate.status == GateStatus.PASS
    assert len(gate.blocking_reasons) == 0


def test_draft_gate():
    intent, clarification, review = _prepare_gate_inputs(status=IntentStatus.DRAFT)
    gate = evaluate_requirements_gate(intent, clarification, review)

    assert gate.status == GateStatus.FAIL
    assert GateBlockingReason.INTENT_NOT_READY in gate.blocking_reasons


def test_needs_clarification_gate():
    intent, clarification, review = _prepare_gate_inputs(
        status=IntentStatus.NEEDS_CLARIFICATION
    )
    gate = evaluate_requirements_gate(intent, clarification, review)

    assert gate.status == GateStatus.FAIL
    assert GateBlockingReason.INTENT_NOT_READY in gate.blocking_reasons


def test_ready_with_blocking_unknown():
    # READY + blocking unknown should fail canonical intent validation before we even get to PR-3
    with pytest.raises(TaskIntentValidationError) as exc:
        intent = make_valid_intent(
            status=IntentStatus.READY,
            unknowns=(IntentUnknown("unk-1", "desc", blocking=True),),
        )
        validate_intent(intent)
    assert exc.value.code == "READY_WITH_BLOCKING_UNKNOWN"


def test_stale_clarification_gate():
    intent1, clar1, rev1 = _prepare_gate_inputs()
    intent2 = make_valid_intent(status=IntentStatus.READY, revision=2)

    gate = evaluate_requirements_gate(intent2, clar1, rev1)

    assert gate.status == GateStatus.FAIL
    assert GateBlockingReason.CLARIFICATION_INTENT_MISMATCH in gate.blocking_reasons
    assert GateBlockingReason.QUALITY_REVIEW_INTENT_MISMATCH in gate.blocking_reasons


def test_missing_criterion_review():
    intent, clar, _ = _prepare_gate_inputs()
    # Build review with only AC-002
    c_reviews = [
        CriterionReview(
            criterion_id="AC-002",
            dimensions={d: ReviewStatus.PASS for d in CriterionDimension},
        )
    ]
    g_reviews = {d: ReviewStatus.PASS for d in GlobalDimension}
    rev = create_requirements_quality_review(
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        reviewer_id="test-reviewer",
        criterion_reviews=c_reviews,
        global_reviews=g_reviews,
    )

    gate = evaluate_requirements_gate(intent, clar, rev)
    assert gate.status == GateStatus.FAIL
    assert GateBlockingReason.CRITERION_REVIEW_MISSING in gate.blocking_reasons


def test_unknown_criterion_review():
    intent, clar, _ = _prepare_gate_inputs()
    c_reviews = [
        CriterionReview(
            criterion_id=c.criterion_id,
            dimensions={d: ReviewStatus.PASS for d in CriterionDimension},
        )
        for c in intent.acceptance_criteria
    ]
    c_reviews.append(
        CriterionReview(
            criterion_id="AC-999",
            dimensions={d: ReviewStatus.PASS for d in CriterionDimension},
        )
    )
    g_reviews = {d: ReviewStatus.PASS for d in GlobalDimension}
    rev = create_requirements_quality_review(
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        reviewer_id="test-reviewer",
        criterion_reviews=c_reviews,
        global_reviews=g_reviews,
    )

    gate = evaluate_requirements_gate(intent, clar, rev)
    assert gate.status == GateStatus.FAIL
    assert GateBlockingReason.CRITERION_REVIEW_UNKNOWN in gate.blocking_reasons


def test_failed_required_dimension():
    intent, clar, _ = _prepare_gate_inputs()
    c_reviews = [
        CriterionReview(
            criterion_id=c.criterion_id,
            dimensions={d: ReviewStatus.PASS for d in CriterionDimension},
        )
        for c in intent.acceptance_criteria
    ]
    g_reviews = {d: ReviewStatus.PASS for d in GlobalDimension}
    g_reviews[GlobalDimension.DESIRED_OUTCOME_CLEAR] = ReviewStatus.FAIL
    rev = create_requirements_quality_review(
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        reviewer_id="test-reviewer",
        criterion_reviews=c_reviews,
        global_reviews=g_reviews,
    )

    gate = evaluate_requirements_gate(intent, clar, rev)
    assert gate.status == GateStatus.FAIL
    assert GateBlockingReason.QUALITY_CHECK_FAILED in gate.blocking_reasons


def test_not_reviewed_required_dimension():
    intent, clar, _ = _prepare_gate_inputs()
    c_reviews = [
        CriterionReview(
            criterion_id=c.criterion_id,
            dimensions={d: ReviewStatus.PASS for d in CriterionDimension},
        )
        for c in intent.acceptance_criteria
    ]
    g_reviews = {d: ReviewStatus.PASS for d in GlobalDimension}
    g_reviews[GlobalDimension.DESIRED_OUTCOME_CLEAR] = ReviewStatus.NOT_REVIEWED
    rev = create_requirements_quality_review(
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        reviewer_id="test-reviewer",
        criterion_reviews=c_reviews,
        global_reviews=g_reviews,
    )

    gate = evaluate_requirements_gate(intent, clar, rev)
    assert gate.status == GateStatus.FAIL
    assert GateBlockingReason.QUALITY_REVIEW_INCOMPLETE in gate.blocking_reasons


# --- CLI Output Safety Tests ---


def test_check_output_alias_identical(tmp_path):
    f1 = tmp_path / "intent.json"
    f1.write_text("{}")

    with pytest.raises(OutputAliasError) as exc:
        check_output_alias(f1, {"--intent": f1}, "test")
    assert "SAFE_WRITE_VIOLATION" in exc.value.message


def test_check_output_alias_relative(tmp_path):
    f1 = tmp_path / "intent.json"
    f1.write_text("{}")
    f2 = tmp_path / "." / "intent.json"

    with pytest.raises(OutputAliasError) as exc:
        check_output_alias(f2, {"--intent": f1}, "test")
    assert "SAFE_WRITE_VIOLATION" in exc.value.message


def test_check_output_alias_symlink(tmp_path):
    f1 = tmp_path / "intent.json"
    f1.write_text("{}")
    f2 = tmp_path / "link.json"
    try:
        f2.symlink_to(f1)
    except OSError:
        pytest.skip("Symlinks not privileged on this OS")

    with pytest.raises(OutputAliasError) as exc:
        check_output_alias(f2, {"--intent": f1}, "test")
    assert "SAFE_WRITE_VIOLATION" in exc.value.message


def test_check_output_alias_hardlink(tmp_path):
    f1 = tmp_path / "intent.json"
    f1.write_text("{}")
    f2 = tmp_path / "hard.json"
    try:
        os.link(f1, f2)
    except OSError:
        pytest.skip("Hardlinks not supported on this OS/FS")

    with pytest.raises(OutputAliasError) as exc:
        check_output_alias(f2, {"--intent": f1}, "test")
    assert "SAFE_WRITE_VIOLATION" in exc.value.message


def test_filesystem_identity_check_error(tmp_path, monkeypatch):
    f1 = tmp_path / "intent.json"
    f1.write_text("{}")
    f2 = tmp_path / "other.json"
    f2.write_text("{}")

    def fake_samefile(a, b):
        raise OSError("Permission denied")

    monkeypatch.setattr(os.path, "samefile", fake_samefile)

    with pytest.raises(OutputAliasError) as exc:
        check_output_alias(f2, {"--intent": f1}, "test")
    assert "SAFE_WRITE_CHECK_FAILED" in exc.value.message


def test_input_hash_unchanged(tmp_path):
    # Just verify that check_output_alias works when they are different
    f1 = tmp_path / "intent.json"
    f1.write_text("{}")
    f2 = tmp_path / "other.json"

    # Should not raise
    check_output_alias(f2, {"--intent": f1}, "test")

    # Now create it and test
    f2.write_text("{}")
    check_output_alias(f2, {"--intent": f1}, "test")


# --- CLI Subprocess Integration Tests ---


def test_cli_clarify_task_subprocess(tmp_path):
    import subprocess
    import sys
    from ai_engineering.task_intent import serialize_intent

    repo_root = Path(__file__).resolve().parents[2]

    # 1. Valid READY intent -> Exit 0
    intent_ready = make_valid_intent(status=IntentStatus.READY)
    intent_file = tmp_path / "intent_ready.json"
    intent_file.write_text(serialize_intent(intent_ready), encoding="utf-8")
    out_file = tmp_path / "clar_out.json"

    res = subprocess.run(
        [sys.executable, "scripts/clarify_task.py", "--intent", str(intent_file), "--output", str(out_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert res.returncode == 0
    assert out_file.exists()

    # 2. NEEDS_CLARIFICATION with blocking unknown -> Exit 1
    intent_needs = make_valid_intent(
        status=IntentStatus.NEEDS_CLARIFICATION,
        unknowns=(IntentUnknown("u1", "desc", blocking=True),),
    )
    intent_needs_file = tmp_path / "intent_needs.json"
    intent_needs_file.write_text(serialize_intent(intent_needs), encoding="utf-8")
    out_needs = tmp_path / "clar_needs_out.json"

    res2 = subprocess.run(
        [sys.executable, "scripts/clarify_task.py", "--intent", str(intent_needs_file), "--output", str(out_needs)],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert res2.returncode == 1

    # 3. Malformed JSON -> Exit 2
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json", encoding="utf-8")
    res3 = subprocess.run(
        [sys.executable, "scripts/clarify_task.py", "--intent", str(bad_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert res3.returncode == 2

    # 4. Output alias -> Exit 2
    res4 = subprocess.run(
        [sys.executable, "scripts/clarify_task.py", "--intent", str(intent_file), "--output", str(intent_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert res4.returncode == 2


def test_cli_requirements_gate_subprocess(tmp_path):
    import subprocess
    import sys
    from ai_engineering.task_intent import serialize_intent

    repo_root = Path(__file__).resolve().parents[2]

    intent = make_valid_intent(status=IntentStatus.READY)
    intent_file = tmp_path / "intent.json"
    intent_file.write_text(serialize_intent(intent), encoding="utf-8")

    clar = generate_clarification_report(intent)
    clar_file = tmp_path / "clar.json"
    clar_file.write_text(serialize_clarification(clar), encoding="utf-8")

    rev = make_valid_review(intent)
    rev_file = tmp_path / "rev.json"
    rev_file.write_text(serialize_review(rev), encoding="utf-8")

    out_file = tmp_path / "gate.json"

    # 1. Valid inputs -> Exit 0
    res = subprocess.run(
        [
            sys.executable,
            "scripts/requirements_gate.py",
            "--intent",
            str(intent_file),
            "--clarification",
            str(clar_file),
            "--review",
            str(rev_file),
            "--output",
            str(out_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert res.returncode == 0
    assert out_file.exists()

    # 2. Gate fail (e.g. DRAFT intent) -> Exit 1
    intent_draft = make_valid_intent(status=IntentStatus.DRAFT)
    draft_file = tmp_path / "draft.json"
    draft_file.write_text(serialize_intent(intent_draft), encoding="utf-8")
    clar_draft = generate_clarification_report(intent_draft)
    clar_draft_file = tmp_path / "clar_draft.json"
    clar_draft_file.write_text(serialize_clarification(clar_draft), encoding="utf-8")
    rev_draft = make_valid_review(intent_draft)
    rev_draft_file = tmp_path / "rev_draft.json"
    rev_draft_file.write_text(serialize_review(rev_draft), encoding="utf-8")

    res2 = subprocess.run(
        [
            sys.executable,
            "scripts/requirements_gate.py",
            "--intent",
            str(draft_file),
            "--clarification",
            str(clar_draft_file),
            "--review",
            str(rev_draft_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert res2.returncode == 1

    # 3. Output alias -> Exit 2
    res3 = subprocess.run(
        [
            sys.executable,
            "scripts/requirements_gate.py",
            "--intent",
            str(intent_file),
            "--clarification",
            str(clar_file),
            "--review",
            str(rev_file),
            "--output",
            str(intent_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    assert res3.returncode == 2
