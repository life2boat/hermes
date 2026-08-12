from __future__ import annotations

import json
from pathlib import Path

from ai_engineering.contracts import Status
from ai_engineering.procedure_maturity import (
    PROCEDURE_MATURITY_POLICY_VERSION,
    ProcedureMaturityError,
    evaluate_procedure_maturity,
)
from scripts.check_procedure_maturity import run


def _evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy_version": PROCEDURE_MATURITY_POLICY_VERSION,
        "procedure_id": "failure-eval-loop",
        "procedure_version": "v1",
        "current_stage": "maturity_review",
        "source_procedure_ref": "docs/FAILURE_CAPTURE_LOOP.md",
        "eval_dataset_ref": "evals/agent_behaviour",
        "stable_intent": "PASS",
        "stable_sequence": "PASS",
        "known_failure_modes": "PASS",
        "adequate_regression_corpus": "PASS",
        "side_effects_understood": "PASS",
        "authority_boundary_known": "PASS",
        "remaining_agent_judgement": [
            {"decision_id": "operator-review", "agent_controlled": True}
        ],
        "required_invariants": ["INV-AI-V2-005"],
        "allowed_effect_classes": ["REPOSITORY_WRITE"],
    }


def test_all_required_maturity_criteria_pass_yields_graph_candidate_only() -> None:
    receipt = evaluate_procedure_maturity(_evidence())

    assert receipt.graph_candidate_eligible is Status.PASS
    assert receipt.authority_expansion_authorized is False
    assert receipt.policy_version == PROCEDURE_MATURITY_POLICY_VERSION


def test_missing_maturity_evidence_is_blocked_not_pass() -> None:
    evidence = _evidence()
    evidence["known_failure_modes"] = "UNKNOWN"

    receipt = evaluate_procedure_maturity(evidence)

    assert receipt.graph_candidate_eligible is Status.BLOCKED
    assert "PROCEDURE_MATURITY_KNOWN_FAILURE_MODES_NOT_PROVEN" in receipt.reason_codes


def test_not_performed_authority_boundary_is_blocked_not_pass() -> None:
    evidence = _evidence()
    evidence["authority_boundary_known"] = "NOT_PERFORMED"

    receipt = evaluate_procedure_maturity(evidence)

    assert receipt.graph_candidate_eligible is Status.BLOCKED


def test_failed_side_effect_contract_fails_maturity() -> None:
    evidence = _evidence()
    evidence["side_effects_understood"] = "FAIL"

    receipt = evaluate_procedure_maturity(evidence)

    assert receipt.graph_candidate_eligible is Status.FAIL
    assert "PROCEDURE_MATURITY_SIDE_EFFECTS_UNDERSTOOD_FAIL" in receipt.reason_codes


def test_model_capability_cannot_be_substituted_for_maturity_evidence() -> None:
    evidence = {**_evidence(), "model_capability": "stronger-model"}

    try:
        evaluate_procedure_maturity(evidence)
    except ProcedureMaturityError as exc:
        assert exc.code == "PROCEDURE_MATURITY_UNEXPECTED_FIELD"
    else:
        raise AssertionError("model capability must not become maturity evidence")


def test_non_agent_controlled_judgement_fails_graph_promotion() -> None:
    evidence = _evidence()
    evidence["remaining_agent_judgement"] = [
        {"decision_id": "operator-review", "agent_controlled": False}
    ]

    receipt = evaluate_procedure_maturity(evidence)

    assert receipt.graph_candidate_eligible is Status.FAIL
    assert receipt.authority_expansion_authorized is False


def test_raw_evidence_is_blocked_instead_of_becoming_an_internal_error() -> None:
    evidence = {**_evidence(), "raw_provider_response": "forbidden"}

    try:
        evaluate_procedure_maturity(evidence)
    except ProcedureMaturityError as exc:
        assert exc.code == "PROCEDURE_MATURITY_EVIDENCE_NOT_SANITIZED"
    else:
        raise AssertionError("raw evidence must remain blocked")


def test_cli_reports_pass_fail_and_blocked(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "maturity.json"
    input_path.write_text(json.dumps(_evidence()), encoding="utf-8")

    assert run(["--input", str(input_path)]) == 0
    assert json.loads(capsys.readouterr().out)["graph_candidate_eligible"] == "PASS"

    failed = _evidence()
    failed["side_effects_understood"] = "FAIL"
    input_path.write_text(json.dumps(failed), encoding="utf-8")
    assert run(["--input", str(input_path)]) == 1
    assert json.loads(capsys.readouterr().out)["graph_candidate_eligible"] == "FAIL"

    blocked = _evidence()
    blocked["authority_boundary_known"] = "UNKNOWN"
    input_path.write_text(json.dumps(blocked), encoding="utf-8")
    assert run(["--input", str(input_path)]) == 2
    assert json.loads(capsys.readouterr().out)["graph_candidate_eligible"] == "BLOCKED"
