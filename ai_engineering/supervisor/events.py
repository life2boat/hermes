"""Supervisor event contract (hermes.supervisor-event.v1)."""

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

from ai_engineering.supervisor.state import SupervisorError, _fail

SUPERVISOR_EVENT_SCHEMA_VERSION = "hermes.supervisor-event.v1"
MAX_EVENT_PAYLOAD_BYTES = 64 * 1024  # 64 KiB

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class SupervisorEventType(StrEnum):
    RUN_INITIALIZED = "RUN_INITIALIZED"
    PHASE_TRANSITIONED = "PHASE_TRANSITIONED"
    RESULT_INGESTED = "RESULT_INGESTED"
    CONTEXT_PACK_BUILT = "CONTEXT_PACK_BUILT"
    DECISION_ACCEPTED = "DECISION_ACCEPTED"
    NEXT_TASK_GENERATED = "NEXT_TASK_GENERATED"
    ATTEMPT_INCREMENTED = "ATTEMPT_INCREMENTED"
    BLOCKER_RECORDED = "BLOCKER_RECORDED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"
    WORK_PROFILE_BOUND = "WORK_PROFILE_BOUND"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    AUTONOMY_LEVEL_CHANGED = "AUTONOMY_LEVEL_CHANGED"
    DISPATCH_SENT = "DISPATCH_SENT"
    WORKER_RESULT_RECEIVED = "WORKER_RESULT_RECEIVED"
    CI_PASSED = "CI_PASSED"
    CI_FAILED = "CI_FAILED"
    SOURCE_RECONCILED = "SOURCE_RECONCILED"
    PR_BOUND = "PR_BOUND"


@dataclass(frozen=True, slots=True)
class SupervisorEvent:
    schema_version: str
    run_id: str
    sequence: int           # strictly increasing, starts at 1
    event_id: str           # sha256 of all fields EXCEPT event_id and event_digest
    previous_event_digest: str | None  # None only for sequence=1
    event_type: SupervisorEventType
    state_revision: int     # after applying this event
    task_id: str
    attempt_id: str
    intent_digest: str
    verified_result_id: str | None
    decision_id: str | None
    payload_digest: str     # sha256 of serialized payload dict
    created_at_utc: str
    event_digest: str       # sha256 of entire event (all fields incl. event_id)


def _event_to_dict_for_id(
    schema_version: str,
    run_id: str,
    sequence: int,
    previous_event_digest: str | None,
    event_type: str,
    state_revision: int,
    task_id: str,
    attempt_id: str,
    intent_digest: str,
    verified_result_id: str | None,
    decision_id: str | None,
    payload_digest: str,
    created_at_utc: str,
) -> dict:
    """All fields except event_id and event_digest for computing event_id."""
    return {
        "attempt_id": attempt_id,
        "created_at_utc": created_at_utc,
        "decision_id": decision_id,
        "event_type": event_type,
        "intent_digest": intent_digest,
        "payload_digest": payload_digest,
        "previous_event_digest": previous_event_digest,
        "run_id": run_id,
        "schema_version": schema_version,
        "sequence": sequence,
        "state_revision": state_revision,
        "task_id": task_id,
        "verified_result_id": verified_result_id,
    }


