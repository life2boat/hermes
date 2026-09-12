"""Context pack for Astra decision provider (hermes.context-pack.v1)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import NoReturn

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from ai_engineering.redaction import reject_forbidden_raw_fields
from ai_engineering.task_intent import TaskIntent
from ai_engineering.supervisor.state import SupervisorError, SupervisorState, _fail
from ai_engineering.supervisor.validator import VerifiedResult

CONTEXT_PACK_SCHEMA_VERSION = "hermes.context-pack.v1"
MAX_CONTEXT_PACK_BYTES = 64 * 1024  # 64 KiB

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class _DuplicateJsonKey(ValueError):
    pass


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for k, v in pairs:
        if k in result:
            raise _DuplicateJsonKey(k)
        result[k] = v
    return result


@dataclass(frozen=True, slots=True)
class GateResultSummary:
    gate_name: str
    required: bool
    status: str  # Status.value


@dataclass(frozen=True, slots=True)
class VerifiedResultSummary:
    result_id: str
    status: str
    blockers: tuple[str, ...]
    reason_codes: tuple[str, ...]
    gate_results: tuple[GateResultSummary, ...]
    base_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class ContextPack:
    schema_version: str
    pack_id: str                    # content-bound digest
    run_id: str
    root_goal: str
    repository: str
    canonical_remote: str
    canonical_main_ref: str
    current_task_id: str
    current_intent_digest: str
    current_intent_revision: int
    current_attempt_id: str
    attempt_number: int
    desired_outcome: str
    task_class: str
    stop_boundary: str
    latest_verified_result: VerifiedResultSummary | None
    phase: str
    current_base_sha: str
    state_revision: int
    known_blockers: tuple[str, ...]


def _vrs_to_dict(vrs: VerifiedResultSummary) -> dict:
    return {
        "base_sha": vrs.base_sha,
        "blockers": list(vrs.blockers),
        "gate_results": [
            {
                "gate_name": gr.gate_name,
                "required": gr.required,
                "status": gr.status,
            }
            for gr in vrs.gate_results
        ],
        "head_sha": vrs.head_sha,
        "reason_codes": list(vrs.reason_codes),
        "result_id": vrs.result_id,
        "status": vrs.status,
    }


def _pack_to_dict(pack: ContextPack) -> dict:
    return {
        "attempt_number": pack.attempt_number,
        "canonical_main_ref": pack.canonical_main_ref,
        "canonical_remote": pack.canonical_remote,
        "current_attempt_id": pack.current_attempt_id,
        "current_base_sha": pack.current_base_sha,
        "current_intent_digest": pack.current_intent_digest,
        "current_intent_revision": pack.current_intent_revision,
        "current_task_id": pack.current_task_id,
        "desired_outcome": pack.desired_outcome,
        "known_blockers": list(pack.known_blockers),
        "latest_verified_result": _vrs_to_dict(pack.latest_verified_result) if pack.latest_verified_result else None,
        "pack_id": pack.pack_id,
        "phase": pack.phase,
        "repository": pack.repository,
        "root_goal": pack.root_goal,
        "run_id": pack.run_id,
        "schema_version": pack.schema_version,
        "state_revision": pack.state_revision,
        "stop_boundary": pack.stop_boundary,
        "task_class": pack.task_class,
    }


def canonical_serialize_context_pack(pack: ContextPack) -> str:
    return json.dumps(
        _pack_to_dict(pack),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def context_pack_digest(pack: ContextPack) -> str:
    return hashlib.sha256(canonical_serialize_context_pack(pack).encode("utf-8")).hexdigest()


def _compute_pack_id(pack_dict_without_id: dict) -> str:
    raw = json.dumps(pack_dict_without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_context_pack(
    state: SupervisorState,
    intent: TaskIntent,
    verified_result: VerifiedResult | None = None,
) -> ContextPack:
    """Build a ContextPack from supervisor state and task intent.

    Applies redaction checks on root_goal and desired_outcome.
    Enforces MAX_CONTEXT_PACK_BYTES limit.
    """
    # Apply redaction checks
    reject_forbidden_raw_fields({"root_goal": state.root_goal})
    reject_forbidden_raw_fields({"desired_outcome": intent.desired_outcome})

    vrs: VerifiedResultSummary | None = None
    if verified_result is not None:
        vrs = VerifiedResultSummary(
            result_id=verified_result.result_id,
            status=verified_result.status.value,
            blockers=verified_result.blockers,
            reason_codes=verified_result.reason_codes,
            gate_results=tuple(
                GateResultSummary(
                    gate_name=gr.gate_name,
                    required=gr.required,
                    status=gr.status.value,
                )
                for gr in verified_result.gate_results
            ),
            base_sha=verified_result.base_sha,
            head_sha=verified_result.head_sha,
        )

    # Compute pack_id without it being in the dict
    proto_dict = {
        "attempt_number": state.attempt_number,
        "canonical_main_ref": state.canonical_main_ref,
        "canonical_remote": state.canonical_remote,
        "current_attempt_id": state.current_attempt_id,
        "current_base_sha": state.current_base_sha,
        "current_intent_digest": state.current_intent_digest,
        "current_intent_revision": state.current_intent_revision,
        "current_task_id": state.current_task_id,
        "desired_outcome": intent.desired_outcome,
        "known_blockers": list(state.blockers),
        "latest_verified_result": _vrs_to_dict(vrs) if vrs else None,
        "phase": state.phase.value,
        "repository": state.repository,
        "root_goal": state.root_goal,
        "run_id": state.run_id,
        "schema_version": CONTEXT_PACK_SCHEMA_VERSION,
        "state_revision": state.state_revision,
        "stop_boundary": intent.stop_boundary.value,
        "task_class": intent.task_class.value,
    }
    pack_id = _compute_pack_id(proto_dict)

    pack = ContextPack(
        schema_version=CONTEXT_PACK_SCHEMA_VERSION,
        pack_id=pack_id,
        run_id=state.run_id,
        root_goal=state.root_goal,
        repository=state.repository,
        canonical_remote=state.canonical_remote,
        canonical_main_ref=state.canonical_main_ref,
        current_task_id=state.current_task_id,
        current_intent_digest=state.current_intent_digest,
        current_intent_revision=state.current_intent_revision,
        current_attempt_id=state.current_attempt_id,
        attempt_number=state.attempt_number,
        desired_outcome=intent.desired_outcome,
        task_class=intent.task_class.value,
        stop_boundary=intent.stop_boundary.value,
        latest_verified_result=vrs,
        phase=state.phase.value,
        current_base_sha=state.current_base_sha,
        state_revision=state.state_revision,
        known_blockers=state.blockers,
    )

    serialized = canonical_serialize_context_pack(pack)
    if len(serialized.encode("utf-8")) > MAX_CONTEXT_PACK_BYTES:
        _fail("CONTEXT_PACK_TOO_LARGE")

    return pack


def deserialize_context_pack(raw: str | bytes) -> ContextPack:
    """Deserialize a ContextPack. Rejects duplicate JSON keys."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("CONTEXT_PACK_JSON_INVALID")
    else:
        text = raw

    try:
        payload = json.loads(text, object_pairs_hook=_no_dup_keys)
    except (_DuplicateJsonKey, json.JSONDecodeError):
        _fail("CONTEXT_PACK_JSON_INVALID")

    if not isinstance(payload, dict):
        _fail("CONTEXT_PACK_JSON_INVALID")

    sv = payload.get("schema_version")
    if sv != CONTEXT_PACK_SCHEMA_VERSION:
        _fail("CONTEXT_PACK_SCHEMA_UNSUPPORTED")

    vrs_raw = payload.get("latest_verified_result")
    vrs: VerifiedResultSummary | None = None
    if vrs_raw is not None:
        gate_results = tuple(
            GateResultSummary(
                gate_name=gr["gate_name"],
                required=gr["required"],
                status=gr["status"],
            )
            for gr in vrs_raw.get("gate_results", [])
        )
        vrs = VerifiedResultSummary(
            result_id=vrs_raw["result_id"],
            status=vrs_raw["status"],
            blockers=tuple(vrs_raw.get("blockers", [])),
            reason_codes=tuple(vrs_raw.get("reason_codes", [])),
            gate_results=gate_results,
            base_sha=vrs_raw["base_sha"],
            head_sha=vrs_raw["head_sha"],
        )

    return ContextPack(
        schema_version=sv,
        pack_id=payload["pack_id"],
        run_id=payload["run_id"],
        root_goal=payload["root_goal"],
        repository=payload["repository"],
        canonical_remote=payload["canonical_remote"],
        canonical_main_ref=payload["canonical_main_ref"],
        current_task_id=payload["current_task_id"],
        current_intent_digest=payload["current_intent_digest"],
        current_intent_revision=payload["current_intent_revision"],
        current_attempt_id=payload["current_attempt_id"],
        attempt_number=payload["attempt_number"],
        desired_outcome=payload["desired_outcome"],
        task_class=payload["task_class"],
        stop_boundary=payload["stop_boundary"],
        latest_verified_result=vrs,
        phase=payload["phase"],
        current_base_sha=payload["current_base_sha"],
        state_revision=payload["state_revision"],
        known_blockers=tuple(payload.get("known_blockers", [])),
    )
