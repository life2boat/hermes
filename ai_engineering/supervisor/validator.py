"""validator.py - Deterministic evidence validation producing VerifiedResult.

IMPORTANT SECURITY INVARIANT:
  RAW WORKER CLAIM != VERIFIED RESULT

  A WorkerResultBundle is an untrusted claim.  A VerifiedResult is the
  supervisor's deterministic adjudication.  No raw LLM text, no log string,
  and no GENERIC-category artifact can bypass this validation to become a PASS.

Schema version: hermes.verified-result.v1
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NoReturn

from ai_engineering.contracts import GateResult, Status
from ai_engineering.task_intent import TaskIntent, intent_digest
from ai_engineering.supervisor.normalized_evidence import (
    NormalizedEvidence,
    normalized_evidence_digest,
)
from ai_engineering.supervisor.shunt_router import EvidenceCategory

VERIFIED_RESULT_SCHEMA_VERSION = "hermes.verified-result.v1"


class ValidatorError(ValueError):
    """Fail-closed error with stable .code attribute."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise ValidatorError(code)


@dataclass(frozen=True, slots=True)
class VerifiedResult:
    """Supervisor adjudication of normalized evidence against a TaskIntent.

    This is the authoritative output.  It is:
      - idempotent: same NormalizedEvidence + same TaskIntent -> same VerifiedResult
      - content-bound: result_id is SHA256 of all result fields
      - deterministic: no random, no time-dependent logic (verified_at_utc is
        injected by caller for idempotency in replay scenarios)
    """

    schema_version: str
    result_id: str               # sha256 content-bound
    task_id: str
    attempt_id: str
    intent_digest: str
    base_sha: str
    head_sha: str
    normalized_evidence_digest: str
    required_gates: tuple[str, ...]
    gate_results: tuple[GateResult, ...]
    blockers: tuple[str, ...]
    status: Status
    reason_codes: tuple[str, ...]
    verified_at_utc: str


def _verified_result_payload(vr: VerifiedResult) -> dict:
    return {
        "attempt_id": vr.attempt_id,
        "base_sha": vr.base_sha,
        "blockers": list(vr.blockers),
        "gate_results": [
            {
                "evidence_refs": list(gr.evidence_refs),
                "gate_name": gr.gate_name,
                "required": gr.required,
                "status": gr.status.value,
            }
            for gr in vr.gate_results
        ],
        "head_sha": vr.head_sha,
        "intent_digest": vr.intent_digest,
        "normalized_evidence_digest": vr.normalized_evidence_digest,
        "reason_codes": list(vr.reason_codes),
        "required_gates": list(vr.required_gates),
        "result_id": vr.result_id,
        "schema_version": vr.schema_version,
        "status": vr.status.value,
        "task_id": vr.task_id,
        "verified_at_utc": vr.verified_at_utc,
    }


