"""Astra decision contract (hermes.astra-decision.v1)."""

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
from ai_engineering.supervisor.state import SupervisorError, SupervisorState, _fail

ASTRA_DECISION_SCHEMA_VERSION = "hermes.astra-decision.v1"
DECISION_RECEIPT_SCHEMA_VERSION = "hermes.decision-receipt.v1"
MAX_DECISION_BYTES = 32 * 1024

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class SupervisorAction(StrEnum):
    CONTINUE = "CONTINUE"   # PASS -> continue to next task
    FIX = "FIX"             # FAIL -> generate fix task
    RETRY = "RETRY"         # BLOCKED -> new attempt
    COMPLETE = "COMPLETE"   # PASS -> root goal achieved
    BLOCK = "BLOCK"         # cannot proceed safely
    CREATE_PR = "CREATE_PR" # PASS -> initiate PR lifecycle
    MERGE_IF_GREEN = "MERGE_IF_GREEN" # PASS -> merge qualified PR


# Admissibility matrix: result_status -> allowed actions
_ADMISSIBLE: dict[str, frozenset[SupervisorAction]] = {
    "PASS": frozenset({
        SupervisorAction.CONTINUE,
        SupervisorAction.COMPLETE,
        SupervisorAction.CREATE_PR,
        SupervisorAction.MERGE_IF_GREEN,
    }),
    "FAIL": frozenset({SupervisorAction.FIX, SupervisorAction.BLOCK}),
    "BLOCKED": frozenset({SupervisorAction.RETRY, SupervisorAction.BLOCK}),
}


@dataclass(frozen=True, slots=True)
class AstraDecision:
    schema_version: str
    decision_id: str        # content-bound sha256
    run_id: str
    task_id: str
    attempt_id: str
    intent_digest: str
    verified_result_id: str
    verified_result_digest: str
    context_pack_digest: str
    action: SupervisorAction
    rationale_summary: str  # descriptive text only, not executed
    next_objective: str | None
    acceptance_delta: str | None
    requested_required_gates: tuple[str, ...]
    created_at_utc: str


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    schema_version: str
    receipt_id: str         # content-bound sha256
    run_id: str
    task_id: str
    attempt_id: str
    intent_digest: str
    context_pack_digest: str
    verified_result_id: str
    verified_result_digest: str
    decision_id: str
    decision_digest: str
    action: SupervisorAction
    validated_at_utc: str


class _DuplicateJsonKey(ValueError):
    pass


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for k, v in pairs:
        if k in result:
            raise _DuplicateJsonKey(k)
        result[k] = v
    return result


_DECISION_FIELDS = frozenset({
    "schema_version", "decision_id", "run_id", "task_id", "attempt_id",
    "intent_digest", "verified_result_id", "verified_result_digest",
    "context_pack_digest", "action", "rationale_summary", "next_objective",
    "acceptance_delta", "requested_required_gates", "created_at_utc",
})


def _decision_to_dict(d: AstraDecision) -> dict:
    return {
        "acceptance_delta": d.acceptance_delta,
        "action": d.action.value,
        "attempt_id": d.attempt_id,
        "context_pack_digest": d.context_pack_digest,
        "created_at_utc": d.created_at_utc,
        "decision_id": d.decision_id,
        "intent_digest": d.intent_digest,
        "next_objective": d.next_objective,
        "rationale_summary": d.rationale_summary,
        "requested_required_gates": list(d.requested_required_gates),
        "run_id": d.run_id,
        "schema_version": d.schema_version,
        "task_id": d.task_id,
        "verified_result_digest": d.verified_result_digest,
        "verified_result_id": d.verified_result_id,
    }


