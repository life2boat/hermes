"""Canonical ExecutionAuthorityReceipt and EvidenceCollectionReceipt.

These receipts bind runtime execution provenance to the ICP evidence chain.

KEY DISTINCTION
===============
AUTHORITY_RECORDED
    The authority token name is recorded in this artifact.
    No external cryptographic attestation has been performed.
    The receipt preserves the capability name and authorized scope,
    but cannot be independently verified without the original operator.

AUTHORITY_EXTERNALLY_ATTESTED
    A cryptographic or external-system attestation of the authority
    exists and is referenced.  This distinction must be recorded
    honestly; do not manufacture attestation that does not exist.

SECRET SAFETY
=============
Do NOT store secret values in these receipts.
The authority_name is a capability identifier (e.g. SUSTAINED_WSL_R0_2A_ALLOWED),
not a credential, token, key, or password.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


AUTHORITY_RECEIPT_SCHEMA_VERSION = 1
COLLECTION_RECEIPT_SCHEMA_VERSION = 1

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class ReceiptError(ValueError):
    """Fail-closed error with a stable error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise ReceiptError(code)


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("VALUE_INVALID")
    return value


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("VALUE_INVALID")
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        _fail("REQUIRED_FIELD_MISSING")
    return value


def _exact_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    payload = _mapping(value)
    keys = frozenset(payload)
    if expected - keys:
        _fail("REQUIRED_FIELD_MISSING")
    if keys - expected:
        _fail("UNEXPECTED_FIELD")
    return payload


def _enum(value: object, enum_type: type, code: str = "VALUE_INVALID"):
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        _fail(code)
    try:
        return enum_type(value)
    except ValueError:
        _fail(code)


def _compute_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# ExecutionAuthorityReceipt
# ---------------------------------------------------------------------------


class AuthorityKind(StrEnum):
    OPERATOR_TOKEN = "OPERATOR_TOKEN"
    SIGNED_ATTESTATION = "SIGNED_ATTESTATION"


class AuthorityStatus(StrEnum):
    """Attestation level of the recorded authority.

    AUTHORITY_RECORDED
        The authority is recorded by name in this artifact.
        No external cryptographic attestation exists.
        Honest default when external platform attestation is unavailable.

    AUTHORITY_EXTERNALLY_ATTESTED
        External cryptographic or system attestation of this authority
        exists and is referenced via attestation_ref.
    """
    AUTHORITY_RECORDED = "AUTHORITY_RECORDED"
    AUTHORITY_EXTERNALLY_ATTESTED = "AUTHORITY_EXTERNALLY_ATTESTED"


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityReceipt:
    schema_version: int
    receipt_id: str  # content-bound digest
    task_id: str
    intent_digest: str
    subject_sha: str
    authority_kind: AuthorityKind
    authority_name: str  # capability name, NOT a secret
    authorized_effect_classes: tuple[str, ...]
    forbidden_effect_classes: tuple[str, ...]
    stop_boundary: str
    status: AuthorityStatus
    attestation_ref: str | None  # only for AUTHORITY_EXTERNALLY_ATTESTED


_AUTHORITY_RECEIPT_FIELDS = frozenset({
    "schema_version",
    "receipt_id",
    "task_id",
    "intent_digest",
    "subject_sha",
    "authority_kind",
    "authority_name",
    "authorized_effect_classes",
    "forbidden_effect_classes",
    "stop_boundary",
    "status",
    "attestation_ref",
})


def compute_authority_receipt_id(
    task_id: str,
    intent_digest: str,
    subject_sha: str,
    authority_kind: AuthorityKind | str,
    authority_name: str,
    authorized_effect_classes: Sequence[str],
    forbidden_effect_classes: Sequence[str],
    stop_boundary: str,
    status: AuthorityStatus | str,
    attestation_ref: str | None,
    schema_version: int = AUTHORITY_RECEIPT_SCHEMA_VERSION,
) -> str:
    kind_val = authority_kind.value if isinstance(authority_kind, AuthorityKind) else str(authority_kind)
    status_val = status.value if isinstance(status, AuthorityStatus) else str(status)
    return _compute_digest({
        "attestation_ref": attestation_ref,
        "authority_kind": kind_val,
        "authority_name": str(authority_name),
        "authorized_effect_classes": sorted(str(e) for e in authorized_effect_classes),
        "forbidden_effect_classes": sorted(str(e) for e in forbidden_effect_classes),
        "intent_digest": str(intent_digest),
        "schema_version": schema_version,
        "status": status_val,
        "stop_boundary": str(stop_boundary),
        "subject_sha": str(subject_sha),
        "task_id": str(task_id),
    })