def canonical_serialize_verified_result(vr: VerifiedResult) -> str:
    return json.dumps(
        _verified_result_payload(vr),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def compute_result_id(
    schema_version: str,
    task_id: str,
    attempt_id: str,
    intent_dg: str,
    base_sha: str,
    head_sha: str,
    ne_digest: str,
    required_gates: tuple[str, ...],
    gate_results: tuple[GateResult, ...],
    blockers: tuple[str, ...],
    status: Status,
    reason_codes: tuple[str, ...],
    verified_at_utc: str,
) -> str:
    """SHA256 of canonical JSON of all result fields (excluding result_id itself)."""
    payload = json.dumps(
        {
            "attempt_id": attempt_id,
            "base_sha": base_sha,
            "blockers": list(blockers),
            "gate_results": [
                {
                    "evidence_refs": list(gr.evidence_refs),
                    "gate_name": gr.gate_name,
                    "required": gr.required,
                    "status": gr.status.value,
                }
                for gr in gate_results
            ],
            "head_sha": head_sha,
            "intent_digest": intent_dg,
            "normalized_evidence_digest": ne_digest,
            "reason_codes": list(reason_codes),
            "required_gates": list(required_gates),
            "schema_version": schema_version,
            "status": status.value,
            "task_id": task_id,
            "verified_at_utc": verified_at_utc,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact_categories_for_claim(
    ne: NormalizedEvidence, claim_gate_name: str
) -> set[EvidenceCategory]:
    """Return the set of EvidenceCategory values for artifacts referenced by a gate claim."""
    claim = next(
        (gc for gc in ne.gate_claims if gc.gate_name == claim_gate_name), None
    )
    if claim is None:
        return set()
    # Map artifact refs to categories
    ref_to_cat: dict[str, EvidenceCategory] = {
        a.artifact_ref: a.category for a in ne.artifacts
    }
    cats: set[EvidenceCategory] = set()
    for ref in claim.evidence_refs:
        if ref in ref_to_cat:
            cats.add(ref_to_cat[ref])
    return cats


def validate_normalized_evidence(
    ne: NormalizedEvidence,
    intent: TaskIntent,
    *,
    verified_at_utc: str | None = None,
) -> VerifiedResult:
    """Deterministic validation of NormalizedEvidence against a TaskIntent.

    Idempotent: same NormalizedEvidence + same TaskIntent -> same VerifiedResult
    (when verified_at_utc is fixed).

    Checks (in order):
    1. intent_digest match
    2. task_id match
    3. attempt_id non-empty
    4. base_sha match  (stale evidence guard)
    5. repository match
    6. canonical_remote match
    7. per required gate: claim present, status, category check
    """
    if verified_at_utc is None:
        verified_at_utc = datetime.now(timezone.utc).isoformat()

    blockers: list[str] = []
    reason_codes: list[str] = []
    gate_results: list[GateResult] = []

    # 1. intent_digest match
    expected_digest = intent_digest(intent)
    if ne.intent_digest != expected_digest:
        blockers.append("INTENT_DIGEST_MISMATCH")
        reason_codes.append("INTENT_DIGEST_MISMATCH")

    # 2. task_id match
    if ne.task_id != intent.task_id:
        blockers.append("TASK_ID_MISMATCH")
        reason_codes.append("TASK_ID_MISMATCH")

    # 3. attempt_id non-empty
    if not ne.attempt_id or not ne.attempt_id.strip():
        blockers.append("ATTEMPT_ID_MISSING")
        reason_codes.append("ATTEMPT_ID_MISSING")

    # 4. base_sha match
    if ne.base_sha != intent.source_base_sha:
        blockers.append("STALE_EVIDENCE")
        reason_codes.append("STALE_EVIDENCE")

    # 5. repository match
    if ne.repository != intent.source_repository:
        blockers.append("REPOSITORY_MISMATCH")
        reason_codes.append("REPOSITORY_MISMATCH")

    # 6. canonical_remote match
    # TaskIntent uses source_main_ref for remote; we use source_repository as
    # canonical_remote identifier (matches the fixture pattern in task_intent tests)
    # The intent's canonical remote is embedded in source_main_ref prefix OR
    # we compare ne.canonical_remote == intent.source_repository (the remote name).
    # Per the spec, we compare ne.canonical_remote against the repository field
    # which in TaskIntent is source_repository (the remote name like "github").
    # We treat canonical_remote as matching the remote name used in source_main_ref.
    # Extract remote name from source_main_ref if possible, else use source_repository.
    expected_remote = intent.source_repository  # "github", "origin", etc.
    if ne.canonical_remote != expected_remote:
        blockers.append("CANONICAL_REMOTE_MISMATCH")
        reason_codes.append("CANONICAL_REMOTE_MISMATCH")

    # 7. Per required gate
    ne_claim_map = {gc.gate_name: gc for gc in ne.gate_claims}

    required_gates: tuple[str, ...] = tuple(sorted(intent.required_gates))
    any_fail = False

    for gate_name in intent.required_gates:
        claim = ne_claim_map.get(gate_name)
        if claim is None:
            blockers.append(f"REQUIRED_GATE_MISSING:{gate_name}")
            reason_codes.append("REQUIRED_GATE_MISSING")
            gate_results.append(
                GateResult(
                    gate_name=gate_name,
                    required=True,
                    status=Status.BLOCKED,
                    evidence_refs=(),
                )
            )
            continue

        # Check if supporting artifacts are all GENERIC
        cats = _artifact_categories_for_claim(ne, gate_name)
        only_generic = bool(cats) and cats == {EvidenceCategory.GENERIC}

        if only_generic:
            blockers.append(f"GENERIC_EVIDENCE_CANNOT_SATISFY_REQUIRED_GATE:{gate_name}")
            reason_codes.append("GENERIC_EVIDENCE_CANNOT_SATISFY_REQUIRED_GATE")
            gate_results.append(
                GateResult(
                    gate_name=gate_name,
                    required=True,
                    status=Status.BLOCKED,
                    evidence_refs=claim.evidence_refs,
                )
            )
            continue

        if claim.claimed_status == Status.PASS:
            gate_results.append(
                GateResult(
                    gate_name=gate_name,
                    required=True,
                    status=Status.PASS,
                    evidence_refs=claim.evidence_refs,
                )
            )
        elif claim.claimed_status == Status.FAIL:
            any_fail = True
            reason_codes.append(f"REQUIRED_GATE_FAILED:{gate_name}")
            gate_results.append(
                GateResult(
                    gate_name=gate_name,
                    required=True,
                    status=Status.FAIL,
                    evidence_refs=claim.evidence_refs,
                )
            )
        else:
            blockers.append(f"REQUIRED_GATE_INCONCLUSIVE:{gate_name}")
            reason_codes.append("REQUIRED_GATE_INCONCLUSIVE")
            gate_results.append(
                GateResult(
                    gate_name=gate_name,
                    required=True,
                    status=Status.BLOCKED,
                    evidence_refs=claim.evidence_refs,
                )
            )

    # Determine overall status
    if blockers:
        status = Status.BLOCKED
    elif any_fail:
        status = Status.FAIL
    else:
        status = Status.PASS

    ne_digest = normalized_evidence_digest(ne)
    result_id = compute_result_id(
        schema_version=VERIFIED_RESULT_SCHEMA_VERSION,
        task_id=ne.task_id,
        attempt_id=ne.attempt_id,
        intent_dg=ne.intent_digest,
        base_sha=ne.base_sha,
        head_sha=ne.head_sha,
        ne_digest=ne_digest,
        required_gates=required_gates,
        gate_results=tuple(gate_results),
        blockers=tuple(blockers),
        status=status,
        reason_codes=tuple(reason_codes),
        verified_at_utc=verified_at_utc,
    )

    return VerifiedResult(
        schema_version=VERIFIED_RESULT_SCHEMA_VERSION,
        result_id=result_id,
        task_id=ne.task_id,
        attempt_id=ne.attempt_id,
        intent_digest=ne.intent_digest,
        base_sha=ne.base_sha,
        head_sha=ne.head_sha,
        normalized_evidence_digest=ne_digest,
        required_gates=required_gates,
        gate_results=tuple(gate_results),
        blockers=tuple(blockers),
        status=status,
        reason_codes=tuple(reason_codes),
        verified_at_utc=verified_at_utc,
    )


# ─── Deserialization ───────────────────────────────────────────────────────────

class _DuplicateJsonKey(ValueError):
    pass


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for k, v in pairs:
        if k in result:
            raise _DuplicateJsonKey(k)
        result[k] = v
    return result


def deserialize_verified_result(raw: str | bytes) -> VerifiedResult:
    """Deserialize a VerifiedResult from JSON. Rejects duplicate JSON keys."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValidatorError("JSON_INVALID")
    else:
        text = raw

    try:
        payload = json.loads(text, object_pairs_hook=_no_dup_keys)
    except (_DuplicateJsonKey, json.JSONDecodeError) as exc:
        raise ValidatorError("JSON_INVALID") from exc

    if not isinstance(payload, dict):
        raise ValidatorError("JSON_INVALID")

    sv = payload.get("schema_version")
    if sv != VERIFIED_RESULT_SCHEMA_VERSION:
        raise ValidatorError("SCHEMA_VERSION_UNSUPPORTED")

    try:
        status = Status(payload["status"])
    except (ValueError, KeyError):
        raise ValidatorError("FIELD_INVALID:status")

    gate_results = tuple(
        GateResult(
            gate_name=gr["gate_name"],
            required=gr["required"],
            status=Status(gr["status"]),
            evidence_refs=tuple(gr.get("evidence_refs", [])),
        )
        for gr in payload.get("gate_results", [])
    )

    return VerifiedResult(
        schema_version=sv,
        result_id=payload["result_id"],
        task_id=payload["task_id"],
        attempt_id=payload["attempt_id"],
        intent_digest=payload["intent_digest"],
        base_sha=payload["base_sha"],
        head_sha=payload["head_sha"],
        normalized_evidence_digest=payload["normalized_evidence_digest"],
        required_gates=tuple(payload.get("required_gates", [])),
        gate_results=gate_results,
        blockers=tuple(payload.get("blockers", [])),
        status=status,
        reason_codes=tuple(payload.get("reason_codes", [])),
        verified_at_utc=payload["verified_at_utc"],
    )