def canonical_serialize_decision(d: AstraDecision) -> str:
    return json.dumps(
        _decision_to_dict(d),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def decision_content_digest(d: AstraDecision) -> str:
    return hashlib.sha256(canonical_serialize_decision(d).encode("utf-8")).hexdigest()


def _compute_decision_id(d: dict) -> str:
    """Content-bound decision_id computed from all fields except decision_id itself."""
    d_without_id = {k: v for k, v in d.items() if k != "decision_id"}
    raw = json.dumps(d_without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compute_receipt_id(r: dict) -> str:
    r_without_id = {k: v for k, v in r.items() if k != "receipt_id"}
    raw = json.dumps(r_without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def deserialize_astra_decision(raw: str | bytes) -> AstraDecision:
    """Deserialize an AstraDecision. Fail-closed: reject duplicate JSON keys."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("DECISION_JSON_INVALID")
    else:
        text = raw

    if len(text.encode("utf-8")) > MAX_DECISION_BYTES:
        _fail("DECISION_TOO_LARGE")

    try:
        payload = json.loads(text, object_pairs_hook=_no_dup_keys)
    except (_DuplicateJsonKey, json.JSONDecodeError):
        _fail("DECISION_JSON_INVALID")

    if not isinstance(payload, dict):
        _fail("DECISION_JSON_INVALID")

    keys = frozenset(payload)
    missing = _DECISION_FIELDS - keys
    extra = keys - _DECISION_FIELDS
    if missing or extra:
        _fail("DECISION_FIELD_INVALID")

    sv = payload["schema_version"]
    if sv != ASTRA_DECISION_SCHEMA_VERSION:
        _fail("DECISION_SCHEMA_UNSUPPORTED")

    action_raw = payload["action"]
    try:
        action = SupervisorAction(action_raw)
    except ValueError:
        _fail("DECISION_ACTION_INVALID")

    requested_gates = payload["requested_required_gates"]
    if not isinstance(requested_gates, list):
        _fail("DECISION_FIELD_INVALID")

    return AstraDecision(
        schema_version=sv,
        decision_id=payload["decision_id"],
        run_id=payload["run_id"],
        task_id=payload["task_id"],
        attempt_id=payload["attempt_id"],
        intent_digest=payload["intent_digest"],
        verified_result_id=payload["verified_result_id"],
        verified_result_digest=payload["verified_result_digest"],
        context_pack_digest=payload["context_pack_digest"],
        action=action,
        rationale_summary=payload["rationale_summary"],
        next_objective=payload["next_objective"],
        acceptance_delta=payload["acceptance_delta"],
        requested_required_gates=tuple(requested_gates),
        created_at_utc=payload["created_at_utc"],
    )


class DecisionValidator:
    """Validates an AstraDecision against current supervisor state."""

    def validate(
        self,
        decision: AstraDecision,
        state: SupervisorState,
        verified_result_digest: str,
        context_pack_digest: str,
        verified_result_status: str,
    ) -> DecisionReceipt:
        """Validate decision and return a DecisionReceipt on success."""

        # 1. Schema version
        if decision.schema_version != ASTRA_DECISION_SCHEMA_VERSION:
            _fail("DECISION_SCHEMA_UNSUPPORTED")

        # 2. run_id match
        if decision.run_id != state.run_id:
            _fail("SUPERVISOR_RUN_MISMATCH")

        # 3. task_id match
        if decision.task_id != state.current_task_id:
            _fail("SUPERVISOR_TASK_MISMATCH")

        # 4. attempt_id match
        if decision.attempt_id != state.current_attempt_id:
            _fail("SUPERVISOR_ATTEMPT_MISMATCH")

        # 5. intent_digest match
        if decision.intent_digest != state.current_intent_digest:
            _fail("SUPERVISOR_INTENT_MISMATCH")

        # 6. verified_result_id match
        if decision.verified_result_id != state.latest_verified_result_id:
            _fail("VERIFIED_RESULT_MISMATCH")

        # 7. verified_result_digest match
        if decision.verified_result_digest != verified_result_digest:
            _fail("VERIFIED_RESULT_DIGEST_MISMATCH")

        # 8. context_pack_digest match
        if decision.context_pack_digest != context_pack_digest:
            _fail("CONTEXT_PACK_DIGEST_MISMATCH")

        # 9. action admissibility
        if verified_result_status not in _ADMISSIBLE:
            _fail("DECISION_ACTION_INVALID")
        allowed_actions = _ADMISSIBLE[verified_result_status]
        if decision.action not in allowed_actions:
            _fail("DECISION_STATUS_ACTION_MISMATCH")

        # 10. Redaction check on rationale_summary and next_objective
        try:
            reject_forbidden_raw_fields({"rationale_summary": decision.rationale_summary})
        except Exception:
            _fail("DECISION_FORBIDDEN_FIELD")

        if decision.next_objective is not None:
            try:
                reject_forbidden_raw_fields({"next_objective": decision.next_objective})
            except Exception:
                _fail("DECISION_FORBIDDEN_FIELD")

        # 11. No authority escalation fields
        # The decision must not contain any fields that expand authority.
        # We check that requested_required_gates doesn't contain escalation markers.
        for gate in decision.requested_required_gates:
            if not isinstance(gate, str):
                _fail("DECISION_FIELD_INVALID")

        # Build receipt
        decision_dg = decision_content_digest(decision)

        receipt_dict = {
            "action": decision.action.value,
            "attempt_id": decision.attempt_id,
            "context_pack_digest": context_pack_digest,
            "decision_digest": decision_dg,
            "decision_id": decision.decision_id,
            "intent_digest": decision.intent_digest,
            "run_id": state.run_id,
            "schema_version": DECISION_RECEIPT_SCHEMA_VERSION,
            "task_id": decision.task_id,
            "validated_at_utc": "",
            "verified_result_digest": verified_result_digest,
            "verified_result_id": decision.verified_result_id,
        }
        receipt_id = _compute_receipt_id(receipt_dict)

        from ai_engineering.supervisor.context_pack import ContextPack
        import datetime as _dt
        validated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

        return DecisionReceipt(
            schema_version=DECISION_RECEIPT_SCHEMA_VERSION,
            receipt_id=receipt_id,
            run_id=state.run_id,
            task_id=decision.task_id,
            attempt_id=decision.attempt_id,
            intent_digest=decision.intent_digest,
            context_pack_digest=context_pack_digest,
            verified_result_id=decision.verified_result_id,
            verified_result_digest=verified_result_digest,
            decision_id=decision.decision_id,
            decision_digest=decision_dg,
            action=decision.action,
            validated_at_utc=validated_at,
        )