def create_authority_receipt(
    task_id: str,
    intent_digest: str,
    subject_sha: str,
    authority_kind: AuthorityKind | str,
    authority_name: str,
    authorized_effect_classes: Sequence[str],
    forbidden_effect_classes: Sequence[str],
    stop_boundary: str,
    status: AuthorityStatus | str,
    attestation_ref: str | None = None,
) -> ExecutionAuthorityReceipt:
    """Public factory for ExecutionAuthorityReceipt with deterministic receipt_id."""
    t_id = _identifier(task_id)
    i_dgst = _digest(intent_digest)
    s_sha = _sha(subject_sha)
    a_kind = _enum(authority_kind, AuthorityKind)
    a_name = _string(authority_name)
    auth_eff = tuple(_string(e) for e in authorized_effect_classes)
    forb_eff = tuple(_string(e) for e in forbidden_effect_classes)
    stop = _string(stop_boundary)
    a_status = _enum(status, AuthorityStatus)
    att_ref = _string(attestation_ref) if attestation_ref is not None else None

    if a_status == AuthorityStatus.AUTHORITY_EXTERNALLY_ATTESTED and att_ref is None:
        _fail("ATTESTATION_REF_REQUIRED")

    receipt_id = compute_authority_receipt_id(
        task_id=t_id,
        intent_digest=i_dgst,
        subject_sha=s_sha,
        authority_kind=a_kind,
        authority_name=a_name,
        authorized_effect_classes=auth_eff,
        forbidden_effect_classes=forb_eff,
        stop_boundary=stop,
        status=a_status,
        attestation_ref=att_ref,
    )
    receipt = ExecutionAuthorityReceipt(
        schema_version=AUTHORITY_RECEIPT_SCHEMA_VERSION,
        receipt_id=receipt_id,
        task_id=t_id,
        intent_digest=i_dgst,
        subject_sha=s_sha,
        authority_kind=a_kind,
        authority_name=a_name,
        authorized_effect_classes=auth_eff,
        forbidden_effect_classes=forb_eff,
        stop_boundary=stop,
        status=a_status,
        attestation_ref=att_ref,
    )
    return validate_authority_receipt(receipt)


def _authority_from_mapping(payload: Mapping[str, object]) -> ExecutionAuthorityReceipt:
    task_id = _identifier(payload["task_id"])
    intent_dgst = _digest(payload["intent_digest"])
    subject_sha = _sha(payload["subject_sha"])
    a_kind = _enum(payload["authority_kind"], AuthorityKind)
    a_name = _string(payload["authority_name"])
    auth_eff = tuple(_string(e) for e in _items(payload["authorized_effect_classes"]))
    forb_eff = tuple(_string(e) for e in _items(payload["forbidden_effect_classes"]))
    stop = _string(payload["stop_boundary"])
    a_status = _enum(payload["status"], AuthorityStatus)
    att_raw = payload["attestation_ref"]
    att_ref = _string(att_raw) if att_raw is not None else None

    expected_id = compute_authority_receipt_id(
        task_id=task_id,
        intent_digest=intent_dgst,
        subject_sha=subject_sha,
        authority_kind=a_kind,
        authority_name=a_name,
        authorized_effect_classes=auth_eff,
        forbidden_effect_classes=forb_eff,
        stop_boundary=stop,
        status=a_status,
        attestation_ref=att_ref,
    )
    if expected_id != payload["receipt_id"]:
        _fail("TAMPERED_RECEIPT_ID")

    return ExecutionAuthorityReceipt(
        schema_version=AUTHORITY_RECEIPT_SCHEMA_VERSION,
        receipt_id=expected_id,
        task_id=task_id,
        intent_digest=intent_dgst,
        subject_sha=subject_sha,
        authority_kind=a_kind,
        authority_name=a_name,
        authorized_effect_classes=auth_eff,
        forbidden_effect_classes=forb_eff,
        stop_boundary=stop,
        status=a_status,
        attestation_ref=att_ref,
    )


