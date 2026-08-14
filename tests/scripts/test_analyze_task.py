"""Tests for scripts/analyze_task.py CLI.

Covers:
- Exit code 0 (no errors)
- Exit code 1 (errors found)
- Exit code 2 (invalid input, missing file)
- --output flag
- --expected-sha flag (SOURCE_IDENTITY_MISMATCH)
- Read-only: input files not mutated
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.analyze_task import main


def _write_intent(path: Path, **overrides) -> Path:
    data = {
        "schema_version": 1,
        "task_id": "CLI-TEST",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Test outcome",
        "source_repository": "life2boat/hermes",
        "source_main_ref": "main",
        "source_base_sha": "b" * 40,
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
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_lineage(path: Path, nodes=None, edges=None) -> Path:
    data = {
        "schema_version": 1,
        "nodes": nodes or [],
        "edges": edges or [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestCliExitCodes:
    def test_exit_0_no_errors(self, tmp_path: Path) -> None:
        intent = _write_intent(tmp_path / "intent.json")
        lineage = _write_lineage(tmp_path / "lineage.json")
        result = main(["--intent", str(intent), "--lineage", str(lineage)])
        assert result == 0

    def test_exit_1_error_findings(self, tmp_path: Path) -> None:
        intent = _write_intent(
            tmp_path / "intent.json",
            acceptance_criteria=[{"criterion_id": "AC-1", "statement": "Must pass"}],
        )
        lineage = _write_lineage(tmp_path / "lineage.json")
        result = main(["--intent", str(intent), "--lineage", str(lineage)])
        assert result == 1

    def test_exit_2_invalid_intent(self, tmp_path: Path, capsys) -> None:
        intent = tmp_path / "intent.json"
        intent.write_text("{bad json", encoding="utf-8")
        lineage = _write_lineage(tmp_path / "lineage.json")
        result = main(["--intent", str(intent), "--lineage", str(lineage)])
        assert result == 2
        captured = capsys.readouterr()
        assert "analyze_task:" in captured.err

    def test_exit_2_missing_file(self, tmp_path: Path, capsys) -> None:
        intent = tmp_path / "nonexistent.json"
        lineage = _write_lineage(tmp_path / "lineage.json")
        result = main(["--intent", str(intent), "--lineage", str(lineage)])
        assert result == 2
        captured = capsys.readouterr()
        assert "analyze_task:" in captured.err

    def test_exit_2_invalid_expected_sha(self, tmp_path: Path, capsys) -> None:
        intent = _write_intent(tmp_path / "intent.json")
        lineage = _write_lineage(tmp_path / "lineage.json")
        result = main([
            "--intent", str(intent),
            "--lineage", str(lineage),
            "--expected-sha", "not-a-sha",
        ])
        assert result == 2
        captured = capsys.readouterr()
        assert "EXPECTED_SHA_INVALID" in captured.err


class TestCliExpectedSha:
    def test_matching_expected_sha_exit_0(self, tmp_path: Path) -> None:
        sha = "b" * 40
        intent = _write_intent(tmp_path / "intent.json", source_base_sha=sha)
        lineage = _write_lineage(tmp_path / "lineage.json")
        result = main([
            "--intent", str(intent),
            "--lineage", str(lineage),
            "--expected-sha", sha,
        ])
        assert result == 0

    def test_mismatched_expected_sha_exit_1(self, tmp_path: Path) -> None:
        intent = _write_intent(tmp_path / "intent.json", source_base_sha="a" * 40)
        lineage = _write_lineage(tmp_path / "lineage.json")
        result = main([
            "--intent", str(intent),
            "--lineage", str(lineage),
            "--expected-sha", "c" * 40,
        ])
        assert result == 1

    def test_no_expected_sha_no_mismatch(self, tmp_path: Path) -> None:
        intent = _write_intent(tmp_path / "intent.json", source_base_sha="a" * 40)
        lineage = _write_lineage(tmp_path / "lineage.json")
        # No --expected-sha: SOURCE_IDENTITY_MISMATCH not checked
        result = main(["--intent", str(intent), "--lineage", str(lineage)])
        assert result == 0


class TestCliOutput:
    def test_output_flag_writes_json(self, tmp_path: Path) -> None:
        intent = _write_intent(tmp_path / "intent.json")
        lineage = _write_lineage(tmp_path / "lineage.json")
        report_path = tmp_path / "report.json"
        result = main([
            "--intent", str(intent),
            "--lineage", str(lineage),
            "--output", str(report_path),
        ])
        assert result == 0
        assert report_path.exists()
        parsed = json.loads(report_path.read_text(encoding="utf-8"))
        assert parsed["schema_version"] == 1
        assert "analysis_id" in parsed

    def test_output_is_deterministic(self, tmp_path: Path) -> None:
        intent = _write_intent(
            tmp_path / "intent.json",
            acceptance_criteria=[{"criterion_id": "AC-1", "statement": "Must pass"}],
        )
        lineage = _write_lineage(tmp_path / "lineage.json")
        out1 = tmp_path / "report1.json"
        out2 = tmp_path / "report2.json"
        main(["--intent", str(intent), "--lineage", str(lineage), "--output", str(out1)])
        main(["--intent", str(intent), "--lineage", str(lineage), "--output", str(out2)])
        assert out1.read_bytes() == out2.read_bytes()


class TestCliReadOnly:
    def test_input_files_not_mutated(self, tmp_path: Path) -> None:
        intent = _write_intent(tmp_path / "intent.json")
        lineage = _write_lineage(tmp_path / "lineage.json")
        intent_hash_before = hashlib.sha256(intent.read_bytes()).hexdigest()
        lineage_hash_before = hashlib.sha256(lineage.read_bytes()).hexdigest()
        main(["--intent", str(intent), "--lineage", str(lineage)])
        intent_hash_after = hashlib.sha256(intent.read_bytes()).hexdigest()
        lineage_hash_after = hashlib.sha256(lineage.read_bytes()).hexdigest()
        assert intent_hash_before == intent_hash_after
        assert lineage_hash_before == lineage_hash_after
