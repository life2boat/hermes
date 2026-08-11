from __future__ import annotations

import json
import shutil
from pathlib import Path

from ai_engineering.eval_runner import corpus_review_is_applicable, run_evals
from ai_engineering.scenario import load_trace_fixture
from ai_engineering.trace import trace_digest


EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals" / "agent_behaviour"


def _copy_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "agent_behaviour"
    shutil.copytree(EVAL_ROOT, corpus)
    return corpus


def _dataset_records(corpus: Path, name: str) -> tuple[Path, list[dict[str, object]]]:
    path = corpus / "datasets" / f"{name}.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path, records


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    lines = [json.dumps(record, sort_keys=True, separators=(",", ":")) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _digest(corpus: Path) -> str:
    return run_evals(corpus, use_baseline=False).corpus_digest


def test_corpus_digest_stable_across_candidate_to_golden_promotion(
    tmp_path: Path,
) -> None:
    corpus = _copy_corpus(tmp_path)
    candidate_digest = _digest(corpus)
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corpus_status"] = "GOLDEN"
    manifest["baseline"] = "baselines/promotion-metadata.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    assert _digest(corpus) == candidate_digest


def test_expected_outcome_change_invalidates_corpus_digest(tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    original_digest = _digest(corpus)
    path, records = _dataset_records(corpus, "authority")
    records[0]["expected_evaluation_status"] = "FAIL"
    _write_records(path, records)

    assert _digest(corpus) != original_digest


def test_case_criticality_change_invalidates_corpus_digest(tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    original_digest = _digest(corpus)
    path, records = _dataset_records(corpus, "failure_handling")
    assert records[0]["critical"] is False
    records[0]["critical"] = True
    _write_records(path, records)

    assert _digest(corpus) != original_digest


def test_dataset_membership_change_invalidates_corpus_digest(tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    original_digest = _digest(corpus)
    path, records = _dataset_records(corpus, "failure_handling")
    removed = records.pop()
    trace_path = corpus / str(removed["trace_reference"])
    trace_path.unlink()
    _write_records(path, records)

    assert _digest(corpus) != original_digest


def test_trace_fixture_change_invalidates_corpus_digest(tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    original_digest = _digest(corpus)
    path, records = _dataset_records(corpus, "provenance")
    trace_reference = str(records[0]["trace_reference"])
    trace_path = corpus / trace_reference
    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    trace_payload["repository"]["branch"] = "codex/content-change"
    trace_path.write_text(
        json.dumps(trace_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    records[0]["expected_trace_digest"] = trace_digest(
        load_trace_fixture(corpus, trace_reference)
    )
    _write_records(path, records)

    assert _digest(corpus) != original_digest


def test_required_behaviour_dimension_change_invalidates_digest(tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    original_digest = _digest(corpus)
    path, records = _dataset_records(corpus, "provenance")
    scenario = records[0]["scenario"]
    assert isinstance(scenario, dict)
    dimensions = scenario["required_behaviour_dimensions"]
    assert isinstance(dimensions, list)
    dimensions.append("evidence_sanitization_contract")
    _write_records(path, records)

    assert _digest(corpus) != original_digest


def test_reviewed_digest_controls_approval_applicability() -> None:
    reviewed = "1" * 64
    assert corpus_review_is_applicable(reviewed, reviewed)
    assert not corpus_review_is_applicable(reviewed, "2" * 64)
    assert not corpus_review_is_applicable("not-a-digest", reviewed)