def validate_authority_receipt(
    value: "ExecutionAuthorityReceipt | Mapping[str, object]",
) -> ExecutionAuthorityReceipt:
    if isinstance(value, ExecutionAuthorityReceipt):
        payload: Mapping[str, object] = {
            "task_id": value.task_id,
            "intent_digest": value.intent_digest,
            "subject_sha": value.subject_sha,
            "authority_kind": value.authority_kind.value,
            "authority_name": value.authority_name,
            "authorized_effect_classes": list(value.authorized_effect_classes),
            "forbidden_effect_classes": list(value.forbidden_effect_classes),
            "stop_boundary": value.stop_boundary,
            "status": value.status.value,
            "attestation_ref": value.attestation_ref,
            "receipt_id": value.receipt_id,
        }
    else:
        if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
            _fail("REQUIRED_FIELD_MISSING")
        raw_version = value.get("schema_version")
        if raw_version != AUTHORITY_RECEIPT_SCHEMA_VERSION or isinstance(raw_version, bool):
            _fail("UNKNOWN_RECEIPT_SCHEMA_VERSION")
        payload = _exact_fields(value, _AUTHORITY_RECEIPT_FIELDS)
    return _authority_from_mapping(payload)


def deserialize_authority_receipt(
    value: "Mapping[str, object] | str | bytes",
) -> ExecutionAuthorityReceipt:
    import json as _json
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReceiptError("JSON_INVALID") from exc
    if isinstance(value, str):
        try:
            data = _json.loads(value)
        except Exception as exc:
            raise ReceiptError("JSON_INVALID") from exc
        return validate_authority_receipt(data)
    if isinstance(value, Mapping):
        return validate_authority_receipt(value)
    _fail("REQUIRED_FIELD_MISSING")


def serialize_authority_receipt(receipt: ExecutionAuthorityReceipt) -> dict[str, Any]:
    return {
        "schema_version": receipt.schema_version,
        "receipt_id": receipt.receipt_id,
        "task_id": receipt.task_id,
        "intent_digest": receipt.intent_digest,
        "subject_sha": receipt.subject_sha,
        "authority_kind": receipt.authority_kind.value,
        "authority_name": receipt.authority_name,
        "authorized_effect_classes": list(receipt.authorized_effect_classes),
        "forbidden_effect_classes": list(receipt.forbidden_effect_classes),
        "stop_boundary": receipt.stop_boundary,
        "status": receipt.status.value,
        "attestation_ref": receipt.attestation_ref,
    }


# ---------------------------------------------------------------------------
# EvidenceCollectionReceipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceCollectionReceipt:
    schema_version: int
    receipt_id: str  # content-bound digest
    task_id: str
    intent_digest: str
    subject_sha: str
    producer_id: str
    artifact_ref: str
    artifact_digest: str
    collection_mode: str  # e.g. "READ_ONLY_WSL_SESSION", "OFFLINE_REPLAY"
    authority_receipt_id: str | None  # links to ExecutionAuthorityReceipt if applicable


_COLLECTION_RECEIPT_FIELDS = frozenset({
    "schema_version",
    "receipt_id",
    "task_id",
    "intent_digest",
    "subject_sha",
    "producer_id",
    "artifact_ref",
    "artifact_digest",
    "collection_mode",
    "authority_receipt_id",
})


def compute_collection_receipt_id(
    task_id: str,
    intent_digest: str,
    subject_sha: str,
    producer_id: str,
    artifact_ref: str,
    artifact_digest: str,
    collection_mode: str,
    authority_receipt_id: str | None,
    schema_version: int = COLLECTION_RECEIPT_SCHEMA_VERSION,
) -> str:
    return _compute_digest({
        "artifact_digest": str(artifact_digest),
        "artifact_ref": str(artifact_ref),
        "authority_receipt_id": authority_receipt_id,
        "collection_mode": str(collection_mode),
        "intent_digest": str(intent_digest),
        "producer_id": str(producer_id),
        "schema_version": schema_version,
        "subject_sha": str(subject_sha),
        "task_id": str(task_id),
    })


def create_collection_receipt(
    task_id: str,
    intent_digest: str,
    subject_sha: str,
    producer_id: str,
    artifact_ref: str,
    artifact_digest: str,
    collection_mode: str,
    authority_receipt_id: str | None = None,
) -> EvidenceCollectionReceipt:
    """Public factory for EvidenceCollectionReceipt with deterministic receipt_id."""
    t_id = _identifier(task_id)
    i_dgst = _digest(intent_digest)
    s_sha = _sha(subject_sha)
    p_id = _string(producer_id)
    a_ref = _string(artifact_ref)
    a_dgst = _digest(artifact_digest)
    mode = _string(collection_mode)
    auth_rid = _digest(authority_receipt_id) if authority_receipt_id is not None else None

    receipt_id = compute_collection_receipt_id(
        task_id=t_id,
        intent_digest=i_dgst,
        subject_sha=s_sha,
        producer_id=p_id,
        artifact_ref=a_ref,
        artifact_digest=a_dgst,
        collection_mode=mode,
        authority_receipt_id=auth_rid,
    )
    receipt = EvidenceCollectionReceipt(
        schema_version=COLLECTION_RECEIPT_SCHEMA_VERSION,
        receipt_id=receipt_id,
        task_id=t_id,
        intent_digest=i_dgst,
        subject_sha=s_sha,
        producer_id=p_id,
        artifact_ref=a_ref,
        artifact_digest=a_dgst,
        collection_mode=mode,
        authority_receipt_id=auth_rid,
    )
    return validate_collection_receipt(receipt)


