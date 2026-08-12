"""Deterministically evaluate whether a procedure is ready for graph review."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn

from ai_engineering.contracts import EffectClass, Status, TraceValidationError
from ai_engineering.redaction import reject_forbidden_raw_fields, verify_sanitized_evidence


PROCEDURE_MATURITY_SCHEMA_VERSION = 1
PROCEDURE_MATURITY_POLICY_VERSION = 1

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "policy_version",
        "procedure_id",
        "procedure_version",
        "current_stage",
        "source_procedure_ref",
        "eval_dataset_ref",
        "stable_intent",
        "stable_sequence",
        "known_failure_modes",
        "adequate_regression_corpus",
        "side_effects_understood",
        "authority_boundary_known",
        "remaining_agent_judgement",
        "required_invariants",
        "allowed_effect_classes",
    }
)
_JUDGEMENT_FIELDS = frozenset({"decision_id", "agent_controlled"})
_CRITERIA = (
    "stable_intent",
    "stable_sequence",
    "known_failure_modes",
    "adequate_regression_corpus",
    "side_effects_understood",
    "authority_boundary_known",
)


class ProcedureMaturityError(ValueError):
    """Fail-closed maturity input/configuration error with a public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RemainingAgentJudgement:
    """A decision that stays explicit rather than being frozen into a graph."""

    decision_id: str
    agent_controlled: bool


@dataclass(frozen=True, slots=True)
class ProcedureMaturityEvidence:
    """Sanitized evidence for one procedure-maturity determination."""

    schema_version: int
    policy_version: int
    procedure_id: str
    procedure_version: str
    current_stage: str
    source_procedure_ref: str
    eval_dataset_ref: str
    stable_intent: Status
    stable_sequence: Status
    known_failure_modes: Status
    adequate_regression_corpus: Status
    side_effects_understood: Status
    authority_boundary_known: Status
    remaining_agent_judgement: tuple[RemainingAgentJudgement, ...]
    required_invariants: tuple[str, ...]
    allowed_effect_classes: tuple[EffectClass, ...]


@dataclass(frozen=True, slots=True)
class ProcedureMaturityReceipt:
    """A review-only graph-candidate determination without execution authority."""

    schema_version: int
    policy_version: int
    evidence: ProcedureMaturityEvidence
    graph_candidate_eligible: Status
    authority_expansion_authorized: bool
    reason_codes: tuple[str, ...]
    receipt_digest: str


def _fail(code: str) -> NoReturn:
    raise ProcedureMaturityError(code)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        _fail("PROCEDURE_MATURITY_REQUIRED_FIELD_MISSING")
    return value


def _exact_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    payload = _mapping(value)
    keys = frozenset(payload)
    if expected - keys:
        _fail("PROCEDURE_MATURITY_REQUIRED_FIELD_MISSING")
    if keys - expected:
        _fail("PROCEDURE_MATURITY_UNEXPECTED_FIELD")
    return payload


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail("PROCEDURE_MATURITY_VALUE_INVALID")
    return value


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("PROCEDURE_MATURITY_VALUE_INVALID")
    return value


def _identifiers(value: object) -> tuple[str, ...]:
    result = tuple(_identifier(item) for item in _items(value))
    if len(result) != len(set(result)):
        _fail("PROCEDURE_MATURITY_VALUE_INVALID")
    return result


def _status(value: object) -> Status:
    if not isinstance(value, str):
        _fail("PROCEDURE_MATURITY_STATUS_INVALID")
    try:
        return Status(value)
    except ValueError:
        _fail("PROCEDURE_MATURITY_STATUS_INVALID")


def _effects(value: object) -> tuple[EffectClass, ...]:
    result: list[EffectClass] = []
    for item in _items(value):
        if not isinstance(item, str):
            _fail("PROCEDURE_MATURITY_EFFECT_CLASS_INVALID")
        try:
            result.append(EffectClass(item))
        except ValueError:
            _fail("PROCEDURE_MATURITY_EFFECT_CLASS_INVALID")
    if len(result) != len(set(result)):
        _fail("PROCEDURE_MATURITY_EFFECT_CLASS_INVALID")
    return tuple(result)


