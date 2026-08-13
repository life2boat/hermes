#!/usr/bin/env python3
"""Opt-in, non-production DashScope food-Vision quality harness.

Normal test and CI execution never invokes a provider: real execution requires
``--execute-provider`` and a process-only ``DASHSCOPE_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))





SUPPORTED_FIXTURE_SET_VERSIONS = frozenset({
    "food_vision_quality_v1",
    "food_vision_quality_v2",
})
# Kept as the default only for existing callers that construct an in-memory v1
# manifest. Receipt provenance always comes from the verified manifest.
FIXTURE_SET_VERSION = "food_vision_quality_v1"
RECEIPT_SCHEMA_VERSION = 3
REQUIRED_PROVIDER = "alibaba"
PROVIDER_TIMEOUT_SECONDS = 60


class HarnessInputError(ValueError):
    """A fixed-class input failure safe to serialize in a receipt."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    try:
        manifest_bytes = path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessInputError("FIXTURE_MANIFEST_INVALID") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("fixture_set_version") not in SUPPORTED_FIXTURE_SET_VERSIONS
    ):
        raise HarnessInputError("FIXTURE_MANIFEST_INVALID")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) != 3:
        raise HarnessInputError("FIXTURE_MANIFEST_INVALID")
    ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise HarnessInputError("FIXTURE_MANIFEST_INVALID")
        fixture_id = fixture.get("id")
        relative_path = fixture.get("image_path")
        image_hash = fixture.get("image_sha256")
        if (
            not isinstance(fixture_id, str)
            or not fixture_id
            or fixture_id in ids
            or not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
            or not isinstance(image_hash, str)
            or len(image_hash) != 64
            or any(character not in "0123456789abcdef" for character in image_hash)
            or not isinstance(fixture.get("expected_food_items"), list)
            or not isinstance(fixture.get("expected_sauce_items"), list)
            or fixture.get("expected_invalid") is not False
        ):
            raise HarnessInputError("FIXTURE_MANIFEST_INVALID")
        ids.add(fixture_id)
        validated.append(fixture)
    return manifest, validated, _sha256_bytes(manifest_bytes)


def _read_verified_fixture(manifest_path: Path, fixture: dict[str, Any]) -> bytes:
    try:
        image_path = (manifest_path.parent / fixture["image_path"]).resolve(strict=True)
        image_path.relative_to(manifest_path.parent.resolve())
        image_bytes = image_path.read_bytes()
    except (OSError, ValueError) as exc:
        raise HarnessInputError("FIXTURE_HASH_MISMATCH") from exc
    if _sha256_bytes(image_bytes) != fixture["image_sha256"]:
        raise HarnessInputError("FIXTURE_HASH_MISMATCH")
    return image_bytes


def _safe_fixture_entry(
    fixture: dict[str, Any],
    *,
    status: str,
    schema_valid: bool | None = None,
    prediction_count: int = 0,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "fixture_id": fixture["id"],
        "image_sha256": fixture["image_sha256"],
        "request_status_class": status,
        "schema_valid": schema_valid,
        "normalized_prediction_count": prediction_count,
        "expected_count": len(fixture["expected_food_items"]) + len(fixture["expected_sauce_items"]),
        "diagnostics": diagnostics or {
            "schema_error_code": "NOT_EVALUATED",
            "schema_error_summary": "NOT_EVALUATED",
            "validated_prediction_labels": [],
            "canonical_predicted_components": [],
            "matched_expected_components": [],
            "missed_expected_components": [],
            "unexpected_predicted_components": [],
            "canonical_predicted_sauces": [],
            "matched_expected_sauces": [],
            "missed_expected_sauces": [],
            "diagnostic_redaction_count": 0,
        },
    }


