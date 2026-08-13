"""Closed validation for Food Vision Quality V3 human-review evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
FIXTURE_SET_VERSION = "food_vision_quality_v3"
REVIEWER_ROLE = "HUMAN_OPERATOR"
REVIEW_DATE = "2026-08-13"
REVIEWED_SOURCE_SHA = "309ba39bd5e9425cfbf0ea3cb5d2199d0ca1ae22"
MANIFEST_SHA256 = "543948ff57e27327ec1233a282a62fb230d39b12c02cde0e63e96955500e4202"
REVIEW_PACKAGE_SHA256 = "9d67211c005ad5b7758e67ce6f58c8c5d5a29d6739039f463dc2cb2d9c7762a1"
MAX_EVIDENCE_BYTES = 64 * 1024

FIXTURE_HASHES = {
    "general-food-recognition": "a4e259a7ec6a37b6b26dedd1671e9fddfca6528cf417642b504134121cbdb412",
    "mixed-food-with-distractor": "44d4b162fe04a9255e83d0d310ee4315afc2cf794c13fdc045b4ff6b32ed7f9a",
    "separate-condiments": "94ba73a38e715f300331d7b9616fb3fd9f31bee12b6dca2e267f882097176ccd",
}

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "fixture_set_version",
        "review_date",
        "reviewer_role",
        "canonical_source_sha",
        "manifest_sha256",
        "review_package_sha256",
        "fixture_hashes",
        "fixture_verdicts",
        "overall_verdict",
    }
)
_VERDICT_FIELDS = frozenset({"fixture_id", "verdict", "assertions"})
_EXPECTED_ASSERTIONS: dict[str, dict[str, bool | str]] = {
    "general-food-recognition": {
        "visible_apple": True,
        "visible_banana": True,
        "visible_bread": True,
        "other_material_food": "none",
    },
    "mixed-food-with-distractor": {
        "visible_carrot": True,
        "visible_cucumber": True,
        "visible_cheese": True,
        "empty_cup_is_non_food_distractor": True,
        "other_material_food": "none",
    },
    "separate-condiments": {
        "visible_red_ketchup_or_tomato_sauce": True,
        "visible_yellow_sauce": True,
        "visible_white_sauce_generic": True,
        "white_exact_subtype_visually_provable": False,
        "white_could_reasonably_be_mayonnaise": True,
        "white_could_reasonably_be_yogurt_or_similar": True,
        "generic_sauce_plus_clarification_correct": True,
    },
}


class HumanReviewError(ValueError):
    """Fixed-class review preflight failure safe for a durable receipt."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")


def _read_bytes(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HumanReviewError("HUMAN_REVIEW_EVIDENCE_UNAVAILABLE") from exc
    if not data or len(data) > MAX_EVIDENCE_BYTES or b"\x00" in data:
        raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
    return data


def _canonical_git_text_bytes(path: Path) -> bytes:
    data = _read_bytes(path)
    normalized = data.replace(b"\r\n", b"\n")
    if b"\r" in normalized:
        raise HumanReviewError("HUMAN_REVIEW_TARGET_MISMATCH")
    return normalized


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path) -> tuple[dict[str, Any], bytes]:
    data = _canonical_git_text_bytes(path)
    try:
        value = json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID") from exc
    if not isinstance(value, dict):
        raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
    return value, data