def _judgements(value: object) -> tuple[RemainingAgentJudgement, ...]:
    results: list[RemainingAgentJudgement] = []
    for item in _items(value):
        payload = _exact_fields(item, _JUDGEMENT_FIELDS)
        if not isinstance(payload["agent_controlled"], bool):
            _fail("PROCEDURE_MATURITY_VALUE_INVALID")
        results.append(
            RemainingAgentJudgement(
                decision_id=_identifier(payload["decision_id"]),
                agent_controlled=payload["agent_controlled"],
            )
        )
    if len({item.decision_id for item in results}) != len(results):
        _fail("PROCEDURE_MATURITY_VALUE_INVALID")
    return tuple(results)


def validate_procedure_maturity_evidence(
    value: ProcedureMaturityEvidence | Mapping[str, object],
) -> ProcedureMaturityEvidence:
    """Validate fixed-schema evidence without inferring missing maturity."""

    if isinstance(value, ProcedureMaturityEvidence):
        return validate_procedure_maturity_evidence(normalize_procedure_maturity_evidence(value))
    try:
        reject_forbidden_raw_fields(value)
        verify_sanitized_evidence(value)
    except TraceValidationError as exc:
        raise ProcedureMaturityError("PROCEDURE_MATURITY_EVIDENCE_NOT_SANITIZED") from exc
    payload = _exact_fields(value, _EVIDENCE_FIELDS)
    if payload["schema_version"] != PROCEDURE_MATURITY_SCHEMA_VERSION:
        _fail("PROCEDURE_MATURITY_SCHEMA_VERSION_UNSUPPORTED")
    if payload["policy_version"] != PROCEDURE_MATURITY_POLICY_VERSION:
        _fail("PROCEDURE_MATURITY_POLICY_VERSION_UNSUPPORTED")
    evidence = ProcedureMaturityEvidence(
        schema_version=PROCEDURE_MATURITY_SCHEMA_VERSION,
        policy_version=PROCEDURE_MATURITY_POLICY_VERSION,
        procedure_id=_identifier(payload["procedure_id"]),
        procedure_version=_identifier(payload["procedure_version"]),
        current_stage=_identifier(payload["current_stage"]),
        source_procedure_ref=_identifier(payload["source_procedure_ref"]),
        eval_dataset_ref=_identifier(payload["eval_dataset_ref"]),
        stable_intent=_status(payload["stable_intent"]),
        stable_sequence=_status(payload["stable_sequence"]),
        known_failure_modes=_status(payload["known_failure_modes"]),
        adequate_regression_corpus=_status(payload["adequate_regression_corpus"]),
        side_effects_understood=_status(payload["side_effects_understood"]),
        authority_boundary_known=_status(payload["authority_boundary_known"]),
        remaining_agent_judgement=_judgements(payload["remaining_agent_judgement"]),
        required_invariants=_identifiers(payload["required_invariants"]),
        allowed_effect_classes=_effects(payload["allowed_effect_classes"]),
    )
    try:
        verify_sanitized_evidence(normalize_procedure_maturity_evidence(evidence))
    except TraceValidationError as exc:
        raise ProcedureMaturityError("PROCEDURE_MATURITY_EVIDENCE_NOT_SANITIZED") from exc
    return evidence