def _compute_event_id(d: dict) -> str:
    raw = json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compute_event_digest(event_dict_with_id: dict) -> str:
    """sha256 of all fields INCLUDING event_id but EXCLUDING event_digest."""
    raw = json.dumps(event_dict_with_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_event(
    run_id: str,
    sequence: int,
    previous_event_digest: str | None,
    event_type: SupervisorEventType,
    state_revision: int,
    task_id: str,
    attempt_id: str,
    intent_digest: str,
    payload: dict,
    created_at_utc: str,
    verified_result_id: str | None = None,
    decision_id: str | None = None,
) -> SupervisorEvent:
    """Create a new SupervisorEvent with computed event_id and event_digest."""
    payload_raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload_digest = hashlib.sha256(payload_raw.encode("utf-8")).hexdigest()

    id_dict = _event_to_dict_for_id(
        schema_version=SUPERVISOR_EVENT_SCHEMA_VERSION,
        run_id=run_id,
        sequence=sequence,
        previous_event_digest=previous_event_digest,
        event_type=event_type.value,
        state_revision=state_revision,
        task_id=task_id,
        attempt_id=attempt_id,
        intent_digest=intent_digest,
        verified_result_id=verified_result_id,
        decision_id=decision_id,
        payload_digest=payload_digest,
        created_at_utc=created_at_utc,
    )
    event_id = _compute_event_id(id_dict)

    digest_dict = dict(id_dict)
    digest_dict["event_id"] = event_id
    event_digest_val = _compute_event_digest(digest_dict)

    return SupervisorEvent(
        schema_version=SUPERVISOR_EVENT_SCHEMA_VERSION,
        run_id=run_id,
        sequence=sequence,
        event_id=event_id,
        previous_event_digest=previous_event_digest,
        event_type=event_type,
        state_revision=state_revision,
        task_id=task_id,
        attempt_id=attempt_id,
        intent_digest=intent_digest,
        verified_result_id=verified_result_id,
        decision_id=decision_id,
        payload_digest=payload_digest,
        created_at_utc=created_at_utc,
        event_digest=event_digest_val,
    )


def _event_to_dict(event: SupervisorEvent) -> dict:
    return {
        "attempt_id": event.attempt_id,
        "created_at_utc": event.created_at_utc,
        "decision_id": event.decision_id,
        "event_digest": event.event_digest,
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "intent_digest": event.intent_digest,
        "payload_digest": event.payload_digest,
        "previous_event_digest": event.previous_event_digest,
        "run_id": event.run_id,
        "schema_version": event.schema_version,
        "sequence": event.sequence,
        "state_revision": event.state_revision,
        "task_id": event.task_id,
        "verified_result_id": event.verified_result_id,
    }


def canonical_serialize_event(event: SupervisorEvent) -> str:
    return json.dumps(
        _event_to_dict(event),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def event_digest(event: SupervisorEvent) -> str:
    """Return the event_digest field (already computed at creation)."""
    return event.event_digest


class _DuplicateJsonKey(ValueError):
    pass


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for k, v in pairs:
        if k in result:
            raise _DuplicateJsonKey(k)
        result[k] = v
    return result


_EVENT_FIELDS = frozenset({
    "schema_version", "run_id", "sequence", "event_id", "previous_event_digest",
    "event_type", "state_revision", "task_id", "attempt_id", "intent_digest",
    "verified_result_id", "decision_id", "payload_digest", "created_at_utc", "event_digest",
})


def deserialize_event(raw: str | bytes) -> SupervisorEvent:
    """Deserialize and validate a SupervisorEvent. Rejects duplicate JSON keys and verifies digest."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("EVENT_JSON_INVALID")
    else:
        text = raw

    if len(text.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        _fail("EVENT_TOO_LARGE")

    try:
        payload = json.loads(text, object_pairs_hook=_no_dup_keys)
    except (_DuplicateJsonKey, json.JSONDecodeError):
        _fail("EVENT_JSON_INVALID")

    if not isinstance(payload, dict):
        _fail("EVENT_JSON_INVALID")

    keys = frozenset(payload)
    if _EVENT_FIELDS - keys or keys - _EVENT_FIELDS:
        _fail("EVENT_FIELD_INVALID")

    sv = payload["schema_version"]
    if sv != SUPERVISOR_EVENT_SCHEMA_VERSION:
        _fail("EVENT_SCHEMA_VERSION_UNSUPPORTED")

    # Validate fields
    run_id = payload["run_id"]
    if not isinstance(run_id, str):
        _fail("EVENT_FIELD_INVALID")

    seq = payload["sequence"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        _fail("EVENT_FIELD_INVALID")

    event_id = payload["event_id"]
    if not isinstance(event_id, str) or not re.match(r"^[0-9a-f]{64}$", event_id):
        _fail("EVENT_FIELD_INVALID")

    prev_digest = payload["previous_event_digest"]
    if prev_digest is not None:
        if not isinstance(prev_digest, str) or not re.match(r"^[0-9a-f]{64}$", prev_digest):
            _fail("EVENT_FIELD_INVALID")

    event_type_raw = payload["event_type"]
    try:
        event_type = SupervisorEventType(event_type_raw)
    except ValueError:
        _fail("EVENT_FIELD_INVALID")

    state_rev = payload["state_revision"]
    if isinstance(state_rev, bool) or not isinstance(state_rev, int) or state_rev < 1:
        _fail("EVENT_FIELD_INVALID")

    task_id = payload["task_id"]
    if not isinstance(task_id, str):
        _fail("EVENT_FIELD_INVALID")

    attempt_id = payload["attempt_id"]
    if not isinstance(attempt_id, str):
        _fail("EVENT_FIELD_INVALID")

    intent_dg = payload["intent_digest"]
    if not isinstance(intent_dg, str) or not re.match(r"^[0-9a-f]{64}$", intent_dg):
        _fail("EVENT_FIELD_INVALID")

    verified_result_id = payload["verified_result_id"]
    if verified_result_id is not None and not isinstance(verified_result_id, str):
        _fail("EVENT_FIELD_INVALID")

    decision_id = payload["decision_id"]
    if decision_id is not None and not isinstance(decision_id, str):
        _fail("EVENT_FIELD_INVALID")

    payload_dg = payload["payload_digest"]
    if not isinstance(payload_dg, str) or not re.match(r"^[0-9a-f]{64}$", payload_dg):
        _fail("EVENT_FIELD_INVALID")

    created_at_utc = payload["created_at_utc"]
    if not isinstance(created_at_utc, str):
        _fail("EVENT_FIELD_INVALID")

    stored_event_digest = payload["event_digest"]
    if not isinstance(stored_event_digest, str) or not re.match(r"^[0-9a-f]{64}$", stored_event_digest):
        _fail("EVENT_FIELD_INVALID")

    # Verify event_id
    id_dict = _event_to_dict_for_id(
        schema_version=SUPERVISOR_EVENT_SCHEMA_VERSION,
        run_id=run_id,
        sequence=seq,
        previous_event_digest=prev_digest,
        event_type=event_type.value,
        state_revision=state_rev,
        task_id=task_id,
        attempt_id=attempt_id,
        intent_digest=intent_dg,
        verified_result_id=verified_result_id,
        decision_id=decision_id,
        payload_digest=payload_dg,
        created_at_utc=created_at_utc,
    )
    expected_event_id = _compute_event_id(id_dict)
    if event_id != expected_event_id:
        _fail("EVENT_TAMPERED")

    # Verify event_digest
    digest_dict = dict(id_dict)
    digest_dict["event_id"] = event_id
    expected_digest = _compute_event_digest(digest_dict)
    if stored_event_digest != expected_digest:
        _fail("EVENT_TAMPERED")

    return SupervisorEvent(
        schema_version=SUPERVISOR_EVENT_SCHEMA_VERSION,
        run_id=run_id,
        sequence=seq,
        event_id=event_id,
        previous_event_digest=prev_digest,
        event_type=event_type,
        state_revision=state_rev,
        task_id=task_id,
        attempt_id=attempt_id,
        intent_digest=intent_dg,
        verified_result_id=verified_result_id,
        decision_id=decision_id,
        payload_digest=payload_dg,
        created_at_utc=created_at_utc,
        event_digest=stored_event_digest,
    )


def validate_event_chain(events: list[SupervisorEvent]) -> None:
    """Validate event chain integrity. Raises SupervisorError on any violation."""
    if not events:
        return

    # First event must have previous_event_digest=None
    if events[0].previous_event_digest is not None:
        _fail("EVENT_CHAIN_FIRST_HAS_PREVIOUS")

    if events[0].sequence != 1:
        _fail("EVENT_CHAIN_SEQUENCE_GAP")

    for i, ev in enumerate(events[1:], start=1):
        prev = events[i - 1]
        # Sequence must be strictly increasing by 1
        if ev.sequence != prev.sequence + 1:
            if ev.sequence == prev.sequence:
                _fail("EVENT_CHAIN_DUPLICATE")
            _fail("EVENT_CHAIN_SEQUENCE_GAP")
        # Chain: event N's previous_event_digest == event N-1's event_digest
        if ev.previous_event_digest != prev.event_digest:
            _fail("EVENT_CHAIN_BROKEN")
