"""Deterministic contracts for Food Vision Quality V3 human review."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.food_vision_human_review import (
    HumanReviewError,
    MANIFEST_SHA256,
    REVIEW_PACKAGE_SHA256,
    load_and_validate_human_review,
)


@pytest.fixture
def review_paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[2] / "tests/fixtures/food_vision_quality/v3"
    return root / "human-review.json", root / "manifest.json", root / "review-package.json"


def _load(paths: tuple[Path, Path, Path]) -> tuple[dict[str, object], str]:
    receipt, manifest, review_package = paths
    return load_and_validate_human_review(
        receipt,
        manifest_path=manifest,
        review_package_path=review_package,
    )


def _tamper_json(source: Path, destination: Path, mutate) -> Path:
    value = json.loads(source.read_text(encoding="utf-8"))
    mutate(value)
    destination.write_text(json.dumps(value), encoding="utf-8")
    return destination


def test_canonical_human_review_is_complete_sanitized_and_digest_bound(review_paths):
    receipt, digest = _load(review_paths)

    assert receipt["reviewer_role"] == "HUMAN_OPERATOR"
    assert receipt["manifest_sha256"] == MANIFEST_SHA256
    assert receipt["review_package_sha256"] == REVIEW_PACKAGE_SHA256
    assert receipt["overall_verdict"] == "PASS"
    assert len(receipt["fixture_verdicts"]) == 3
    assert len(digest) == 64
    evidence_text = json.dumps(receipt, sort_keys=True)
    for forbidden in ("email", "telegram", "account_id", "reviewer_name", "provider_response"):
        assert forbidden not in evidence_text.lower()


@pytest.mark.parametrize("target", ["manifest", "review_package"])
def test_changed_review_target_digest_is_rejected(review_paths, tmp_path, target):
    receipt, manifest, review_package = review_paths
    source = manifest if target == "manifest" else review_package
    tampered = tmp_path / source.name
    tampered.write_bytes(source.read_bytes() + b"\n")

    with pytest.raises(HumanReviewError, match="HUMAN_REVIEW_TARGET_MISMATCH"):
        load_and_validate_human_review(
            receipt,
            manifest_path=tampered if target == "manifest" else manifest,
            review_package_path=tampered if target == "review_package" else review_package,
        )


def test_wrong_fixture_digest_is_rejected(review_paths, tmp_path):
    receipt, manifest, review_package = review_paths
    tampered = _tamper_json(
        receipt,
        tmp_path / "human-review.json",
        lambda value: value["fixture_hashes"].update({"general-food-recognition": "0" * 64}),
    )

    with pytest.raises(HumanReviewError, match="HUMAN_REVIEW_RECEIPT_INVALID"):
        load_and_validate_human_review(
            tampered,
            manifest_path=manifest,
            review_package_path=review_package,
        )


def test_partial_fixture_review_is_rejected(review_paths, tmp_path):
    receipt, manifest, review_package = review_paths
    tampered = _tamper_json(
        receipt,
        tmp_path / "human-review.json",
        lambda value: value["fixture_verdicts"].pop(),
    )

    with pytest.raises(HumanReviewError, match="HUMAN_REVIEW_RECEIPT_INVALID"):
        load_and_validate_human_review(
            tampered,
            manifest_path=manifest,
            review_package_path=review_package,
        )


def test_overall_pass_cannot_hide_fixture_failure(review_paths, tmp_path):
    receipt, manifest, review_package = review_paths

    def fail_fixture(value):
        value["fixture_verdicts"][1]["verdict"] = "FAIL"

    tampered = _tamper_json(receipt, tmp_path / "human-review.json", fail_fixture)
    with pytest.raises(HumanReviewError, match="HUMAN_REVIEW_RECEIPT_INVALID"):
        load_and_validate_human_review(
            tampered,
            manifest_path=manifest,
            review_package_path=review_package,
        )


def test_fixture_c_ambiguity_decision_is_required(review_paths, tmp_path):
    receipt, manifest, review_package = review_paths

    def claim_exact_subtype(value):
        value["fixture_verdicts"][2]["assertions"][
            "white_exact_subtype_visually_provable"
        ] = True

    tampered = _tamper_json(receipt, tmp_path / "human-review.json", claim_exact_subtype)
    with pytest.raises(HumanReviewError, match="HUMAN_REVIEW_SEMANTIC_MISMATCH"):
        load_and_validate_human_review(
            tampered,
            manifest_path=manifest,
            review_package_path=review_package,
        )


def test_unknown_pii_field_is_rejected(review_paths, tmp_path):
    receipt, manifest, review_package = review_paths
    tampered = _tamper_json(
        receipt,
        tmp_path / "human-review.json",
        lambda value: value.update({"reviewer_name": "synthetic-person"}),
    )

    with pytest.raises(HumanReviewError, match="HUMAN_REVIEW_RECEIPT_INVALID"):
        load_and_validate_human_review(
            tampered,
            manifest_path=manifest,
            review_package_path=review_package,
        )


def test_validation_does_not_mutate_loaded_evidence(review_paths):
    receipt, _manifest, _review_package = review_paths
    before = json.loads(receipt.read_text(encoding="utf-8"))
    expected = copy.deepcopy(before)

    _load(review_paths)

    assert json.loads(receipt.read_text(encoding="utf-8")) == expected