def _validate_target_files(manifest_path: Path, review_package_path: Path) -> None:
    manifest, manifest_bytes = _load_json(manifest_path)
    review_package, review_bytes = _load_json(review_package_path)
    if _sha256(manifest_bytes) != MANIFEST_SHA256 or _sha256(review_bytes) != REVIEW_PACKAGE_SHA256:
        raise HumanReviewError("HUMAN_REVIEW_TARGET_MISMATCH")
    if (
        manifest.get("fixture_set_version") != FIXTURE_SET_VERSION
        or manifest.get("lifecycle") != "CANDIDATE"
        or manifest.get("human_visual_review_required") is not True
        or review_package.get("fixture_set_version") != FIXTURE_SET_VERSION
        or review_package.get("lifecycle") != "CANDIDATE"
        or review_package.get("human_visual_review") != "NOT_PERFORMED"
        or review_package.get("manifest_sha256") != MANIFEST_SHA256
    ):
        raise HumanReviewError("HUMAN_REVIEW_SEMANTIC_MISMATCH")
    manifest_fixtures = manifest.get("fixtures")
    review_fixtures = review_package.get("fixtures")
    if not isinstance(manifest_fixtures, list) or not isinstance(review_fixtures, list):
        raise HumanReviewError("HUMAN_REVIEW_SEMANTIC_MISMATCH")
    manifest_hashes = {item.get("id"): item.get("image_sha256") for item in manifest_fixtures if isinstance(item, dict)}
    review_hashes = {item.get("id"): item.get("image_sha256") for item in review_fixtures if isinstance(item, dict)}
    if manifest_hashes != FIXTURE_HASHES or review_hashes != FIXTURE_HASHES:
        raise HumanReviewError("HUMAN_REVIEW_TARGET_MISMATCH")
    condiment = next((item for item in manifest_fixtures if isinstance(item, dict) and item.get("id") == "separate-condiments"), None)
    review_condiment = next((item for item in review_fixtures if isinstance(item, dict) and item.get("id") == "separate-condiments"), None)
    expected_ambiguity = [{
        "generic_label": "sauce",
        "plausible_specific_labels": ["mayonnaise", "sour_cream", "yogurt"],
        "exact_subtype_supported": False,
        "clarification_required": True,
    }]
    if (
        not isinstance(condiment, dict)
        or condiment.get("expected_sauce_items") != ["ketchup", "yellow_sauce", "sauce"]
        or condiment.get("expected_needs_clarification") is not True
        or condiment.get("ambiguity_items") != expected_ambiguity
        or not isinstance(review_condiment, dict)
        or review_condiment.get("visually_supported_labels") != ["ketchup", "yellow_sauce", "sauce"]
        or review_condiment.get("exact_subtype_supported") is not False
        or review_condiment.get("clarification_expected") is not True
    ):
        raise HumanReviewError("HUMAN_REVIEW_SEMANTIC_MISMATCH")


def load_and_validate_human_review(
    receipt_path: Path,
    *,
    manifest_path: Path,
    review_package_path: Path,
) -> tuple[dict[str, Any], str]:
    """Validate exact reviewed identities and return sanitized evidence + digest."""

    receipt, receipt_bytes = _load_json(receipt_path)
    if set(receipt) != _TOP_FIELDS:
        raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("fixture_set_version") != FIXTURE_SET_VERSION
        or receipt.get("review_date") != REVIEW_DATE
        or receipt.get("reviewer_role") != REVIEWER_ROLE
        or receipt.get("canonical_source_sha") != REVIEWED_SOURCE_SHA
        or not re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("canonical_source_sha", "")))
        or receipt.get("manifest_sha256") != MANIFEST_SHA256
        or receipt.get("review_package_sha256") != REVIEW_PACKAGE_SHA256
        or receipt.get("fixture_hashes") != FIXTURE_HASHES
        or receipt.get("overall_verdict") != "PASS"
    ):
        raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
    verdicts = receipt.get("fixture_verdicts")
    if not isinstance(verdicts, list) or len(verdicts) != len(FIXTURE_HASHES):
        raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
    seen: set[str] = set()
    for verdict in verdicts:
        if not isinstance(verdict, dict) or set(verdict) != _VERDICT_FIELDS:
            raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
        fixture_id = verdict.get("fixture_id")
        if fixture_id in seen or fixture_id not in FIXTURE_HASHES or verdict.get("verdict") != "PASS":
            raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
        if verdict.get("assertions") != _EXPECTED_ASSERTIONS[fixture_id]:
            raise HumanReviewError("HUMAN_REVIEW_SEMANTIC_MISMATCH")
        seen.add(fixture_id)
    if seen != set(FIXTURE_HASHES):
        raise HumanReviewError("HUMAN_REVIEW_RECEIPT_INVALID")
    _validate_target_files(manifest_path, review_package_path)
    return receipt, _sha256(receipt_bytes)
