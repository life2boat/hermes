from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ai_engineering.contracts import Status
from ai_engineering.eval_runner import EvalConfigurationError, run_evals
from scripts import run_agent_behaviour_evals as cli


EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals" / "agent_behaviour"


def test_committed_corpus_and_baseline_pass_deterministically() -> None:
    first = run_evals(EVAL_ROOT)
    second = run_evals(EVAL_ROOT)
    assert first == second
    assert first.status is Status.PASS
    assert first.baseline_status is Status.PASS
    assert first.total_cases == 49
    assert first.critical_total == 43
    assert first.critical_passed == 43
    assert first.critical_failed == 0


def test_smoke_and_category_selection_pass() -> None:
    smoke = run_evals(EVAL_ROOT, smoke=True)
    authority = run_evals(EVAL_ROOT, category="authority")
    adversarial = run_evals(EVAL_ROOT, category="adversarial")
    assert smoke.status is Status.PASS
    assert smoke.total_cases == 9
    assert authority.status is Status.PASS
    assert adversarial.status is Status.PASS


def test_deliberate_oracle_mismatch_proves_fail_path(tmp_path: Path) -> None:
    corpus = tmp_path / "agent_behaviour"
    shutil.copytree(EVAL_ROOT, corpus)
    dataset = corpus / "datasets" / "authority.jsonl"
    lines = dataset.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["expected_evaluation_status"] = "FAIL"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    dataset.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = run_evals(corpus, use_baseline=False)
    assert result.status is Status.FAIL
    assert result.failed == 1


def test_unknown_assertion_proves_blocked_path(tmp_path: Path) -> None:
    corpus = tmp_path / "agent_behaviour"
    shutil.copytree(EVAL_ROOT, corpus)
    dataset = corpus / "datasets" / "provenance.jsonl"
    lines = dataset.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["scenario"]["deterministic_assertions"][0]["kind"] = "unknown_kind"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    dataset.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = run_evals(corpus, use_baseline=False)
    assert result.status is Status.BLOCKED
    assert result.blocked == 1


def test_invalid_manifest_is_configuration_blocked(tmp_path: Path) -> None:
    corpus = tmp_path / "agent_behaviour"
    shutil.copytree(EVAL_ROOT, corpus)
    manifest = corpus / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvalConfigurationError) as caught:
        run_evals(corpus)
    assert caught.value.code == "EVAL_CORPUS_INVALID"


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [(Status.PASS, 0), (Status.FAIL, 1), (Status.BLOCKED, 2)],
)
def test_cli_exit_codes(monkeypatch, capsys, status: Status, expected_exit: int) -> None:
    result = run_evals(EVAL_ROOT, smoke=True)
    monkeypatch.setattr(cli, "run_evals", lambda *_args, **_kwargs: replace(result, status=status))
    assert cli.run(["--smoke"]) == expected_exit
    assert f'"status":"{status.value}"' in capsys.readouterr().out
