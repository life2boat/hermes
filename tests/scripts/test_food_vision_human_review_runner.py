"""Harness preflight tests for canonical V3 human-review evidence."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import run_food_vision_quality as harness


def _manifest() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "tests/fixtures/food_vision_quality/v3/manifest.json"
    )


def test_v3_runner_validates_default_review_before_provider_boundary(
    tmp_path, monkeypatch
):
    output = tmp_path / "quality-receipt.json"
    monkeypatch.setattr(
        harness,
        "_provider_dependencies",
        lambda: (_ for _ in ()).throw(
            AssertionError("dry run must not load provider")
        ),
    )

    assert harness.run(
        [
            "--provider",
            "alibaba",
            "--model",
            "not-selected",
            "--fixture-manifest",
            str(_manifest()),
            "--receipt-out",
            str(output),
        ]
    ) == 0
    durable = json.loads(output.read_text(encoding="utf-8"))
    assert durable["status"] == "DRY_RUN"
    assert durable["requests_used"] == 0


def test_v3_runner_rejects_missing_explicit_review_before_provider_boundary(
    tmp_path, monkeypatch
):
    output = tmp_path / "quality-receipt.json"
    monkeypatch.setattr(
        harness,
        "_provider_dependencies",
        lambda: (_ for _ in ()).throw(
            AssertionError("review failure must precede provider")
        ),
    )

    assert harness.run(
        [
            "--provider",
            "alibaba",
            "--model",
            "not-selected",
            "--fixture-manifest",
            str(_manifest()),
            "--human-review-receipt",
            str(tmp_path / "missing.json"),
            "--receipt-out",
            str(output),
        ]
    ) == 2
    durable = json.loads(output.read_text(encoding="utf-8"))
    assert durable["error_class"] == "HUMAN_REVIEW_EVIDENCE_UNAVAILABLE"
    assert durable["requests_used"] == 0
