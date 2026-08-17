from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_engineering.production_runtime_attestation import (
    CollectorStatus,
    ComparisonStatus,
    ProductionRuntimeAttestationError,
    compare_production_runtime,
    create_attestation,
    create_collector_result,
    create_intended_state,
    deserialize_attestation,
    deserialize_intended_state,
    parse_json_document,
    sanitize_evidence,
    serialize_attestation,
    serialize_intended_state,
)
from scripts.production_runtime_attestation import run


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "production_runtime_attestation"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _intended():
    return deserialize_intended_state(_fixture("intended.json"))


def _attestation(name: str):
    return deserialize_attestation(_fixture(name))


def test_attestation_id_and_collector_order_are_deterministic() -> None:
    first = create_attestation(
        target="synthetic-prod",
        collected_at_utc="2026-01-02T03:04:05Z",
        collectors=[
            create_collector_result("zeta", "AVAILABLE", {"ready": True}),
            create_collector_result("alpha", "AVAILABLE", {"ready": True}),
        ],
    )
    second = create_attestation(
        target="synthetic-prod",
        collected_at_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        collectors=list(reversed(first.collectors)),
    )
    assert first.attestation_id == second.attestation_id
    assert [item.collector_id for item in first.collectors] == ["alpha", "zeta"]


def test_canonical_serialization_is_compact_sorted_utf8() -> None:
    payload = serialize_attestation(_attestation("evidence_match.json"))
    assert payload == serialize_attestation(deserialize_attestation(payload))
    assert b"\n" not in payload and b": " not in payload
    assert payload.decode("utf-8").startswith('{"attestation_id":')


def test_tampered_attestation_id_is_rejected() -> None:
    value = _attestation("evidence_match.json")
    with pytest.raises(ProductionRuntimeAttestationError, match="TAMPERED"):
        deserialize_attestation(serialize_attestation(replace(value, target="other")))


def test_duplicate_json_key_is_rejected() -> None:
    with pytest.raises(ProductionRuntimeAttestationError, match="DUPLICATE"):
        parse_json_document('{"target":"a","target":"b"}')


def test_non_finite_number_is_rejected() -> None:
    with pytest.raises(ProductionRuntimeAttestationError, match="NON_FINITE"):
        parse_json_document('{"value":NaN}')


def test_timestamp_must_be_canonical_utc() -> None:
    with pytest.raises(ProductionRuntimeAttestationError, match="UTC_TIMESTAMP"):
        create_attestation(
            target="synthetic-prod",
            collected_at_utc="2026-01-02T03:04:05+01:00",
            collectors=[],
        )


def test_sensitive_mapping_key_is_redacted_without_metadata() -> None:
    sanitized = sanitize_evidence(
        {"service_token": "fixture-placeholder-value", "configured": True}
    )
    assert sanitized == {"service_token": "<REDACTED>", "configured": True}
    assert "hash" not in json.dumps(sanitized).casefold()


def test_inline_credential_is_redacted() -> None:
    sanitized = sanitize_evidence(
        {"header": "Bearer fixture-placeholder-value", "healthy": True}
    )
    assert sanitized == {"header": "<REDACTED>", "healthy": True}


def test_unsanitized_attestation_deserialization_fails_closed() -> None:
    payload = json.loads(_fixture("evidence_match.json"))
    payload["collectors"][0]["observations"]["service_token"] = (
        "fixture-placeholder-value"
    )
    payload["attestation_id"] = "0" * 64
    with pytest.raises(ProductionRuntimeAttestationError):
        deserialize_attestation(json.dumps(payload))


def test_unsanitized_intended_state_deserialization_fails_closed() -> None:
    payload = json.loads(_fixture("intended.json"))
    payload["expected_collectors"][0]["observations"]["service_token"] = (
        "fixture-placeholder-value"
    )
    with pytest.raises(ProductionRuntimeAttestationError, match="UNSANITIZED"):
        deserialize_intended_state(json.dumps(payload))


def test_b1_modules_have_no_live_collector_imports() -> None:
    forbidden = {"docker", "qdrant_client", "sqlite3", "subprocess", "requests"}
    for relative in (
        "ai_engineering/production_runtime_attestation.py",
        "scripts/production_runtime_attestation.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (
                node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
            )
        }
        assert imports.isdisjoint(forbidden)


def test_match_fixture() -> None:
    result = compare_production_runtime(_intended(), _attestation("evidence_match.json"))
    assert result.status == ComparisonStatus.MATCH
    assert result.drifted_observations == result.missing_observations == ()


def test_drift_fixture_and_precedence_over_missing() -> None:
    result = compare_production_runtime(_intended(), _attestation("evidence_drift.json"))
    assert result.status == ComparisonStatus.DRIFT
    assert result.drifted_observations == ("runtime.image_revision",)
    assert result.missing_observations == ("storage.integrity",)


def test_insufficient_evidence_fixture() -> None:
    result = compare_production_runtime(
        _intended(), _attestation("evidence_insufficient.json")
    )
    assert result.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert result.drifted_observations == ()
    assert result.missing_observations == ("storage.integrity",)


def test_target_mismatch_is_contract_error() -> None:
    intended = create_intended_state(
        target="other-target", expected_observations={"runtime": {"running": True}}
    )
    with pytest.raises(ProductionRuntimeAttestationError, match="TARGET_MISMATCH"):
        compare_production_runtime(intended, _attestation("evidence_match.json"))


def test_collector_status_contract() -> None:
    result = create_collector_result("runtime", CollectorStatus.UNAVAILABLE, {})
    assert result.status == CollectorStatus.UNAVAILABLE
    with pytest.raises(ProductionRuntimeAttestationError, match="UNAVAILABLE"):
        create_collector_result("runtime", "UNAVAILABLE", {"running": False})


def test_cli_create_verify_and_sanitize(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    created = tmp_path / "created.json"
    verified = tmp_path / "verified.json"
    unsafe = tmp_path / "unsafe.json"
    safe = tmp_path / "safe.json"
    raw.write_text(
        json.dumps(
            {
                "target": "synthetic-prod",
                "collected_at_utc": "2026-01-02T03:04:05Z",
                "collectors": [
                    {
                        "collector_id": "runtime",
                        "status": "AVAILABLE",
                        "observations": {"running": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    unsafe.write_text(
        json.dumps({"service_token": "fixture-placeholder-value"}), encoding="utf-8"
    )
    assert run(["create", "--input", str(raw), "--output", str(created)]) == 0
    assert run(["verify", "--input", str(created), "--output", str(verified)]) == 0
    assert created.read_bytes() == verified.read_bytes()
    assert run(["sanitize", "--input", str(unsafe), "--output", str(safe)]) == 0
    assert json.loads(safe.read_bytes()) == {"service_token": "<REDACTED>"}


def test_cli_compare_exit_semantics(tmp_path: Path) -> None:
    expected = {
        "evidence_match.json": 0,
        "evidence_drift.json": 2,
        "evidence_insufficient.json": 2,
    }
    for index, (fixture, exit_code) in enumerate(expected.items()):
        output = tmp_path / f"comparison-{index}.json"
        assert run(
            [
                "compare",
                "--intended",
                str(FIXTURES / "intended.json"),
                "--attestation",
                str(FIXTURES / fixture),
                "--output",
                str(output),
            ]
        ) == exit_code
        assert output.is_file()