def _receipt_base(
    args: argparse.Namespace,
    *,
    fixture_set_version: str | None,
    manifest_sha256: str | None,
    fixture_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "fixture_set_version": fixture_set_version,
        "manifest_sha256": manifest_sha256,
        "provider": args.provider,
        "model": args.model,
        "fixture_count": fixture_count,
        "request_budget": fixture_count,
        "requests_used": 0,
        "retries_used": 0,
        "credential_recovery_retries": 0,
        "cross_provider_fallbacks": 0,
        "fixtures": [],
        "aggregate": {
            "precision": None,
            "recall": None,
            "sauce_recall": None,
            "unsafe_aggregate_count": None,
            "invalid_aggregate_count": None,
        },
        "quality_gate": "NOT_RUN",
        "model_access": "NOT_RUN",
        "vision_capability": "NOT_RUN",
        "hermes_schema_compatibility": "NOT_RUN",
        "started_at": _utc_now(),
        "completed_at": None,
    }


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt["completed_at"] = _utc_now()
    encoded = json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
    except FileExistsError as exc:
        raise HarnessInputError("RECEIPT_PATH_EXISTS") from exc


def _provider_dependencies() -> tuple[Any, Any, Any, Any]:
    """Load the application provider boundary only for acknowledged execution."""

    from agent.auxiliary_client import (
        ExternalRequestTelemetry,
        VISION_SINGLE_REQUEST_LLM_CALL_POLICY,
        extract_content_or_reasoning,
        safe_async_call_llm,
    )

    return (
        ExternalRequestTelemetry,
        VISION_SINGLE_REQUEST_LLM_CALL_POLICY,
        extract_content_or_reasoning,
        safe_async_call_llm,
    )


def _quality_dependencies() -> tuple[Any, Any, Any, str]:
    """Load application scoring only for acknowledged provider execution."""

    from gateway.food_vision_quality import (
        aggregate_food_vision_scores,
        quality_gate_passes,
        score_food_vision_payload,
    )
    from gateway.healbite_nutrition_diary import _VISION_PROMPT

    return score_food_vision_payload, aggregate_food_vision_scores, quality_gate_passes, _VISION_PROMPT


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    parser.add_argument("--execute-provider", action="store_true")
    return parser.parse_args(argv)


