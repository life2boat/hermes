"""Tests for ai_engineering.evidence_receipts."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ai_engineering.evidence_receipts import (
    AuthorityKind,
    AuthorityStatus,
    EvidenceCollectionReceipt,
    ExecutionAuthorityReceipt,
    ReceiptError,
    create_authority_receipt,
    create_collection_receipt,
    deserialize_authority_receipt,
    deserialize_collection_receipt,
    serialize_authority_receipt,
    serialize_collection_receipt,
    validate_authority_receipt,
    validate_collection_receipt,
)

SAMPLE_SHA = "a" * 40
SAMPLE_DIGEST = "b" * 64


def _make_authority_receipt(**kwargs) -> ExecutionAuthorityReceipt:
    defaults = dict(
        task_id="TEST-TASK-001",
        intent_digest=SAMPLE_DIGEST,
        subject_sha=SAMPLE_SHA,
        authority_kind=AuthorityKind.OPERATOR_TOKEN,
        authority_name="SUSTAINED_WSL_R0_2A_ALLOWED",
        authorized_effect_classes=["READ_ONLY_WSL_SESSION"],
        forbidden_effect_classes=["PRODUCTION_MUTATION", "DATABASE_WRITE"],
        stop_boundary="READ_ONLY_EVIDENCE_COLLECTION_COMPLETE",
        status=AuthorityStatus.AUTHORITY_RECORDED,
    )
    defaults.update(kwargs)
    return create_authority_receipt(**defaults)


def _make_collection_receipt(**kwargs) -> EvidenceCollectionReceipt:
    defaults = dict(
        task_id="TEST-TASK-001",
        intent_digest=SAMPLE_DIGEST,
        subject_sha=SAMPLE_SHA,
        producer_id="test-producer",
        artifact_ref="runtime-evidence.json",
        artifact_digest=SAMPLE_DIGEST,
        collection_mode="READ_ONLY_WSL_SESSION",
    )
    defaults.update(kwargs)
    return create_collection_receipt(**defaults)


class TestAuthorityReceiptCreate:
    def test_creates_valid_receipt(self) -> None:
        r = _make_authority_receipt()
        assert r.schema_version == 1
        assert r.authority_kind == AuthorityKind.OPERATOR_TOKEN
        assert r.status == AuthorityStatus.AUTHORITY_RECORDED
        assert r.attestation_ref is None

    def test_receipt_id_is_deterministic(self) -> None:
        r1 = _make_authority_receipt()
        r2 = _make_authority_receipt()
        assert r1.receipt_id == r2.receipt_id

    def test_receipt_id_changes_on_authority_name_change(self) -> None:
        r1 = _make_authority_receipt(authority_name="TOKEN_A")
        r2 = _make_authority_receipt(authority_name="TOKEN_B")
        assert r1.receipt_id != r2.receipt_id

    def test_authority_recorded_vs_externally_attested(self) -> None:
        assert AuthorityStatus.AUTHORITY_RECORDED != AuthorityStatus.AUTHORITY_EXTERNALLY_ATTESTED

    def test_externally_attested_requires_attestation_ref(self) -> None:
        with pytest.raises(ReceiptError) as exc_info:
            _make_authority_receipt(
                status=AuthorityStatus.AUTHORITY_EXTERNALLY_ATTESTED,
                attestation_ref=None,
            )
        assert exc_info.value.code == "ATTESTATION_REF_REQUIRED"

    def test_externally_attested_with_ref(self) -> None:
        r = _make_authority_receipt(
            status=AuthorityStatus.AUTHORITY_EXTERNALLY_ATTESTED,
            attestation_ref="https://example.com/attestation",
        )
        assert r.status == AuthorityStatus.AUTHORITY_EXTERNALLY_ATTESTED
        assert r.attestation_ref == "https://example.com/attestation"


class TestAuthorityReceiptTampering:
    def test_tampered_receipt_id_rejected(self) -> None:
        r = _make_authority_receipt()
        serialized = serialize_authority_receipt(r)
        serialized["receipt_id"] = "f" * 64
        with pytest.raises(ReceiptError) as exc_info:
            deserialize_authority_receipt(serialized)
        assert exc_info.value.code == "TAMPERED_RECEIPT_ID"

    def test_tampered_authority_name_rejected(self) -> None:
        r = _make_authority_receipt()
        serialized = serialize_authority_receipt(r)
        serialized["authority_name"] = "DIFFERENT_TOKEN"
        with pytest.raises(ReceiptError) as exc_info:
            deserialize_authority_receipt(serialized)
        assert exc_info.value.code == "TAMPERED_RECEIPT_ID"


class TestCollectionReceiptCreate:
    def test_creates_valid_receipt(self) -> None:
        r = _make_collection_receipt()
        assert r.schema_version == 1
        assert r.collection_mode == "READ_ONLY_WSL_SESSION"
        assert r.authority_receipt_id is None

    def test_with_authority_receipt_id(self) -> None:
        r = _make_collection_receipt(authority_receipt_id=SAMPLE_DIGEST)
        assert r.authority_receipt_id == SAMPLE_DIGEST

    def test_receipt_id_deterministic(self) -> None:
        r1 = _make_collection_receipt()
        r2 = _make_collection_receipt()
        assert r1.receipt_id == r2.receipt_id

    def test_receipt_id_changes_on_artifact_digest_change(self) -> None:
        r1 = _make_collection_receipt(artifact_digest=SAMPLE_DIGEST)
        r2 = _make_collection_receipt(artifact_digest="c" * 64)
        assert r1.receipt_id != r2.receipt_id


class TestCollectionReceiptTampering:
    def test_tampered_receipt_id_rejected(self) -> None:
        r = _make_collection_receipt()
        serialized = serialize_collection_receipt(r)
        serialized["receipt_id"] = "f" * 64
        with pytest.raises(ReceiptError) as exc_info:
            deserialize_collection_receipt(serialized)
        assert exc_info.value.code == "TAMPERED_RECEIPT_ID"


class TestJsonRoundtrip:
    def test_authority_receipt_roundtrip(self) -> None:
        r = _make_authority_receipt()
        s = serialize_authority_receipt(r)
        d = deserialize_authority_receipt(json.dumps(s))
        assert d.receipt_id == r.receipt_id

    def test_collection_receipt_roundtrip(self) -> None:
        r = _make_collection_receipt(authority_receipt_id=SAMPLE_DIGEST)
        s = serialize_collection_receipt(r)
        d = deserialize_collection_receipt(json.dumps(s))
        assert d.receipt_id == r.receipt_id
        assert d.authority_receipt_id == SAMPLE_DIGEST


class TestSchemaVersion:
    def test_wrong_authority_schema_version_rejected(self) -> None:
        r = _make_authority_receipt()
        s = serialize_authority_receipt(r)
        s["schema_version"] = 99
        with pytest.raises(ReceiptError) as exc_info:
            deserialize_authority_receipt(s)
        assert exc_info.value.code == "UNKNOWN_RECEIPT_SCHEMA_VERSION"

    def test_wrong_collection_schema_version_rejected(self) -> None:
        r = _make_collection_receipt()
        s = serialize_collection_receipt(r)
        s["schema_version"] = 99
        with pytest.raises(ReceiptError) as exc_info:
            deserialize_collection_receipt(s)
        assert exc_info.value.code == "UNKNOWN_RECEIPT_SCHEMA_VERSION"
