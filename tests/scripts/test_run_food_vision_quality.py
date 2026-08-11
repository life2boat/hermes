"""Offline contracts for the opt-in Qwen/DashScope quality harness."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_food_vision_quality as harness


class _Telemetry:
    def __init__(self) -> None:
        self.external_request_attempts = 0
        self.external_request_budget = None
        self.retry_performed = False
        self.fallback_performed = False


def _install_provider(monkeypatch, provider_call):
    policy = object()
    monkeypatch.setattr(
        harness,
        "_provider_dependencies",
        lambda: (
            _Telemetry,
            policy,
            lambda response: response.choices[0].message.content,
            provider_call,
        ),
    )
    return policy


def _item(name: str, *, sauce: bool = False) -> dict[str, object]:
    return {
        "visible_name": name,
        "normalized_name": name,
        "confidence": 0.9,
        "estimated_grams_min": 20,
        "estimated_grams_max": 40,
        "preparation": "",
        "is_sauce": sauce,
        "uncertainty": "",
    }


def _payload(*names: str, sauce_names: tuple[str, ...] = ()) -> str:
    return json.dumps(
        {
            "schema_version": "food_vision_inventory_v1",
            "items": [_item(name, sauce=name in sauce_names) for name in names],
            "overall_confidence": 0.9,
            "needs_user_confirmation": False,
            "warnings": [],
        }
    )


def _response(payload: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=payload))],
    )


def _manifest(
    tmp_path: Path,
    *,
    corrupt_hash: bool = False,
    fixture_set_version: str = harness.FIXTURE_SET_VERSION,
) -> Path:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    fixtures = []
    for index, (foods, sauces) in enumerate(
        ((["apple"], []), (["banana"], []), ([], ["sauce"]))
    ):
        fixture_id = f"fixture-{index + 1}"
        image_path = fixture_dir / f"{fixture_id}.png"
        image_bytes = f"synthetic-image-{fixture_id}".encode("ascii")
        image_path.write_bytes(image_bytes)
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        fixtures.append(
            {
                "id": fixture_id,
                "image_path": image_path.name,
                "image_sha256": "0" * 64 if corrupt_hash and index == 0 else image_hash,
                "expected_food_items": foods,
                "expected_sauce_items": sauces,
                "allowed_aliases": {},
                "expected_needs_clarification": False,
                "expected_invalid": False,
            }
        )
    manifest_path = fixture_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"fixture_set_version": fixture_set_version, "fixtures": fixtures}),
        encoding="utf-8",
    )
    return manifest_path


def _args(manifest: Path, receipt: Path, *, execute: bool = False, provider: str = "alibaba", model: str = "qwen3.6-flash") -> list[str]:
    args = [
        "--provider", provider,
        "--model", model,
        "--fixture-manifest", str(manifest),
        "--receipt-out", str(receipt),
    ]
    if execute:
        args.append("--execute-provider")
    return args


def _read_receipt(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_dry_run_validates_all_hashes_and_makes_zero_provider_requests(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    called = False

    async def fake_provider(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("dry run must not call a provider")

    _install_provider(monkeypatch, fake_provider)

    assert harness.run(_args(manifest, receipt_path)) == 0

    receipt = _read_receipt(receipt_path)
    assert called is False
    assert receipt["status"] == "DRY_RUN"
    assert receipt["fixture_count"] == 3
    assert receipt["request_budget"] == 3
    assert receipt["requests_used"] == 0
    assert receipt["credential_present"] == "NOT_CHECKED"


@pytest.mark.parametrize("key_value", [None, ""])
def test_missing_or_empty_dashscope_key_blocks_before_any_request(tmp_path, monkeypatch, key_value):
    manifest = _manifest(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    calls = []
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("QWEN_API_KEY", "must-not-be-read")
    if key_value is not None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", key_value)

    async def fake_provider(**kwargs):
        calls.append(kwargs)
        raise AssertionError("missing key must block before provider invocation")

    _install_provider(monkeypatch, fake_provider)

    assert harness.run(_args(manifest, receipt_path, execute=True)) == 2

    receipt = _read_receipt(receipt_path)
    assert calls == []
    assert receipt["status"] == "BLOCKED"
    assert receipt["error_class"] == "CREDENTIAL_MISSING"
    assert receipt["credential_present"] is False
    assert "must-not-be-read" not in receipt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("provider", "model", "corrupt_hash", "expected_error"),
    [
        ("qwen", "qwen3.6-flash", False, "UNSUPPORTED_PROVIDER"),
        ("alibaba", "", False, "MODEL_REQUIRED"),
        ("alibaba", "qwen3.6-flash", True, "FIXTURE_HASH_MISMATCH"),
    ],
)
def test_invalid_preflight_never_calls_provider(tmp_path, monkeypatch, provider, model, corrupt_hash, expected_error):
    manifest = _manifest(tmp_path, corrupt_hash=corrupt_hash)
    receipt_path = tmp_path / "receipt.json"
    calls = []
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-test-key")

    async def fake_provider(**kwargs):
        calls.append(kwargs)
        raise AssertionError("invalid preflight must not call a provider")

    _install_provider(monkeypatch, fake_provider)

    assert harness.run(_args(manifest, receipt_path, execute=True, provider=provider, model=model)) == 2

    receipt = _read_receipt(receipt_path)
    assert calls == []
    assert receipt["error_class"] == expected_error
    assert receipt["requests_used"] == 0


def test_execution_uses_one_call_per_fixture_and_writes_sanitized_receipt(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    calls = []
    payloads = [_payload("apple"), _payload("banana"), _payload("sauce", sauce_names=("sauce",))]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-test-key")

    async def fake_provider(**kwargs):
        kwargs["request_telemetry"].external_request_attempts += 1
        calls.append(kwargs)
        return _response(payloads[len(calls) - 1])

    policy = _install_provider(monkeypatch, fake_provider)

    assert harness.run(_args(manifest, receipt_path, execute=True)) == 0

    receipt_text = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert len(calls) == 3
    assert all(call["call_policy"] is policy for call in calls)
    assert receipt["status"] == "PASS"
    assert receipt["requests_used"] == receipt["request_budget"] == 3
    assert receipt["retries_used"] == 0
    assert receipt["credential_recovery_retries"] == 0
    assert receipt["cross_provider_fallbacks"] == 0
    assert receipt["quality_gate"] == "PASS"
    assert receipt["model_access"] == "PASS"
    assert receipt["vision_capability"] == "PASS"
    assert receipt["hermes_schema_compatibility"] == "PASS"
    assert "synthetic-test-key" not in receipt_text
    assert "synthetic-image-fixture-1" not in receipt_text
    assert base64.b64encode(b"synthetic-image-fixture-1").decode("ascii") not in receipt_text
    assert str(tmp_path) not in receipt_text
    assert "data:image" not in receipt_text
    assert "payloads" not in receipt_text


def test_second_provider_failure_is_not_retried_or_fallen_back(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    calls = []
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-test-key")

    async def fake_provider(**kwargs):
        kwargs["request_telemetry"].external_request_attempts += 1
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("provider body must never reach receipt")
        return _response(_payload("apple"))

    _install_provider(monkeypatch, fake_provider)

    assert harness.run(_args(manifest, receipt_path, execute=True)) == 1

    receipt_text = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert len(calls) == 2
    assert receipt["requests_used"] == 2
    assert receipt["error_class"] == "PROVIDER_REQUEST_FAILED"
    assert receipt["cross_provider_fallbacks"] == 0
    assert "provider body" not in receipt_text


@pytest.mark.parametrize(
    ("payloads", "metric", "expected"),
    [
        ([_payload("apple", "extra"), _payload("banana"), _payload("sauce", sauce_names=("sauce",))], "precision", 0.90),
        ([_payload("apple"), _payload("extra"), _payload("sauce", sauce_names=("sauce",))], "recall", 0.90),
        ([_payload("apple"), _payload("banana"), _payload("extra")], "sauce_recall", 0.90),
        ([json.dumps({"totals": {"calories_kcal": 1}}), _payload("banana"), _payload("sauce", sauce_names=("sauce",))], "unsafe_aggregate_count", 0),
        (["not-json", _payload("banana"), _payload("sauce", sauce_names=("sauce",))], "invalid_aggregate_count", 0),
    ],
)
def test_quality_and_schema_failures_cannot_force_pass(tmp_path, monkeypatch, payloads, metric, expected):
    manifest = _manifest(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    calls = []
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-test-key")

    async def fake_provider(**kwargs):
        kwargs["request_telemetry"].external_request_attempts += 1
        calls.append(kwargs)
        return _response(payloads[len(calls) - 1])

    _install_provider(monkeypatch, fake_provider)

    assert harness.run(_args(manifest, receipt_path, execute=True)) == 1

    receipt = _read_receipt(receipt_path)
    assert receipt["status"] == "FAIL"
    if metric in {"unsafe_aggregate_count", "invalid_aggregate_count"}:
        assert receipt["hermes_schema_compatibility"] == "FAIL"
    else:
        assert receipt["aggregate"][metric] < expected


@pytest.mark.parametrize(
    ("fixture_version", "expected_manifest_sha256"),
    [
        ("v1", "7d946a450e84471345114ff1c31dc058289de6e8a87128353bee96a9c9a57505"),
        ("v2", "46eeef07535bf814167e2dab8c8c700ff4de14e1d47ecf7f8cfab21f6f3896c3"),
    ],
)
def test_checked_in_food_vision_assets_match_manifest(
    fixture_version: str,
    expected_manifest_sha256: str | None,
):
    repository_root = Path(__file__).resolve().parents[2]
    manifest_path = repository_root / f"tests/fixtures/food_vision_quality/{fixture_version}/manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)

    assert manifest["fixture_set_version"] == f"food_vision_quality_{fixture_version}"
    assert len(manifest["fixtures"]) == 3
    if expected_manifest_sha256 is not None:
        assert hashlib.sha256(manifest_bytes).hexdigest() == expected_manifest_sha256

    for fixture in manifest["fixtures"]:
        image_path = (manifest_path.parent / fixture["image_path"]).resolve(strict=True)
        image_path.relative_to(manifest_path.parent.resolve())
        assert image_path.is_file()
        assert hashlib.sha256(image_path.read_bytes()).hexdigest() == fixture["image_sha256"]


def test_v2_dry_run_validates_manifest_and_makes_zero_provider_requests(tmp_path, monkeypatch):
    repository_root = Path(__file__).resolve().parents[2]
    manifest_path = repository_root / "tests/fixtures/food_vision_quality/v2/manifest.json"
    receipt_path = tmp_path / "receipt.json"

    def fail_dependencies():
        raise AssertionError("dry run must not load provider dependencies")

    monkeypatch.setattr(harness, "_provider_dependencies", fail_dependencies)

    assert harness.run(_args(manifest_path, receipt_path)) == 0

    receipt = _read_receipt(receipt_path)
    assert receipt["fixture_set_version"] == "food_vision_quality_v2"
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert receipt["status"] == "DRY_RUN"
    assert receipt["requests_used"] == 0
    assert receipt["credential_present"] == "NOT_CHECKED"


def test_unsupported_fixture_version_fails_closed_without_provider_request(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, fixture_set_version="food_vision_quality_v3")
    receipt_path = tmp_path / "receipt.json"

    def fail_dependencies():
        raise AssertionError("unsupported version must not load provider dependencies")

    monkeypatch.setattr(harness, "_provider_dependencies", fail_dependencies)

    assert harness.run(_args(manifest, receipt_path)) == 2

    receipt = _read_receipt(receipt_path)
    assert receipt["status"] == "BLOCKED"
    assert receipt["error_class"] == "FIXTURE_MANIFEST_INVALID"
    assert receipt["fixture_set_version"] is None
    assert receipt["requests_used"] == 0


def test_tampered_v2_png_fails_before_provider_request(tmp_path, monkeypatch):
    repository_root = Path(__file__).resolve().parents[2]
    source_root = repository_root / "tests/fixtures/food_vision_quality/v2"
    fixture_root = tmp_path / "v2"
    shutil.copytree(source_root, fixture_root)
    image_path = fixture_root / "images/fixture_a.png"
    image_path.write_bytes(image_path.read_bytes() + b"tampered")
    receipt_path = tmp_path / "receipt.json"

    def fail_dependencies():
        raise AssertionError("hash mismatch must not load provider dependencies")

    monkeypatch.setattr(harness, "_provider_dependencies", fail_dependencies)

    assert harness.run(_args(fixture_root / "manifest.json", receipt_path)) == 2

    receipt = _read_receipt(receipt_path)
    assert receipt["status"] == "BLOCKED"
    assert receipt["error_class"] == "FIXTURE_HASH_MISMATCH"
    assert receipt["requests_used"] == 0


def test_receipt_v2_preserves_safe_fixture_diagnostics_without_raw_provider_content(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    calls = []
    payloads = [_payload("wrong-a"), _payload("wrong-b"), _payload("wrong-c")]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-test-key")

    async def fake_provider(**kwargs):
        kwargs["request_telemetry"].external_request_attempts += 1
        calls.append(kwargs)
        return _response(payloads[len(calls) - 1])

    _install_provider(monkeypatch, fake_provider)

    assert harness.run(_args(manifest, receipt_path, execute=True)) == 1

    receipt_text = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert receipt["schema_version"] == 2
    assert receipt["quality_gate"] == "FAIL"
    assert receipt["aggregate"]["precision"] == 0.0
    assert receipt["aggregate"]["recall"] == 0.0
    assert len(calls) == 3
    for expected, fixture in zip(("wrong-a", "wrong-b", "wrong-c"), receipt["fixtures"]):
        diagnostics = fixture["diagnostics"]
        assert diagnostics["matched_expected_components"] == []
        assert diagnostics["missed_expected_components"]
        assert diagnostics["unexpected_predicted_components"] == [expected]
        assert diagnostics["validated_prediction_labels"] == [expected]
    for forbidden in ("data:image", "base64", "DASHSCOPE_API_KEY", "synthetic-test-key"):
        assert forbidden not in receipt_text
