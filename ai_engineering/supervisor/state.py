"""Supervisor persistent state contract (hermes.supervisor-state.v1)."""

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


SUPERVISOR_STATE_SCHEMA_VERSION = "hermes.supervisor-state.v1"
MAX_ROOT_GOAL_BYTES = 4096
MAX_BLOCKERS = 32

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class SupervisorError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise SupervisorError(code)


class SupervisorPhase(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COLLECTING = "COLLECTING"
    VERIFYING = "VERIFYING"
    DECIDING = "DECIDING"
    READY_FOR_NEXT_TASK = "READY_FOR_NEXT_TASK"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_TERMINAL_PHASES = frozenset({
    SupervisorPhase.DONE,
    SupervisorPhase.FAILED,
    SupervisorPhase.CANCELLED,
})

_VALID_TRANSITIONS: dict[SupervisorPhase, frozenset[SupervisorPhase]] = {
    SupervisorPhase.PENDING: frozenset({SupervisorPhase.RUNNING, SupervisorPhase.CANCELLED}),
    SupervisorPhase.RUNNING: frozenset({SupervisorPhase.COLLECTING, SupervisorPhase.BLOCKED, SupervisorPhase.FAILED, SupervisorPhase.CANCELLED}),
    SupervisorPhase.COLLECTING: frozenset({SupervisorPhase.VERIFYING, SupervisorPhase.BLOCKED, SupervisorPhase.FAILED, SupervisorPhase.CANCELLED}),
    SupervisorPhase.VERIFYING: frozenset({SupervisorPhase.DECIDING, SupervisorPhase.BLOCKED, SupervisorPhase.FAILED, SupervisorPhase.CANCELLED}),
    SupervisorPhase.DECIDING: frozenset({SupervisorPhase.READY_FOR_NEXT_TASK, SupervisorPhase.DONE, SupervisorPhase.BLOCKED, SupervisorPhase.FAILED, SupervisorPhase.CANCELLED}),
    SupervisorPhase.READY_FOR_NEXT_TASK: frozenset({SupervisorPhase.RUNNING, SupervisorPhase.DONE, SupervisorPhase.BLOCKED, SupervisorPhase.FAILED, SupervisorPhase.CANCELLED}),
    SupervisorPhase.BLOCKED: frozenset({SupervisorPhase.RUNNING, SupervisorPhase.FAILED, SupervisorPhase.CANCELLED}),
    SupervisorPhase.DONE: frozenset(),
    SupervisorPhase.FAILED: frozenset(),
    SupervisorPhase.CANCELLED: frozenset(),
}


def validate_phase_transition(current: SupervisorPhase, next_phase: SupervisorPhase) -> None:
    """Raise SupervisorError if transition is invalid."""
    if current in _TERMINAL_PHASES:
        _fail("PHASE_TERMINAL_CANNOT_TRANSITION")
    allowed = _VALID_TRANSITIONS.get(current, frozenset())
    if next_phase not in allowed:
        _fail("PHASE_TRANSITION_INVALID")


@dataclass(frozen=True, slots=True)
class SupervisorState:
    schema_version: str
    run_id: str
    root_goal_id: str
    root_goal: str
    repository: str
    canonical_remote: str
    canonical_main_ref: str
    current_task_id: str
    current_intent_digest: str
    current_intent_revision: int
    current_base_sha: str
    current_attempt_id: str
    attempt_number: int
    latest_verified_result_id: str | None
    latest_verified_result_digest: str | None
    latest_context_pack_digest: str | None
    latest_decision_id: str | None
    latest_decision_digest: str | None
    engineering_cycle_id: str | None
    engineering_cycle_phase: str | None
    phase: SupervisorPhase
    blockers: tuple[str, ...]
    created_at_utc: str
    updated_at_utc: str
    state_revision: int
    event_sequence: int


def _state_to_dict(state: SupervisorState) -> dict:
    return {
        "attempt_number": state.attempt_number,
        "blockers": list(state.blockers),
        "canonical_main_ref": state.canonical_main_ref,
        "canonical_remote": state.canonical_remote,
        "created_at_utc": state.created_at_utc,
        "current_attempt_id": state.current_attempt_id,
        "current_base_sha": state.current_base_sha,
        "current_intent_digest": state.current_intent_digest,
        "current_intent_revision": state.current_intent_revision,
        "current_task_id": state.current_task_id,
        "engineering_cycle_id": state.engineering_cycle_id,
        "engineering_cycle_phase": state.engineering_cycle_phase,
        "event_sequence": state.event_sequence,
        "latest_context_pack_digest": state.latest_context_pack_digest,
        "latest_decision_digest": state.latest_decision_digest,
        "latest_decision_id": state.latest_decision_id,
        "latest_verified_result_digest": state.latest_verified_result_digest,
        "latest_verified_result_id": state.latest_verified_result_id,
        "phase": state.phase.value,
        "repository": state.repository,
        "root_goal": state.root_goal,
        "root_goal_id": state.root_goal_id,
        "run_id": state.run_id,
        "schema_version": state.schema_version,
        "state_revision": state.state_revision,
        "updated_at_utc": state.updated_at_utc,
    }


def canonical_serialize_state(state: SupervisorState) -> str:
    return json.dumps(
        _state_to_dict(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def state_digest(state: SupervisorState) -> str:
    return hashlib.sha256(canonical_serialize_state(state).encode("utf-8")).hexdigest()


class _DuplicateJsonKey(ValueError):
    pass


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for k, v in pairs:
        if k in result:
            raise _DuplicateJsonKey(k)
        result[k] = v
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError


def _req_identifier(v: object, field: str) -> str:
    if not isinstance(v, str) or _IDENTIFIER_RE.fullmatch(v) is None:
        _fail(f"FIELD_INVALID:{field}")
    return v


def _req_digest(v: object, field: str) -> str:
    if not isinstance(v, str) or _DIGEST_RE.fullmatch(v) is None:
        _fail(f"FIELD_INVALID:{field}")
    return v


def _opt_digest(v: object, field: str) -> str | None:
    if v is None:
        return None
    return _req_digest(v, field)


def _req_sha(v: object, field: str) -> str:
    if not isinstance(v, str) or _SHA_RE.fullmatch(v) is None:
        _fail(f"FIELD_INVALID:{field}")
    return v


def _req_str(v: object, field: str) -> str:
    if not isinstance(v, str):
        _fail(f"FIELD_INVALID:{field}")
    return v


def _opt_str(v: object, field: str) -> str | None:
    if v is None:
        return None
    return _req_str(v, field)


def _req_int(v: object, field: str, min_val: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < min_val:
        _fail(f"FIELD_INVALID:{field}")
    return v


_STATE_FIELDS = frozenset({
    "schema_version", "run_id", "root_goal_id", "root_goal", "repository",
    "canonical_remote", "canonical_main_ref", "current_task_id", "current_intent_digest",
    "current_intent_revision", "current_base_sha", "current_attempt_id", "attempt_number",
    "latest_verified_result_id", "latest_verified_result_digest", "latest_context_pack_digest",
    "latest_decision_id", "latest_decision_digest", "engineering_cycle_id",
    "engineering_cycle_phase", "phase", "blockers", "created_at_utc", "updated_at_utc",
    "state_revision", "event_sequence",
})


def deserialize_state(raw: str | bytes) -> SupervisorState:
    """Deserialize and validate a SupervisorState. Rejects duplicate JSON keys."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("JSON_INVALID")
    else:
        text = raw
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_no_dup_keys,
            parse_constant=_reject_json_constant,
        )
    except (_DuplicateJsonKey, json.JSONDecodeError, ValueError):
        _fail("JSON_INVALID")

    if not isinstance(payload, dict):
        _fail("JSON_INVALID")

    keys = frozenset(payload)
    missing = _STATE_FIELDS - keys
    extra = keys - _STATE_FIELDS
    if missing or extra:
        _fail("FIELD_INVALID:schema")

    sv = payload["schema_version"]
    if sv != SUPERVISOR_STATE_SCHEMA_VERSION:
        _fail("SCHEMA_VERSION_UNSUPPORTED")

    run_id = _req_identifier(payload["run_id"], "run_id")
    root_goal_id = _req_identifier(payload["root_goal_id"], "root_goal_id")
    root_goal = _req_str(payload["root_goal"], "root_goal")
    if len(root_goal.encode("utf-8")) > MAX_ROOT_GOAL_BYTES:
        _fail("ROOT_GOAL_TOO_LARGE")

    repository = _req_str(payload["repository"], "repository")
    canonical_remote = _req_str(payload["canonical_remote"], "canonical_remote")
    canonical_main_ref = _req_str(payload["canonical_main_ref"], "canonical_main_ref")
    current_task_id = _req_identifier(payload["current_task_id"], "current_task_id")
    current_intent_digest = _req_digest(payload["current_intent_digest"], "current_intent_digest")
    current_intent_revision = _req_int(payload["current_intent_revision"], "current_intent_revision", 1)
    current_base_sha = _req_sha(payload["current_base_sha"], "current_base_sha")
    current_attempt_id = _req_identifier(payload["current_attempt_id"], "current_attempt_id")
    attempt_number = _req_int(payload["attempt_number"], "attempt_number", 1)

    latest_verified_result_id = _opt_str(payload["latest_verified_result_id"], "latest_verified_result_id")
    latest_verified_result_digest = _opt_digest(payload["latest_verified_result_digest"], "latest_verified_result_digest")
    latest_context_pack_digest = _opt_digest(payload["latest_context_pack_digest"], "latest_context_pack_digest")
    latest_decision_id = _opt_str(payload["latest_decision_id"], "latest_decision_id")
    latest_decision_digest = _opt_digest(payload["latest_decision_digest"], "latest_decision_digest")
    engineering_cycle_id = _opt_str(payload["engineering_cycle_id"], "engineering_cycle_id")
    engineering_cycle_phase = _opt_str(payload["engineering_cycle_phase"], "engineering_cycle_phase")

    phase_raw = payload["phase"]
    try:
        phase = SupervisorPhase(phase_raw)
    except (ValueError, KeyError):
        _fail("FIELD_INVALID:phase")

    raw_blockers = payload["blockers"]
    if not isinstance(raw_blockers, list):
        _fail("FIELD_INVALID:blockers")
    if len(raw_blockers) > MAX_BLOCKERS:
        _fail("BLOCKERS_TOO_MANY")
    blockers = tuple(_req_str(b, "blockers") for b in raw_blockers)

    created_at_utc = _req_str(payload["created_at_utc"], "created_at_utc")
    updated_at_utc = _req_str(payload["updated_at_utc"], "updated_at_utc")
    state_revision = _req_int(payload["state_revision"], "state_revision", 1)
    event_sequence = _req_int(payload["event_sequence"], "event_sequence", 0)

    return SupervisorState(
        schema_version=SUPERVISOR_STATE_SCHEMA_VERSION,
        run_id=run_id,
        root_goal_id=root_goal_id,
        root_goal=root_goal,
        repository=repository,
        canonical_remote=canonical_remote,
        canonical_main_ref=canonical_main_ref,
        current_task_id=current_task_id,
        current_intent_digest=current_intent_digest,
        current_intent_revision=current_intent_revision,
        current_base_sha=current_base_sha,
        current_attempt_id=current_attempt_id,
        attempt_number=attempt_number,
        latest_verified_result_id=latest_verified_result_id,
        latest_verified_result_digest=latest_verified_result_digest,
        latest_context_pack_digest=latest_context_pack_digest,
        latest_decision_id=latest_decision_id,
        latest_decision_digest=latest_decision_digest,
        engineering_cycle_id=engineering_cycle_id,
        engineering_cycle_phase=engineering_cycle_phase,
        phase=phase,
        blockers=blockers,
        created_at_utc=created_at_utc,
        updated_at_utc=updated_at_utc,
        state_revision=state_revision,
        event_sequence=event_sequence,
    )


def create_initial_state(
    run_id: str,
    root_goal_id: str,
    root_goal: str,
    repository: str,
    canonical_remote: str,
    canonical_main_ref: str,
    task_id: str,
    intent_digest_val: str,
    intent_revision: int,
    base_sha: str,
    attempt_id: str,
    created_at_utc: str,
) -> SupervisorState:
    """Create the initial supervisor state."""
    return SupervisorState(
        schema_version=SUPERVISOR_STATE_SCHEMA_VERSION,
        run_id=run_id,
        root_goal_id=root_goal_id,
        root_goal=root_goal,
        repository=repository,
        canonical_remote=canonical_remote,
        canonical_main_ref=canonical_main_ref,
        current_task_id=task_id,
        current_intent_digest=intent_digest_val,
        current_intent_revision=intent_revision,
        current_base_sha=base_sha,
        current_attempt_id=attempt_id,
        attempt_number=1,
        latest_verified_result_id=None,
        latest_verified_result_digest=None,
        latest_context_pack_digest=None,
        latest_decision_id=None,
        latest_decision_digest=None,
        engineering_cycle_id=None,
        engineering_cycle_phase=None,
        phase=SupervisorPhase.PENDING,
        blockers=(),
        created_at_utc=created_at_utc,
        updated_at_utc=created_at_utc,
        state_revision=1,
        event_sequence=0,
    )
