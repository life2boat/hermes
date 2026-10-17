from __future__ import annotations

import ast
import json
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

from ai_engineering.production_runtime_attestation import (
    CollectorResult,
    CollectorStatus,
    ComparisonStatus,
    EvidenceProvenance,
    IntendedProductionState,
    MockRuntimeCollector,
    ObservedRuntimeState,
    ProductionRuntimeAttestation,
    ProductionRuntimeAttestationError,
    ProductionRuntimeComparison,
    RuntimeCollector,
    SIGNATURE_ALGORITHM_V2,
    SIGNATURE_CONTRACT_VERSION,
    StaticFixtureCollector,
    compare_production_runtime,
    compute_attestation_id,
    compute_v1_attestation_id,
    create_attestation,
    create_collector_result,
    create_intended_state,
    deserialize_attestation,
    deserialize_comparison,
    deserialize_intended_state,
    export_private_key_hex,
    export_public_key_hex,
    generate_keypair,
    import_private_key_hex,
    import_public_key_hex,
    normalize_attestation,
    parse_json_document,
    sanitize_evidence,
    sanitize_json_document,
    serialize_attestation,
    serialize_comparison,
    serialize_intended_state,
    sign_attestation,
    validate_attestation,
    validate_intended_state,
    verify_attestation_signature,
    verify_sanitized_evidence,
)
from scripts.production_runtime_attestation import run

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "production_runtime_attestation"
SCHEMA_FILE = ROOT / "schemas" / "production-runtime-attestation-v1.schema.json"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _intended() -> IntendedProductionState:
    return deserialize_intended_state(_fixture("intended.json"))


def _attestation(name: str) -> ProductionRuntimeAttestation:
    return deserialize_attestation(_fixture(name))


# ---------------------------------------------------------------------------
# Phase H: 16 Required Tests
# ---------------------------------------------------------------------------


def test_schema_validation() -> None:
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))

    # Valid legacy fixture
    valid_legacy = json.loads(_fixture("evidence_match.json"))
    jsonschema.validate(instance=valid_legacy, schema=schema)

    # Valid full v1 signed payload
    priv_key, pub_key = generate_keypair()
    pub_hex = export_public_key_hex(pub_key)
    priv_hex = export_private_key_hex(priv_key)
    att = create_attestation(
        canonical_repository="https://github.com/life2boat/hermes.git",
        canonical_main_sha="a" * 40,
        environment_id="production",
        collection_context_id="ctx-test",
        observed_at="2026-01-02T03:04:05Z",
        collector_id="test-collector",
        collector_version="1.0.0",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "b" * 64,
            oci_revision="c" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
        ),
    )
    signed_att = sign_attestation(att, priv_hex)
    v1_dict = json.loads(serialize_attestation(signed_att).decode("utf-8"))
    jsonschema.validate(instance=v1_dict, schema=schema)

    # Invalid payload (missing required field)
    invalid_dict = dict(valid_legacy)
    del invalid_dict["schema_version"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid_dict, schema=schema)


def test_deterministic_attestation_id() -> None:
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

    # Changing any content field changes attestation_id
    modified = create_attestation(
        target="synthetic-prod-2",
        collected_at_utc="2026-01-02T03:04:05Z",
        collectors=first.collectors,
    )
    assert first.attestation_id != modified.attestation_id


def test_canonical_serialization() -> None:
    payload = serialize_attestation(_attestation("evidence_match.json"))
    assert payload == serialize_attestation(deserialize_attestation(payload))
    assert b"\n" not in payload and b": " not in payload
    assert payload.decode("utf-8").startswith('{"attestation_id":')


def test_integrity_tamper_detection() -> None:
    value = _attestation("evidence_match.json")
    with pytest.raises(ProductionRuntimeAttestationError, match="TAMPERED"):
        deserialize_attestation(serialize_attestation(replace(value, target="other")))


def test_signature_verification() -> None:
    priv_key, pub_key = generate_keypair()
    pub_hex = export_public_key_hex(pub_key)
    priv_hex = export_private_key_hex(priv_key)

    att = create_attestation(
        target="synthetic-prod",
        collected_at_utc="2026-01-02T03:04:05Z",
        collectors=[create_collector_result("runtime", "AVAILABLE", {"running": True})],
    )
    signed_att = sign_attestation(att, priv_hex, signed_at_utc="2026-01-02T03:04:05Z")
    assert signed_att.evidence_provenance is not None
    assert signed_att.evidence_provenance.signer_identity == pub_hex
    assert signed_att.evidence_provenance.signature_algorithm == SIGNATURE_ALGORITHM_V2

    is_valid, reason = verify_attestation_signature(
        signed_att, [pub_hex], current_time_utc="2026-01-02T03:10:00Z"
    )
    assert is_valid is True
    assert reason == "SIGNATURE_VALID"


