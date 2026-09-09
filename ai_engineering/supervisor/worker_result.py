"""worker_result.py - RAW untrusted input from a worker.

Schema version: hermes.worker-result.v1

All fields are fail-closed validated.  The data received here is *untrusted* -
it has been produced by an autonomous agent and must be verified before any
gate decisions are made.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from ai_engineering.contracts import Status
from ai_engineering.redaction import reject_forbidden_raw_fields

WORKER_RESULT_SCHEMA_VERSION = "hermes.worker-result.v1"

# ── Resource bounds ─────────────────────────────────────────────────────────────
MAX_ARTIFACTS = 50
MAX_ARTIFACT_SIZE_BYTES = 100 * 1024 * 1024   # 100 MB
MAX_TOTAL_SIZE_BYTES = 500 * 1024 * 1024       # 500 MB
MAX_MANIFEST_BYTES = 512 * 1024               # 512 KB


class WorkerResultError(ValueError):
    """Fail-closed error that exposes a stable .code attribute."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise WorkerResultError(code)


# ── Path-safety (mirrors evidence_verifier._safe_resolve logic) ──────────────────

def _validate_relative_path(path: str) -> None:
    """Raise WorkerResultError for any path that would escape the evidence root."""
    if any(ord(c) < 32 for c in path):
        _fail("PATH_TRAVERSAL_FORBIDDEN")

    # Reject UNC paths (\\\\ or //) BEFORE the generic POSIX absolute check
    # so that //server/share gets UNC_PATH_FORBIDDEN, not ABSOLUTE_PATH_FORBIDDEN
    if path.startswith("\\\\") or path.startswith("//"):
        _fail("UNC_PATH_FORBIDDEN")

    # Reject absolute POSIX paths (single leading /)
    if posixpath.isabs(path):
        _fail("ABSOLUTE_PATH_FORBIDDEN")

    # Reject Windows drive paths (e.g. C:\ or C:/)
    if len(path) >= 2 and path[1] == ":":
        _fail("DRIVE_PATH_FORBIDDEN")

    # Reject traversal components
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        _fail("PATH_TRAVERSAL_FORBIDDEN")


# ── Domain objects ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ArtifactManifestEntry:
    artifact_type: str
    relative_path: str
    sha256: str
    size_bytes: int
    producer_note: str


@dataclass(frozen=True, slots=True)
class GateClaim:
    gate_name: str
    claimed_status: Status
    evidence_refs: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True, slots=True)
class WorkerResultBundle:
    """RAW, untrusted payload produced by an autonomous worker.

    A WorkerResultBundle is NOT a VerifiedResult.  It is raw input that must
    be validated, path-checked, digest-verified and normalized before any gate
    decision can be made.
    """

    schema_version: str
    result_id: str
    task_id: str
    attempt_id: str
    worker_id: str
    base_sha: str
    head_sha: str
    canonical_remote: str
    repository: str
    intent_digest: str
    produced_at_utc: str
    artifacts: tuple[ArtifactManifestEntry, ...]
    gate_claims: tuple[GateClaim, ...]


# ── Deserialization helpers ──────────────────────────────────────────────────────

def _string(value: object, code: str = "VALUE_INVALID") -> str:
    if not isinstance(value, str):
        _fail(code)
    return value  # type: ignore[return-value]


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("VALUE_INVALID")
    return value  # type: ignore[return-value]


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        _fail("WORKER_RESULT_INVALID")
    return value  # type: ignore[return-value]


class _DuplicateJsonKey(ValueError):
    pass