def normalize_procedure_maturity_evidence(value: ProcedureMaturityEvidence) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "policy_version": value.policy_version,
        "procedure_id": value.procedure_id,
        "procedure_version": value.procedure_version,
        "current_stage": value.current_stage,
        "source_procedure_ref": value.source_procedure_ref,
        "eval_dataset_ref": value.eval_dataset_ref,
        "stable_intent": value.stable_intent.value,
        "stable_sequence": value.stable_sequence.value,
        "known_failure_modes": value.known_failure_modes.value,
        "adequate_regression_corpus": value.adequate_regression_corpus.value,
        "side_effects_understood": value.side_effects_understood.value,
        "authority_boundary_known": value.authority_boundary_known.value,
        "remaining_agent_judgement": [
            {"decision_id": item.decision_id, "agent_controlled": item.agent_controlled}
            for item in value.remaining_agent_judgement
        ],
        "required_invariants": list(value.required_invariants),
        "allowed_effect_classes": [item.value for item in value.allowed_effect_classes],
    }


def _decision(evidence: ProcedureMaturityEvidence) -> tuple[Status, tuple[str, ...]]:
    statuses = {name: getattr(evidence, name) for name in _CRITERIA}
    failing = [name for name, status in statuses.items() if status is Status.FAIL]
    if failing:
        return Status.FAIL, tuple(f"PROCEDURE_MATURITY_{name.upper()}_FAIL" for name in failing)
    unproven = [name for name, status in statuses.items() if status is not Status.PASS]
    if unproven:
        return Status.BLOCKED, tuple(
            f"PROCEDURE_MATURITY_{name.upper()}_NOT_PROVEN" for name in unproven
        )
    if any(not item.agent_controlled for item in evidence.remaining_agent_judgement):
        return Status.FAIL, ("PROCEDURE_MATURITY_AGENT_JUDGEMENT_NOT_EXPLICIT",)
    return Status.PASS, ("PROCEDURE_MATURITY_ALL_REQUIRED_CRITERIA_PASS",)


def _receipt_projection(
    evidence: ProcedureMaturityEvidence,
    status: Status,
    reason_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": PROCEDURE_MATURITY_SCHEMA_VERSION,
        "policy_version": PROCEDURE_MATURITY_POLICY_VERSION,
        "evidence": normalize_procedure_maturity_evidence(evidence),
        "graph_candidate_eligible": status.value,
        "authority_expansion_authorized": False,
        "reason_codes": list(reason_codes),
    }


def procedure_maturity_receipt_digest(value: ProcedureMaturityReceipt) -> str:
    payload = _receipt_projection(
        value.evidence,
        value.graph_candidate_eligible,
        value.reason_codes,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_procedure_maturity(
    value: ProcedureMaturityEvidence | Mapping[str, object],
) -> ProcedureMaturityReceipt:
    """Assess maturity only; this function cannot compile a graph or grant authority."""

    evidence = validate_procedure_maturity_evidence(value)
    status, reason_codes = _decision(evidence)
    provisional = ProcedureMaturityReceipt(
        schema_version=PROCEDURE_MATURITY_SCHEMA_VERSION,
        policy_version=PROCEDURE_MATURITY_POLICY_VERSION,
        evidence=evidence,
        graph_candidate_eligible=status,
        authority_expansion_authorized=False,
        reason_codes=reason_codes,
        receipt_digest="",
    )
    return ProcedureMaturityReceipt(
        schema_version=provisional.schema_version,
        policy_version=provisional.policy_version,
        evidence=provisional.evidence,
        graph_candidate_eligible=provisional.graph_candidate_eligible,
        authority_expansion_authorized=False,
        reason_codes=provisional.reason_codes,
        receipt_digest=procedure_maturity_receipt_digest(provisional),
    )


def normalize_procedure_maturity_receipt(value: ProcedureMaturityReceipt) -> dict[str, object]:
    payload = _receipt_projection(
        value.evidence,
        value.graph_candidate_eligible,
        value.reason_codes,
    )
    payload["receipt_digest"] = procedure_maturity_receipt_digest(value)
    return payload


def serialize_procedure_maturity_receipt(value: ProcedureMaturityReceipt) -> str:
    return json.dumps(
        normalize_procedure_maturity_receipt(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