def _collection_from_mapping(payload: Mapping[str, object]) -> EvidenceCollectionReceipt:
    task_id = _identifier(payload["task_id"])
    intent_dgst = _digest(payload["intent_digest"])
    subject_sha = _sha(payload["subject_sha"])
    producer_id = _string(payload["producer_id"])
    artifact_ref = _string(payload["artifact_ref"])
    artifact_digest = _digest(payload["artifact_digest"])
    collection_mode = _string(payload["collection_mode"])
    auth_raw = payload["authority_receipt_id"]
    auth_rid = _digest(auth_raw) if auth_raw is not None else None

    expected_id = compute_collection_receipt_id(
        task_id=task_id,
        intent_digest=intent_dgst,
        subject_sha=subject_sha,
        producer_id=producer_id,
        artifact_ref=artifact_ref,
        artifact_digest=artifact_digest,
        collection_mode=collection_mode,
        authority_receipt_id=auth_rid,
    )
    if expected_id != payload["receipt_id"]:
        _fail("TAMPERED_RECEIPT_ID")

    return EvidenceCollectionReceipt(
        schema_version=COLLECTION_RECEIPT_SCHEMA_VERSION,
        receipt_id=expected_id,
        task_id=task_id,
        intent_digest=intent_dgst,
        subject_sha=subject_sha,
        producer_id=producer_id,
        artifact_ref=artifact_ref,
        artifact_digest=artifact_digest,
        collection_mode=collection_mode,
        authority_receipt_id=auth_rid,
    )


def validate_collection_receipt(
    value: "EvidenceCollectionReceipt | Mapping[str, object]",
) -> EvidenceCollectionReceipt:
    if isinstance(value, EvidenceCollectionReceipt):
        payload: Mapping[str, object] = {
            "task_id": value.task_id,
            "intent_digest": value.intent_digest,
            "subject_sha": value.subject_sha,
            "producer_id": value.producer_id,
            "artifact_ref": value.artifact_ref,
            "artifact_digest": value.artifact_digest,
            "collection_mode": value.collection_mode,
            "authority_receipt_id": value.authority_receipt_id,
            "receipt_id": value.receipt_id,
        }
    else:
        if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
            _fail("REQUIRED_FIELD_MISSING")
        raw_version = value.get("schema_version")
        if raw_version != COLLECTION_RECEIPT_SCHEMA_VERSION or isinstance(raw_version, bool):
            _fail("UNKNOWN_RECEIPT_SCHEMA_VERSION")
        payload = _exact_fields(value, _COLLECTION_RECEIPT_FIELDS)
    return _collection_from_mapping(payload)


def deserialize_collection_receipt(
    value: "Mapping[str, object] | str | bytes",
) -> EvidenceCollectionReceipt:
    import json as _json
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReceiptError("JSON_INVALID") from exc
    if isinstance(value, str):
        try:
            data = _json.loads(value)
        except Exception as exc:
            raise ReceiptError("JSON_INVALID") from exc
        return validate_collection_receipt(data)
    if isinstance(value, Mapping):
        return validate_collection_receipt(value)
    _fail("REQUIRED_FIELD_MISSING")


def serialize_collection_receipt(receipt: EvidenceCollectionReceipt) -> dict[str, Any]:
    return {
        "schema_version": receipt.schema_version,
        "receipt_id": receipt.receipt_id,
        "task_id": receipt.task_id,
        "intent_digest": receipt.intent_digest,
        "subject_sha": receipt.subject_sha,
        "producer_id": receipt.producer_id,
        "artifact_ref": receipt.artifact_ref,
        "artifact_digest": receipt.artifact_digest,
        "collection_mode": receipt.collection_mode,
        "authority_receipt_id": receipt.authority_receipt_id,
    }
