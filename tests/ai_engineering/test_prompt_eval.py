from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ai_engineering.prompt_system import run_prompt_evals
from scripts.run_prompt_quality_evals import run


EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals" / "prompt_quality"


def test_candidate_prompt_corpus_passes_all_required_categories() -> None:
    result = run_prompt_evals(EVAL_ROOT)
    assert result.status == "PASS"
    assert result.total == 8
    assert result.passed == 8
    assert result.failed == 0
    assert result.blocked == 0
    assert len(result.corpus_digest) == 64
    assert {item.case_id for item in result.cases} == {
        "repository-fix",
        "ambiguous-request",
        "missing-evidence",
        "structured-extraction",
        "untrusted-injection",
        "multi-step",
        "failure-handling",
        "historical-regression",
    }


def test_prompt_corpus_digest_is_deterministic() -> None:
    assert (
        run_prompt_evals(EVAL_ROOT).corpus_digest
        == run_prompt_evals(EVAL_ROOT).corpus_digest
    )


def test_cli_writes_new_sanitized_receipt(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    assert (
        run(["--eval-root", str(EVAL_ROOT), "--json-out", str(output.resolve())]) == 0
    )
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["status"] == "PASS"
    assert receipt["total"] == 8
    assert "generated_at" not in receipt
    assert (
        run(["--eval-root", str(EVAL_ROOT), "--json-out", str(output.resolve())]) == 2
    )


def test_prompt_corpus_review_state_and_ci_gate_are_explicit() -> None:
    review = (EVAL_ROOT / "CORPUS_REVIEW.md").read_text(encoding="utf-8")
    result = run_prompt_evals(EVAL_ROOT)
    assert "CORPUS_STATUS=CANDIDATE" in review
    assert "HUMAN_REVIEW=NOT_PERFORMED" in review
    assert f"CORPUS_DIGEST={result.corpus_digest}" in review

    root = EVAL_ROOT.parents[1]
    workflow = (root / ".github" / "workflows" / "agent-release-gate.yml").read_text(
        encoding="utf-8"
    )
    assert "Evaluate exact-head prompt quality corpus" in workflow
    assert "scripts/run_prompt_quality_evals.py" in workflow
    assert "prompt-quality-report.json" in workflow


def test_standalone_cli_resolves_repository_package() -> None:
    root = EVAL_ROOT.parents[1]
    completed = subprocess.run(
        [sys.executable, "scripts/run_prompt_quality_evals.py"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["status"] == "PASS"
    assert completed.stderr == ""