def _object_pairs_no_dup(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for k, v in pairs:
        if k in result:
            raise _DuplicateJsonKey(k)
        result[k] = v
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("json constant not allowed")


def _parse_artifact(raw: object) -> ArtifactManifestEntry:
    m = _mapping(raw)
    required = {"artifact_type", "relative_path", "sha256", "size_bytes", "producer_note"}
    if required - frozenset(m):
        _fail("WORKER_RESULT_INVALID")
    if frozenset(m) - required:
        _fail("WORKER_RESULT_INVALID")

    artifact_type = _string(m["artifact_type"])
    relative_path = _string(m["relative_path"])
    sha256 = _string(m["sha256"])
    raw_size = m["size_bytes"]
    if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
        _fail("VALUE_INVALID")
    producer_note = _string(m["producer_note"])

    # Path safety validation
    _validate_relative_path(relative_path)

    return ArtifactManifestEntry(
        artifact_type=artifact_type,
        relative_path=relative_path,
        sha256=sha256,
        size_bytes=raw_size,
        producer_note=producer_note,
    )


def _parse_gate_claim(raw: object) -> GateClaim:
    m = _mapping(raw)
    required = {"gate_name", "claimed_status", "evidence_refs", "reason_code"}
    if required - frozenset(m):
        _fail("WORKER_RESULT_INVALID")
    if frozenset(m) - required:
        _fail("WORKER_RESULT_INVALID")

    gate_name = _string(m["gate_name"])
    raw_status = m["claimed_status"]
    if not isinstance(raw_status, str):
        _fail("VALUE_INVALID")
    try:
        claimed_status = Status(raw_status)
    except ValueError:
        _fail("VALUE_INVALID")

    raw_refs = _items(m["evidence_refs"])
    evidence_refs = tuple(_string(r) for r in raw_refs)
    reason_code = _string(m["reason_code"])

    return GateClaim(
        gate_name=gate_name,
        claimed_status=claimed_status,  # type: ignore[arg-type]
        evidence_refs=evidence_refs,
        reason_code=reason_code,
    )


def _parse_bundle(payload: Mapping[str, object]) -> WorkerResultBundle:
    """Parse and validate a mapping into a WorkerResultBundle."""
    required_fields = {
        "schema_version", "result_id", "task_id", "attempt_id",
        "worker_id", "base_sha", "head_sha", "canonical_remote",
        "repository", "intent_digest", "produced_at_utc",
        "artifacts", "gate_claims",
    }
    if required_fields - frozenset(payload):
        _fail("WORKER_RESULT_INVALID")
    if frozenset(payload) - required_fields:
        _fail("WORKER_RESULT_INVALID")

    schema_version = _string(payload["schema_version"])
    if schema_version != WORKER_RESULT_SCHEMA_VERSION:
        _fail("SCHEMA_VERSION_UNSUPPORTED")

    result_id = _string(payload["result_id"])
    task_id = _string(payload["task_id"])
    attempt_id = _string(payload["attempt_id"])
    worker_id = _string(payload["worker_id"])
    base_sha = _string(payload["base_sha"])
    head_sha = _string(payload["head_sha"])
    canonical_remote = _string(payload["canonical_remote"])
    repository = _string(payload["repository"])
    intent_digest_val = _string(payload["intent_digest"])
    produced_at_utc = _string(payload["produced_at_utc"])

    raw_artifacts = _items(payload["artifacts"])
    if len(raw_artifacts) > MAX_ARTIFACTS:
        _fail("ARTIFACT_COUNT_EXCEEDED")

    artifacts_list: list[ArtifactManifestEntry] = []
    total_size = 0
    for raw_a in raw_artifacts:
        entry = _parse_artifact(raw_a)
        if entry.size_bytes > MAX_ARTIFACT_SIZE_BYTES:
            _fail("ARTIFACT_TOO_LARGE")
        total_size += entry.size_bytes
        if total_size > MAX_TOTAL_SIZE_BYTES:
            _fail("TOTAL_SIZE_EXCEEDED")
        artifacts_list.append(entry)

    raw_claims = _items(payload["gate_claims"])
    gate_claims_list: list[GateClaim] = []
    for raw_c in raw_claims:
        gate_claims_list.append(_parse_gate_claim(raw_c))

    # Secret redaction check on producer_note metadata
    for entry in artifacts_list:
        try:
            reject_forbidden_raw_fields({"producer_note": entry.producer_note})
        except Exception:
            _fail("WORKER_RESULT_INVALID")

    return WorkerResultBundle(
        schema_version=schema_version,
        result_id=result_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worker_id=worker_id,
        base_sha=base_sha,
        head_sha=head_sha,
        canonical_remote=canonical_remote,
        repository=repository,
        intent_digest=intent_digest_val,
        produced_at_utc=produced_at_utc,
        artifacts=tuple(artifacts_list),
        gate_claims=tuple(gate_claims_list),
    )


def deserialize_worker_result(raw: str | bytes) -> WorkerResultBundle:
    """Fail-closed JSON parsing with duplicate key rejection."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkerResultError("JSON_INVALID") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        _fail("JSON_INVALID")

    # Manifest size bound
    raw_bytes: bytes = text.encode("utf-8") if isinstance(raw, str) else raw
    if len(raw_bytes) > MAX_MANIFEST_BYTES:
        _fail("MANIFEST_TOO_LARGE")

    if not text.strip():
        _fail("JSON_INVALID")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=_object_pairs_no_dup,
            parse_constant=_reject_json_constant,
        )
    except (_DuplicateJsonKey, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise WorkerResultError("JSON_INVALID") from exc

    if not isinstance(payload, dict):
        _fail("JSON_INVALID")

    return _parse_bundle(payload)


def validate_worker_result(bundle: WorkerResultBundle) -> WorkerResultBundle:
    """Re-validate an already-parsed bundle (idempotent)."""
    for entry in bundle.artifacts:
        _validate_relative_path(entry.relative_path)
    return bundle


# ── Canonical serialization ────────────────────────────────────────────────────

def _bundle_to_dict(bundle: WorkerResultBundle) -> dict[str, Any]:
    return {
        "artifacts": [
            {
                "artifact_type": a.artifact_type,
                "producer_note": a.producer_note,
                "relative_path": a.relative_path,
                "sha256": a.sha256,
                "size_bytes": a.size_bytes,
            }
            for a in bundle.artifacts
        ],
        "attempt_id": bundle.attempt_id,
        "base_sha": bundle.base_sha,
        "canonical_remote": bundle.canonical_remote,
        "gate_claims": [
            {
                "claimed_status": gc.claimed_status.value,
                "evidence_refs": list(gc.evidence_refs),
                "gate_name": gc.gate_name,
                "reason_code": gc.reason_code,
            }
            for gc in bundle.gate_claims
        ],
        "head_sha": bundle.head_sha,
        "intent_digest": bundle.intent_digest,
        "produced_at_utc": bundle.produced_at_utc,
        "repository": bundle.repository,
        "result_id": bundle.result_id,
        "schema_version": bundle.schema_version,
        "task_id": bundle.task_id,
        "worker_id": bundle.worker_id,
    }


def canonical_serialize_worker_result(bundle: WorkerResultBundle) -> str:
    return json.dumps(
        _bundle_to_dict(bundle),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def worker_result_digest(bundle: WorkerResultBundle) -> str:
    return hashlib.sha256(
        canonical_serialize_worker_result(bundle).encode("utf-8")
    ).hexdigest()