async def _execute(args: argparse.Namespace, fixtures: list[dict[str, Any]], receipt: dict[str, Any], manifest_path: Path) -> int:
    credential_present = bool(os.environ.get("DASHSCOPE_API_KEY", "").strip())
    if not credential_present:
        receipt.update({
            "status": "BLOCKED",
            "error_class": "CREDENTIAL_MISSING",
            "credential_present": False,
        })
        return 2
    receipt["credential_present"] = True
    try:
        telemetry_type, call_policy, content_extractor, provider_call = _provider_dependencies()
        score_payload, aggregate_scores, gate_passes, vision_prompt = _quality_dependencies()
    except Exception:
        receipt.update({
            "status": "BLOCKED",
            "error_class": "HARNESS_RUNTIME_UNAVAILABLE",
        })
        return 2
    scores: list[dict[str, float | int | bool]] = []
    schema_invalid = False
    for fixture in fixtures:
        image_bytes = _read_verified_fixture(manifest_path, fixture)
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        telemetry = telemetry_type()
        try:
            response = await provider_call(
                task="vision",
                provider=REQUIRED_PROVIDER,
                model=args.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
                temperature=0,
                max_tokens=2000,
                timeout=PROVIDER_TIMEOUT_SECONDS,
                call_policy=call_policy,
                request_telemetry=telemetry,
            )
            payload_text = content_extractor(response)
        except Exception:
            receipt["requests_used"] += telemetry.external_request_attempts
            receipt["retries_used"] += int(bool(telemetry.retry_performed))
            receipt["cross_provider_fallbacks"] += int(bool(telemetry.fallback_performed))
            receipt["fixtures"].append(_safe_fixture_entry(fixture, status="PROVIDER_REQUEST_FAILED"))
            receipt.update({
                "status": "FAIL",
                "error_class": "PROVIDER_REQUEST_FAILED",
                "model_access": "FAIL",
                "vision_capability": "FAIL",
                "hermes_schema_compatibility": "NOT_RUN",
            })
            return 1
        receipt["requests_used"] += telemetry.external_request_attempts
        if telemetry.external_request_attempts != 1 or telemetry.retry_performed or telemetry.fallback_performed:
            receipt["fixtures"].append(_safe_fixture_entry(fixture, status="REQUEST_POLICY_VIOLATION"))
            receipt.update({
                "status": "FAIL",
                "error_class": "REQUEST_POLICY_VIOLATION",
                "model_access": "FAIL",
                "vision_capability": "FAIL",
                "hermes_schema_compatibility": "NOT_RUN",
            })
            return 1
        score = score_payload(
            payload_text,
            expected_food_items=fixture["expected_food_items"],
            expected_sauce_items=fixture["expected_sauce_items"],
            expected_needs_clarification=bool(fixture.get("expected_needs_clarification", True)),
            allowed_aliases=fixture.get("allowed_aliases"),
        )
        scores.append(score)
        receipt["fixtures"].append(_safe_fixture_entry(
            fixture,
            status="PASS" if score["schema_valid"] else "SCHEMA_INVALID",
            schema_valid=bool(score["schema_valid"]),
            prediction_count=int(score["normalized_prediction_count"]),
            diagnostics=score.get("diagnostics"),
        ))
        if not score["schema_valid"]:
            schema_invalid = True

    aggregate = aggregate_scores(scores)
    receipt["aggregate"] = aggregate
    receipt["model_access"] = "PASS"
    receipt["vision_capability"] = "PASS"
    receipt["hermes_schema_compatibility"] = "FAIL" if schema_invalid else "PASS"
    receipt["quality_gate"] = "PASS" if (not schema_invalid and gate_passes(aggregate)) else "FAIL"
    receipt["status"] = "PASS" if receipt["quality_gate"] == "PASS" else "FAIL"
    receipt["error_class"] = (
        "NONE" if receipt["status"] == "PASS"
        else "SCHEMA_INVALID" if schema_invalid
        else "QUALITY_THRESHOLD_NOT_MET"
    )
    return 0 if receipt["status"] == "PASS" else 1


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    manifest_sha256: str | None = None
    fixture_set_version: str | None = None
    fixtures: list[dict[str, Any]] = []
    try:
        if args.provider != REQUIRED_PROVIDER:
            raise HarnessInputError("UNSUPPORTED_PROVIDER")
        if not args.model.strip():
            raise HarnessInputError("MODEL_REQUIRED")
        manifest, fixtures, manifest_sha256 = _load_manifest(args.fixture_manifest)
        fixture_set_version = manifest["fixture_set_version"]
        verified_fixtures = [_read_verified_fixture(args.fixture_manifest, fixture) for fixture in fixtures]
        del verified_fixtures
        receipt = _receipt_base(
            args,
            fixture_set_version=fixture_set_version,
            manifest_sha256=manifest_sha256,
            fixture_count=len(fixtures),
        )
        if not args.execute_provider:
            receipt.update({
                "status": "DRY_RUN",
                "error_class": "PROVIDER_EXECUTION_DISABLED",
                "credential_present": "NOT_CHECKED",
            })
            _write_receipt(args.receipt_out, receipt)
            print("CREDENTIAL_PRESENT=NOT_CHECKED")
            return 0
        try:
            exit_code = asyncio.run(_execute(args, fixtures, receipt, args.fixture_manifest))
        except Exception:
            receipt.update({
                "status": "FAIL",
                "error_class": "HARNESS_EXECUTION_FAILED",
                "model_access": "FAIL",
                "vision_capability": "FAIL",
                "hermes_schema_compatibility": "NOT_RUN",
            })
            exit_code = 1
        _write_receipt(args.receipt_out, receipt)
        print(f"CREDENTIAL_PRESENT={'true' if receipt['credential_present'] else 'false'}")
        return exit_code
    except HarnessInputError as exc:
        receipt = _receipt_base(
            args,
            fixture_set_version=fixture_set_version,
            manifest_sha256=manifest_sha256,
            fixture_count=len(fixtures),
        )
        receipt.update({"status": "BLOCKED", "error_class": exc.error_class})
        _write_receipt(args.receipt_out, receipt)
        print("CREDENTIAL_PRESENT=NOT_CHECKED")
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