def test_untrusted_signature_rejection() -> None:
    priv_key1, pub_key1 = generate_keypair()
    priv_key2, pub_key2 = generate_keypair()
    pub_hex1 = export_public_key_hex(pub_key1)
    pub_hex2 = export_public_key_hex(pub_key2)

    att = create_attestation(
        target="synthetic-prod",
        collected_at_utc="2026-01-02T03:04:05Z",
        collectors=[create_collector_result("runtime", "AVAILABLE", {"running": True})],
    )
    # Signed by key1
    signed_att = sign_attestation(
        att, export_private_key_hex(priv_key1), signed_at_utc="2026-01-02T03:04:05Z"
    )

    # Verification against key2 only should be rejected
    is_valid, reason = verify_attestation_signature(
        signed_att, [pub_hex2], current_time_utc="2026-01-02T03:10:00Z"
    )
    assert is_valid is False
    assert reason == "UNTRUSTED_SIGNATURE_KEY"

    # Corrupted signature
    tampered_prov = EvidenceProvenance(
        signer_identity=pub_hex1,
        signature_algorithm=SIGNATURE_ALGORITHM_V2,
        signature="0" * 128,
        signed_at_utc=signed_att.evidence_provenance.signed_at_utc,
        valid_until_utc=signed_att.evidence_provenance.valid_until_utc,
        signature_contract_version=SIGNATURE_CONTRACT_VERSION,
    )
    tampered_att = replace(signed_att, evidence_provenance=tampered_prov)
    is_valid, reason = verify_attestation_signature(
        tampered_att, [pub_hex1], current_time_utc="2026-01-02T03:10:00Z"
    )
    assert is_valid is False
    assert reason in ("INVALID_CRYPTOGRAPHIC_SIGNATURE", "SIGNATURE_VERIFICATION_ERROR")


def test_missing_evidence() -> None:
    result = compare_production_runtime(
        _intended(), _attestation("evidence_insufficient.json")
    )
    assert result.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "storage.integrity" in result.missing_observations


def test_expired_evidence() -> None:
    priv_key, pub_key = generate_keypair()
    pub_hex = export_public_key_hex(pub_key)
    priv_hex = export_private_key_hex(priv_key)

    att = create_attestation(
        target="synthetic-prod",
        collected_at_utc="2026-01-02T03:04:05Z",
        collectors=[create_collector_result("runtime", "AVAILABLE", {"running": True})],
    )
    # Sign with validity duration of 10 seconds at 2026-01-02T03:04:05Z
    signed_att = sign_attestation(
        att, priv_hex, valid_duration_seconds=10, signed_at_utc="2026-01-02T03:04:05Z"
    )

    # Check with current time 100 seconds in the future
    future_time = datetime(2026, 1, 2, 4, 0, 0, tzinfo=UTC)
    is_valid, reason = verify_attestation_signature(
        signed_att, [pub_hex], current_time_utc=future_time
    )
    assert is_valid is False
    assert reason == "EXPIRED_EVIDENCE"


def test_match() -> None:
    # Scenario 1 — MATCH: authentic runtime evidence matches intended state
    att = _attestation("evidence_match.json")
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    result = compare_production_runtime(
        _intended(),
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert result.status == ComparisonStatus.MATCH
    assert result.drifted_observations == ()
    assert result.missing_observations == ()


def test_drift() -> None:
    # Scenario 2 — DRIFT: authentic runtime evidence contains mismatch in image digest / revision
    att = _attestation("evidence_drift.json")
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    result = compare_production_runtime(
        _intended(),
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert result.status == ComparisonStatus.DRIFT
    assert "runtime.image_revision" in result.drifted_observations


def test_insufficient_evidence() -> None:
    # Scenario 3 — INSUFFICIENT_EVIDENCE: evidence missing required collectors/observations
    result = compare_production_runtime(
        _intended(), _attestation("evidence_insufficient.json")
    )
    assert result.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert result.drifted_observations == ()
    assert "storage.integrity" in result.missing_observations


def test_secret_sanitization() -> None:
    # Canary secret in mapping keys and inline credential values
    canary = "ghp_CANARY_SECRET_LEAK_TOKEN_12345"
    sanitized = sanitize_evidence(
        {
            "service_token": canary,
            "normal_field": f"Bearer {canary}",
            "configured": True,
        }
    )
    serialized = json.dumps(sanitized)
    assert canary not in serialized
    assert "service_token" in sanitized
    assert sanitized["service_token"] == "<REDACTED>"
    assert sanitized["normal_field"] == "<REDACTED>"
    assert sanitized["configured"] is True


def test_collector_interface() -> None:
    # Test RuntimeCollector protocol and offline MockRuntimeCollector
    collector: RuntimeCollector = MockRuntimeCollector(
        collector_id="mock-1",
        collector_version="1.0.0",
        identity={"environment_id": "production"},
        image_metadata={"image_digest": "sha256:" + "0" * 64, "oci_revision": "0" * 40},
        service_state={"active_services": ["gateway"]},
        runtime_contract={"runtime_contract": "hermes-runtime-v1"},
        schema_metadata={"schema_version": 1},
    )
    assert collector.collector_id == "mock-1"
    assert collector.collect_identity() == {"environment_id": "production"}
    assert collector.collect_image_metadata()["oci_revision"] == "0" * 40
    assert collector.collect_service_state() == {"active_services": ["gateway"]}
    assert collector.collect_runtime_contract() == {"runtime_contract": "hermes-runtime-v1"}
    assert collector.collect_schema_metadata() == {"schema_version": 1}

    # StaticFixtureCollector
    fixture_collector = StaticFixtureCollector(
        {
            "collector_id": "fixture-1",
            "collector_version": "1.0.0",
            "identity": {"environment_id": "staging"},
        }
    )
    assert fixture_collector.collect_identity() == {"environment_id": "staging"}


def test_offline_cli(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    created = tmp_path / "created.json"
    verified = tmp_path / "verified.json"
    inspected = tmp_path / "inspected.json"
    unsafe = tmp_path / "unsafe.json"
    safe = tmp_path / "safe.json"
    priv_file = tmp_path / "priv_key.txt"
    key_file = tmp_path / "trusted_keys.txt"

    # Generate keypair for CLI signing/verification test
    priv_key, pub_key = generate_keypair()
    priv_hex = export_private_key_hex(priv_key)
    pub_hex = export_public_key_hex(pub_key)
    priv_file.write_text(priv_hex + "\n", encoding="utf-8")
    key_file.write_text(pub_hex + "\n", encoding="utf-8")

    now_utc_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw.write_text(
        json.dumps(
            {
                "target": "synthetic-prod",
                "collected_at_utc": now_utc_str,
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

    # CLI create with private key file
    assert (
        run(
            [
                "create",
                "--input",
                str(raw),
                "--output",
                str(created),
                "--private-key-file",
                str(priv_file),
            ]
        )
        == 0
    )
    assert created.is_file()

    # CLI verify with trusted keys
    assert (
        run(
            [
                "verify",
                "--input",
                str(created),
                "--output",
                str(verified),
                "--trusted-keys",
                str(key_file),
            ]
        )
        == 0
    )
    assert created.read_bytes() == verified.read_bytes()

    # CLI inspect
    assert (
        run(
            [
                "inspect",
                "--input",
                str(created),
                "--json",
                "--output",
                str(inspected),
            ]
        )
        == 0
    )
    assert inspected.is_file()

    # CLI sanitize
    assert run(["sanitize", "--input", str(unsafe), "--output", str(safe)]) == 0
    assert json.loads(safe.read_bytes()) == {"service_token": "<REDACTED>"}

    # CLI compare: unsigned fixture without trust store returns non-zero (exit code 2)
    comp_unsigned_output = tmp_path / "comparison_unsigned.json"
    assert (
        run(
            [
                "compare",
                "--intended",
                str(FIXTURES / "intended.json"),
                "--attestation",
                str(FIXTURES / "evidence_match.json"),
                "--output",
                str(comp_unsigned_output),
            ]
        )
        == 2
    )

    # CLI compare: signed and matching with trusted keys returns 0
    comp_output = tmp_path / "comparison.json"
    intended_file = tmp_path / "intended.json"
    intended_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": "synthetic-prod",
                "expected_collectors": [
                    {
                        "collector_id": "runtime",
                        "observations": {"running": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert (
        run(
            [
                "compare",
                "--intended",
                str(intended_file),
                "--attestation",
                str(created),
                "--output",
                str(comp_output),
                "--trusted-keys",
                str(key_file),
            ]
        )
        == 0
    )
    assert comp_output.is_file()


def test_cross_platform_determinism() -> None:
    # Verifies identical bytes & hash regardless of CRLF vs LF or key ordering
    json1 = '{"b": 2, "a": 1}\n'
    json2 = '{"a": 1, "b": 2}\r\n'
    doc1 = parse_json_document(json1)
    doc2 = parse_json_document(json2)
    bytes1 = sanitize_json_document(json1)
    bytes2 = sanitize_json_document(json2)
    assert bytes1 == bytes2 == b'{"a":1,"b":2}'


def test_no_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Verify AST imports have no prohibited network/live modules
    forbidden = {"docker", "qdrant_client", "sqlite3", "subprocess", "requests", "urllib"}
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

    # 2. Block socket operations at runtime and verify core functions execute without socket
    def blocked_socket(*args: object, **kwargs: object) -> None:
        pytest.fail("Network socket access attempted during offline attestation operation!")

    monkeypatch.setattr(socket, "socket", blocked_socket)

    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    att = create_attestation(
        target="synthetic-prod",
        collected_at_utc=now_str,
        collectors=[create_collector_result("runtime", "AVAILABLE", {"running": True})],
    )
    priv_key, pub_key = generate_keypair()
    signed = sign_attestation(att, export_private_key_hex(priv_key))
    serialized = serialize_attestation(signed)
    deserialized = deserialize_attestation(serialized)
    is_valid, _ = verify_attestation_signature(
        deserialized, [export_public_key_hex(pub_key)]
    )
    assert is_valid is True


# ---------------------------------------------------------------------------
# Additional Invariant & Boundary Tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# FIX-5: Required Security Remediation Regression Tests (10 tests)
# ---------------------------------------------------------------------------


def test_regression_unsigned_evidence_yields_insufficient_evidence() -> None:
    # 1. Unsigned evidence → INSUFFICIENT_EVIDENCE
    intended = _intended()
    unsigned_att = _attestation("evidence_match.json")
    priv, pub = generate_keypair()
    pub_hex = export_public_key_hex(pub)
    comp = compare_production_runtime(
        intended, unsigned_att, trusted_public_keys=[pub_hex]
    )
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "MISSING_EVIDENCE_PROVENANCE" in comp.reason_codes
    assert "evidence_provenance" in comp.missing_observations


def test_regression_missing_trust_store_yields_insufficient_evidence() -> None:
    # 2. Missing trust store → INSUFFICIENT_EVIDENCE
    intended = _intended()
    att = _attestation("evidence_match.json")
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")

    # Passing None for trusted_public_keys
    comp1 = compare_production_runtime(
        intended, signed_att, trusted_public_keys=None
    )
    assert comp1.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "MISSING_TRUST_STORE" in comp1.reason_codes
    assert "trusted_public_keys" in comp1.missing_observations

    # Passing empty sequence for trusted_public_keys
    comp2 = compare_production_runtime(
        intended, signed_att, trusted_public_keys=[]
    )
    assert comp2.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "MISSING_TRUST_STORE" in comp2.reason_codes


def test_regression_expired_signed_evidence_yields_insufficient_evidence() -> None:
    # 3. Expired signed evidence → INSUFFICIENT_EVIDENCE
    intended = _intended()
    att = _attestation("evidence_match.json")
    priv, pub = generate_keypair()
    pub_hex = export_public_key_hex(pub)
    t0 = "2026-01-02T03:04:05Z"
    signed_att = sign_attestation(
        att, priv, valid_duration_seconds=60, signed_at_utc=t0
    )
    # Evaluated after expiration
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[pub_hex],
        current_time_utc="2026-01-02T04:00:00Z",
    )
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "EXPIRED_EVIDENCE" in comp.reason_codes


def test_regression_tampered_validity_timestamp_breaks_signature() -> None:
    # 4. Tampered validity timestamp → invalid signature
    att = _attestation("evidence_match.json")
    priv, pub = generate_keypair()
    pub_hex = export_public_key_hex(pub)
    t0 = "2026-01-02T03:04:05Z"
    signed_att = sign_attestation(
        att, priv, valid_duration_seconds=3600, signed_at_utc=t0
    )
    assert signed_att.evidence_provenance is not None

    # Tamper valid_until_utc: metadata bound in signature payload breaks signature
    tampered_prov = replace(
        signed_att.evidence_provenance,
        valid_until_utc="2026-01-02T03:30:00Z",
    )
    tampered_att = replace(signed_att, evidence_provenance=tampered_prov)

    is_valid, reason = verify_attestation_signature(
        tampered_att, [pub_hex], current_time_utc="2026-01-02T03:10:00Z"
    )
    assert is_valid is False
    assert reason == "INVALID_CRYPTOGRAPHIC_SIGNATURE"

    # Also tamper signed_at_utc
    tampered_signed_at = replace(
        signed_att.evidence_provenance,
        signed_at_utc="2026-01-02T03:05:00Z",
    )
    tampered_att2 = replace(signed_att, evidence_provenance=tampered_signed_at)
    is_valid2, reason2 = verify_attestation_signature(
        tampered_att2, [pub_hex], current_time_utc="2026-01-02T03:10:00Z"
    )
    assert is_valid2 is False
    assert reason2 == "INVALID_CRYPTOGRAPHIC_SIGNATURE"


def test_regression_untrusted_signer_yields_insufficient_evidence() -> None:
    # 5. Untrusted signer → INSUFFICIENT_EVIDENCE
    intended = _intended()
    att = _attestation("evidence_match.json")
    priv_a, pub_a = generate_keypair()
    priv_b, pub_b = generate_keypair()
    signed_att = sign_attestation(
        att, priv_a, signed_at_utc="2026-01-02T03:04:05Z"
    )
    # Trust store only contains pub_b
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub_b)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "UNTRUSTED_SIGNATURE_KEY" in comp.reason_codes


def test_regression_authenticated_image_mismatch_yields_drift() -> None:
    # 6. Authenticated image mismatch → DRIFT
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "2" * 64,  # mismatched image digest
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(
        att, priv, signed_at_utc="2026-01-02T03:04:05Z"
    )
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.DRIFT
    assert any("image_digest" in d for d in comp.drifted_observations)
    assert "INVARIANT_DRIFT:image_digest" in comp.reason_codes


def test_regression_valid_signed_and_matching_state_yields_match() -> None:
    # 7. Valid signed and matching state → MATCH
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(
        att, priv, signed_at_utc="2026-01-02T03:04:05Z"
    )
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.MATCH
    assert comp.drifted_observations == ()
    assert comp.missing_observations == ()
    assert comp.reason_codes == ()


def test_regression_legacy_unsigned_fixture_never_authorizes_production_match() -> None:
    # 8. Legacy unsigned fixture never authorizes production MATCH
    intended = _intended()
    unsigned_att = _attestation("evidence_match.json")

    # Call without trust store
    comp_no_store = compare_production_runtime(intended, unsigned_att)
    assert comp_no_store.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert comp_no_store.status != ComparisonStatus.MATCH

    # Call with a valid trust store
    priv, pub = generate_keypair()
    comp_with_store = compare_production_runtime(
        intended, unsigned_att, trusted_public_keys=[export_public_key_hex(pub)]
    )
    assert comp_with_store.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert comp_with_store.status != ComparisonStatus.MATCH
    assert "MISSING_EVIDENCE_PROVENANCE" in comp_with_store.reason_codes


def test_regression_cli_verification_cannot_succeed_without_authenticity(
    tmp_path: Path,
) -> None:
    # 9. CLI verification cannot succeed without authenticity
    priv, pub = generate_keypair()
    pub_hex = export_public_key_hex(pub)
    key_file = tmp_path / "trusted_keys.txt"
    key_file.write_text(pub_hex + "\n", encoding="utf-8")

    # Unsigned attestation file
    unsigned_path = tmp_path / "unsigned.json"
    unsigned_path.write_bytes(serialize_attestation(_attestation("evidence_match.json")))

    # Verify without --trusted-keys fails closed (non-zero)
    ret_no_keys = run(["verify", "--input", str(unsigned_path)])
    assert ret_no_keys != 0

    # Verify unsigned evidence with --trusted-keys fails closed (non-zero)
    ret_unsigned = run(
        ["verify", "--input", str(unsigned_path), "--trusted-keys", str(key_file)]
    )
    assert ret_unsigned != 0

    # Verify with --integrity-only succeeds for structurally valid unsigned evidence
    ret_integrity = run(["verify", "--input", str(unsigned_path), "--integrity-only"])
    assert ret_integrity == 0


def test_regression_no_private_key_leakage_via_cli_logs_or_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 10. No private-key leakage via CLI, logs, or artifacts
    priv, pub = generate_keypair()
    priv_hex = export_private_key_hex(priv)
    priv_file = tmp_path / "secret_key.txt"
    priv_file.write_text(priv_hex + "\n", encoding="utf-8")

    raw_file = tmp_path / "raw.json"
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_file.write_text(
        json.dumps(
            {
                "target": "synthetic-prod",
                "collected_at_utc": now_str,
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
    out_file = tmp_path / "created.json"

    # Reject raw hex argument `--private-key` (CLI fails with unknown argument)
    with pytest.raises(SystemExit):
        run(
            [
                "create",
                "--input",
                str(raw_file),
                "--output",
                str(out_file),
                "--private-key",
                priv_hex,
            ]
        )

    # Clear captured stderr from the rejected command invocation
    capsys.readouterr()

    # Valid execution using `--private-key-file`
    ret = run(
        [
            "create",
            "--input",
            str(raw_file),
            "--output",
            str(out_file),
            "--private-key-file",
            str(priv_file),
        ]
    )
    assert ret == 0

    captured = capsys.readouterr()
    # Check stdout and stderr never leak private key hex
    assert priv_hex not in captured.out
    assert priv_hex not in captured.err

    # Check serialized artifact never leaks private key hex
    artifact_bytes = out_file.read_bytes()
    assert priv_hex.encode("utf-8") not in artifact_bytes
    assert bytes.fromhex(priv_hex) not in artifact_bytes


# ===========================================================================
# FIX-5: 18 Mandatory ProductionRuntimeAttestation v1.2 Regression Tests
# ===========================================================================


# 1. SIGNED_EMPTY_EVIDENCE_NO_MATCH
def test_regression_signed_empty_evidence_no_match() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        collectors=(),
        observed_runtime_state=None,
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "EMPTY_RUNTIME_OBSERVATIONS" in comp.reason_codes
    assert comp.authoritative_for_production is False


# 2. PARTIAL_INVARIANTS_MISSING_INSUFFICIENT_EVIDENCE
def test_regression_partial_invariants_missing_insufficient_evidence() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="",  # Missing / unpopulated
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "MISSING_OBSERVED_INVARIANT:release_identity" in comp.reason_codes
    assert comp.authoritative_for_production is False


# 3. DRIFT_AND_MISSING_COOCCURRENCE_INSUFFICIENT_EVIDENCE
def test_regression_drift_and_missing_cooccurrence_insufficient_evidence() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "9" * 64,  # DRIFT
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="",  # MISSING
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "INVARIANT_DRIFT:image_digest" in comp.reason_codes
    assert "MISSING_OBSERVED_INVARIANT:release_identity" in comp.reason_codes
    assert comp.authoritative_for_production is False


# 4. EXTRA_SERVICE_DETECTED_DRIFT
def test_regression_extra_service_detected_drift() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_service_set=("gateway",),
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway", "unapproved_worker"),  # Extra service!
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.DRIFT
    assert "EXTRA_SERVICE_DETECTED" in comp.reason_codes
    assert "EXTRA_SERVICE:unapproved_worker" in comp.reason_codes
    assert "service:unapproved_worker" in comp.drifted_observations


# 5. CANONICAL_PROVENANCE_MISMATCH_DRIFT_OR_INSUFFICIENT
def test_regression_canonical_provenance_mismatch_drift_or_insufficient() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        canonical_main_sha="f" * 40,  # Provenance drift
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.DRIFT
    assert "INVARIANT_DRIFT:canonical_main_sha" in comp.reason_codes
    assert any("canonical_main_sha" in d for d in comp.drifted_observations)


# 6. ZERO_PLACEHOLDER_HASH_REJECTED
def test_regression_zero_placeholder_hash_rejected() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "0" * 64,  # Dummy placeholder
            oci_revision="b" * 40,
            release_sha="0" * 40,  # Dummy placeholder
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "MISSING_OBSERVED_INVARIANT:release_sha" in comp.reason_codes
    assert "MISSING_OBSERVED_INVARIANT:image_digest" in comp.reason_codes
    assert comp.authoritative_for_production is False


# 7. AUTHORITATIVE_MODE_TRUE_ON_FRESH_SIGNED_MATCH
def test_regression_authoritative_mode_true_on_fresh_signed_match() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    now_utc_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    att = create_attestation(
        target="synthetic-prod",
        observed_at=now_utc_str,
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc=now_utc_str)
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc=None,
    )
    assert comp.status == ComparisonStatus.MATCH
    assert comp.evaluation_mode == "AUTHORITATIVE_PRODUCTION"
    assert comp.authoritative_for_production is True


# 8. DIAGNOSTIC_TIME_OVERRIDE_NOT_AUTHORITATIVE
def test_regression_diagnostic_time_override_not_authoritative() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.MATCH
    assert comp.evaluation_mode == "DIAGNOSTIC_TIME_OVERRIDE"
    assert comp.authoritative_for_production is False


# 9. CLI_COMPARE_EXIT_CODE_3_ON_DIAGNOSTIC_MATCH
def test_regression_cli_compare_exit_code_3_on_diagnostic_match(tmp_path: Path) -> None:
    intended_file = tmp_path / "intended.json"
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    intended_file.write_bytes(serialize_intended_state(intended))

    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    att_file = tmp_path / "attestation.json"
    att_file.write_bytes(serialize_attestation(signed_att))

    key_file = tmp_path / "trusted_keys.txt"
    key_file.write_text(export_public_key_hex(pub) + "\n", encoding="utf-8")
    comp_file = tmp_path / "comparison.json"

    exit_code = run(
        [
            "compare",
            "--intended",
            str(intended_file),
            "--attestation",
            str(att_file),
            "--output",
            str(comp_file),
            "--trusted-keys",
            str(key_file),
            "--current-time-utc",
            "2026-01-02T03:10:00Z",
        ]
    )
    assert exit_code == 3


# 10. CLI_COMPARE_EXIT_CODE_0_ON_AUTHORITATIVE_MATCH
def test_regression_cli_compare_exit_code_0_on_authoritative_match(tmp_path: Path) -> None:
    intended_file = tmp_path / "intended.json"
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    intended_file.write_bytes(serialize_intended_state(intended))

    now_utc_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    att = create_attestation(
        target="synthetic-prod",
        observed_at=now_utc_str,
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc=now_utc_str)
    att_file = tmp_path / "attestation.json"
    att_file.write_bytes(serialize_attestation(signed_att))

    key_file = tmp_path / "trusted_keys.txt"
    key_file.write_text(export_public_key_hex(pub) + "\n", encoding="utf-8")
    comp_file = tmp_path / "comparison.json"

    exit_code = run(
        [
            "compare",
            "--intended",
            str(intended_file),
            "--attestation",
            str(att_file),
            "--output",
            str(comp_file),
            "--trusted-keys",
            str(key_file),
        ]
    )
    assert exit_code == 0


# 11. UNTRUSTED_FRESHNESS_POLICY_REJECTED
def test_regression_untrusted_freshness_policy_rejected() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")

    comp1 = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
        max_evidence_age_seconds=100000,
    )
    assert comp1.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "UNTRUSTED_FRESHNESS_POLICY" in comp1.reason_codes

    comp2 = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
        max_clock_skew_seconds=600,
    )
    assert comp2.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "UNTRUSTED_FRESHNESS_POLICY" in comp2.reason_codes


# 12. INDEPENDENT_EVALUATION_OF_COLLECTORS_AND_STRUCTURED_STATE
def test_regression_independent_evaluation_of_collectors_and_structured_state() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        expected_observations={"runtime": {"running": True}},
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
        has_structured_invariants=True,
    )
    priv, pub = generate_keypair()
    pub_hex = export_public_key_hex(pub)

    # Case A: Collectors match, structured state drifts -> DRIFT
    att_drift_structured = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        collectors=[create_collector_result("runtime", "AVAILABLE", {"running": True})],
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "9" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    signed_a = sign_attestation(att_drift_structured, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp_a = compare_production_runtime(
        intended, signed_a, trusted_public_keys=[pub_hex], current_time_utc="2026-01-02T03:10:00Z"
    )
    assert comp_a.status == ComparisonStatus.DRIFT
    assert "INVARIANT_DRIFT:image_digest" in comp_a.reason_codes

    # Case B: Structured state matches, collectors drift -> DRIFT
    att_drift_collector = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        collectors=[create_collector_result("runtime", "AVAILABLE", {"running": False})],
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    signed_b = sign_attestation(att_drift_collector, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp_b = compare_production_runtime(
        intended, signed_b, trusted_public_keys=[pub_hex], current_time_utc="2026-01-02T03:10:00Z"
    )
    assert comp_b.status == ComparisonStatus.DRIFT
    assert "OBSERVATION_DRIFT:runtime.running" in comp_b.reason_codes

    # Case C: Both match -> MATCH
    att_match_both = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        collectors=[create_collector_result("runtime", "AVAILABLE", {"running": True})],
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    signed_c = sign_attestation(att_match_both, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp_c = compare_production_runtime(
        intended, signed_c, trusted_public_keys=[pub_hex], current_time_utc="2026-01-02T03:10:00Z"
    )
    assert comp_c.status == ComparisonStatus.MATCH


# 13. RELEASE_IDENTITY_MISMATCH_DRIFT
def test_regression_release_identity_mismatch_drift() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-v2-prod",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-v1-old",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.DRIFT
    assert "INVARIANT_DRIFT:release_identity" in comp.reason_codes
    assert any("release_identity" in d for d in comp.drifted_observations)


# 14. RELEASE_IDENTITY_MISSING_INSUFFICIENT_EVIDENCE
def test_regression_release_identity_missing_insufficient_evidence() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-v2-prod",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "MISSING_OBSERVED_INVARIANT:release_identity" in comp.reason_codes


# 15. REASON_CODES_SERIALIZED_AND_BOUND_IN_COMPARISON_ID
def test_regression_reason_codes_serialized_and_bound_in_comparison_id() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "2" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    payload_bytes = serialize_comparison(comp)
    raw_dict = json.loads(payload_bytes.decode("utf-8"))

    deserialized = deserialize_comparison(payload_bytes)
    assert deserialized.comparison_id == comp.comparison_id

    # Tampering with reason_codes breaks comparison_id
    tampered_reasons = dict(raw_dict)
    tampered_reasons["reason_codes"] = ["FORGED_REASON"]
    with pytest.raises(ProductionRuntimeAttestationError, match="TAMPERED_COMPARISON_ID"):
        deserialize_comparison(json.dumps(tampered_reasons))

    # Tampering with authoritative_for_production breaks comparison_id
    tampered_auth = dict(raw_dict)
    tampered_auth["authoritative_for_production"] = True
    with pytest.raises(ProductionRuntimeAttestationError, match="TAMPERED_COMPARISON_ID"):
        deserialize_comparison(json.dumps(tampered_auth))

    # Tampering with evaluation_mode breaks comparison_id
    tampered_mode = dict(raw_dict)
    tampered_mode["evaluation_mode"] = "AUTHORITATIVE_PRODUCTION"
    with pytest.raises(ProductionRuntimeAttestationError, match="TAMPERED_COMPARISON_ID"):
        deserialize_comparison(json.dumps(tampered_mode))


# 16. LEGACY_COMPARISON_DESERIALIZATION_NOT_AUTHORITATIVE
def test_regression_legacy_comparison_deserialization_not_authoritative() -> None:
    from ai_engineering.production_runtime_attestation import (
        _digest,
        _legacy_comparison_content,
    )

    legacy_dict = {
        "schema_version": 1,
        "target": "synthetic-prod",
        "attestation_id": "a" * 64,
        "intended_digest": "b" * 64,
        "status": "MATCH",
        "drifted_observations": [],
        "missing_observations": [],
    }
    legacy_content = _legacy_comparison_content(
        target=legacy_dict["target"],
        attestation_id=legacy_dict["attestation_id"],
        intended_digest=legacy_dict["intended_digest"],
        status=ComparisonStatus.MATCH,
        drifted=(),
        missing=(),
    )
    legacy_dict["comparison_id"] = _digest(legacy_content)

    comp = deserialize_comparison(json.dumps(legacy_dict))
    assert comp.status == ComparisonStatus.MATCH
    assert comp.authoritative_for_production is False
    assert comp.evaluation_mode == "LEGACY_UNSPECIFIED"


# 17. UNSIGNED_OR_TAMPERED_EVIDENCE_NO_MATCH
def test_regression_unsigned_or_tampered_evidence_no_match() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    comp_unsigned = compare_production_runtime(intended, att)
    assert comp_unsigned.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert comp_unsigned.authoritative_for_production is False

    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    tampered_prov = replace(signed_att.evidence_provenance, signature="0" * 128)
    tampered_att = replace(signed_att, evidence_provenance=tampered_prov)

    comp_tampered = compare_production_runtime(
        intended,
        tampered_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp_tampered.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert comp_tampered.authoritative_for_production is False


# 18. VALID_SIGNED_COMPLETE_MATCH
def test_regression_valid_signed_complete_match() -> None:
    intended = create_intended_state(
        target="synthetic-prod",
        intended_image_digest="sha256:" + "1" * 64,
        intended_main_sha="a" * 40,
        intended_oci_revision="b" * 40,
        expected_release_identity="hermes-release-1",
    )
    att = create_attestation(
        target="synthetic-prod",
        observed_at="2026-01-02T03:04:05Z",
        observed_runtime_state=ObservedRuntimeState(
            image_digest="sha256:" + "1" * 64,
            oci_revision="b" * 40,
            release_sha="a" * 40,
            runtime_contract="hermes-runtime-v1",
            schema_version=1,
            active_services=("gateway",),
            configuration_contract_version="v1",
            release_identity="hermes-release-1",
        ),
    )
    priv, pub = generate_keypair()
    signed_att = sign_attestation(att, priv, signed_at_utc="2026-01-02T03:04:05Z")
    comp = compare_production_runtime(
        intended,
        signed_att,
        trusted_public_keys=[export_public_key_hex(pub)],
        current_time_utc="2026-01-02T03:10:00Z",
    )
    assert comp.status == ComparisonStatus.MATCH
    assert comp.drifted_observations == ()
    assert comp.missing_observations == ()
    assert comp.reason_codes == ()
