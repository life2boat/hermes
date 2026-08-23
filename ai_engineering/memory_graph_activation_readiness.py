"""Memory Graph Shadow Activation Readiness & Canary Contracts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any


class MemoryGraphActivationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise MemoryGraphActivationError(code)


class GraphSchemaClassification(str, Enum):
    ABSENT = "ABSENT"
    CURRENT = "CURRENT"
    KNOWN_COMPATIBLE_PARTIAL = "KNOWN_COMPATIBLE_PARTIAL"
    INCOMPATIBLE = "INCOMPATIBLE"


class PreflightVerdict(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAIL = "FAIL"


class PreflightReasonCode(str, Enum):
    CANONICAL_SHA_MISMATCH = "CANONICAL_SHA_MISMATCH"
    IMAGE_REVISION_MISMATCH = "IMAGE_REVISION_MISMATCH"
    UNSAFE_DB_PATH = "UNSAFE_DB_PATH"
    DATABASE_INTEGRITY_FAILURE = "DATABASE_INTEGRITY_FAILURE"
    FOREIGN_KEY_VIOLATION = "FOREIGN_KEY_VIOLATION"
    GRAPH_SCHEMA_INCOMPATIBLE = "GRAPH_SCHEMA_INCOMPATIBLE"
    GRAPH_SCHEMA_UNKNOWN = "GRAPH_SCHEMA_UNKNOWN"
    BACKUP_REQUIRED = "BACKUP_REQUIRED"
    BACKUP_INVALID = "BACKUP_INVALID"
    ROLLBACK_NOT_PROVEN = "ROLLBACK_NOT_PROVEN"
    SHADOW_RUNTIME_UNAVAILABLE = "SHADOW_RUNTIME_UNAVAILABLE"
    SERVE_MODE_UNEXPECTEDLY_ENABLED = "SERVE_MODE_UNEXPECTEDLY_ENABLED"
    GRAPH_SERVING_NOT_DISABLED = "GRAPH_SERVING_NOT_DISABLED"


@dataclass(frozen=True, slots=True)
class MemoryGraphShadowActivationPreflight:
    receipt_id: str
    schema_version: int
    subject_main_sha: str
    candidate_image_revision: str
    db_path_safe: bool
    db_integrity: str
    foreign_key_violations: int
    graph_schema_classification: str
    backup_required: bool
    rollback_proven: bool
    shadow_mode_available: bool
    serve_mode_available: bool
    graph_context_served_to_users: bool
    production_activation_authorized: bool
    verdict: str
    reason_codes: list[str]


def _compute_identity(content: dict[str, Any]) -> str:
    canonical = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def serialize_preflight(preflight: MemoryGraphShadowActivationPreflight) -> bytes:
    content = asdict(preflight)
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MemoryGraphShadowHealthReceipt:
    receipt_id: str
    image_revision: str
    canonical_main_sha: str
    runtime_mode: str
    observation_start_utc: str
    observation_end_utc: str
    runtime_status: str
    integrity_block_count: int
    cross_user_leakage: int
    excluded_fact_leakage: int
    unexpected_authoritative_db_mutation: int
    worker_crash_loop: bool
    queue_bounded: bool
    source_churn_exhaustion_bounded: bool
    baseline_memory_path_healthy: bool
    gateway_healthy: bool
    restart_count_stable: bool
    sqlite_integrity: str
    foreign_key_check: int
    graph_serving_disabled_proof: bool
    verdict: str


def serialize_receipt(receipt: MemoryGraphShadowHealthReceipt) -> bytes:
    content = asdict(receipt)
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_valid_sha(val: str) -> bool:
    return len(val) == 40 and all(c in "0123456789abcdef" for c in val)


def check_activation_readiness(
    *,
    subject_main_sha: str,
    candidate_image_revision: str,
    db_path_safe: bool,
    db_integrity: str,
    foreign_key_violations: int,
    graph_schema_classification: str,
    backup_required: bool,
    backup_valid: bool,
    rollback_proven: bool,
    shadow_mode_available: bool,
    serve_mode_available: bool,
    graph_context_served_to_users: bool,
    production_activation_authorized: bool,
    expected_subject_main_sha: str,
    expected_candidate_image_revision: str,
    schema_version: int = 1,
) -> MemoryGraphShadowActivationPreflight:
    reason_codes: list[str] = []

    if schema_version != 1:
        _fail("INVALID_SCHEMA_VERSION")

    if not isinstance(foreign_key_violations, int) or isinstance(foreign_key_violations, bool) or foreign_key_violations < 0:
        _fail("INVALID_FOREIGN_KEY_VIOLATIONS")

    if not _is_valid_sha(subject_main_sha) or not _is_valid_sha(expected_subject_main_sha):
        _fail("INVALID_SHA_FORMAT")

    if not candidate_image_revision or not expected_candidate_image_revision:
        _fail("INVALID_IMAGE_REVISION")

    if subject_main_sha != expected_subject_main_sha:
        reason_codes.append(PreflightReasonCode.CANONICAL_SHA_MISMATCH.value)

    if candidate_image_revision != expected_candidate_image_revision:
        reason_codes.append(PreflightReasonCode.IMAGE_REVISION_MISMATCH.value)

    if not db_path_safe:
        reason_codes.append(PreflightReasonCode.UNSAFE_DB_PATH.value)

    if db_integrity != "ok":
        reason_codes.append(PreflightReasonCode.DATABASE_INTEGRITY_FAILURE.value)

    if foreign_key_violations > 0:
        reason_codes.append(PreflightReasonCode.FOREIGN_KEY_VIOLATION.value)

    try:
        classification = GraphSchemaClassification(graph_schema_classification)
    except ValueError:
        reason_codes.append(PreflightReasonCode.GRAPH_SCHEMA_UNKNOWN.value)
    else:
        if classification == GraphSchemaClassification.INCOMPATIBLE:
            reason_codes.append(PreflightReasonCode.GRAPH_SCHEMA_INCOMPATIBLE.value)

    if not backup_required:
        reason_codes.append(PreflightReasonCode.BACKUP_REQUIRED.value)
    elif not backup_valid:
        reason_codes.append(PreflightReasonCode.BACKUP_INVALID.value)

    if not rollback_proven:
        reason_codes.append(PreflightReasonCode.ROLLBACK_NOT_PROVEN.value)

    if not shadow_mode_available:
        reason_codes.append(PreflightReasonCode.SHADOW_RUNTIME_UNAVAILABLE.value)

    if serve_mode_available:
        reason_codes.append(PreflightReasonCode.SERVE_MODE_UNEXPECTEDLY_ENABLED.value)

    if graph_context_served_to_users:
        reason_codes.append(PreflightReasonCode.GRAPH_SERVING_NOT_DISABLED.value)

    verdict = PreflightVerdict.PASS.value if not reason_codes else PreflightVerdict.BLOCKED.value

    content_for_id = {
        "schema_version": schema_version,
        "subject_main_sha": subject_main_sha,
        "candidate_image_revision": candidate_image_revision,
        "db_path_safe": db_path_safe,
        "db_integrity": db_integrity,
        "foreign_key_violations": foreign_key_violations,
        "graph_schema_classification": graph_schema_classification,
        "backup_required": backup_required,
        "rollback_proven": rollback_proven,
        "shadow_mode_available": shadow_mode_available,
        "serve_mode_available": serve_mode_available,
        "graph_context_served_to_users": graph_context_served_to_users,
        "production_activation_authorized": production_activation_authorized,
        "verdict": verdict,
        "reason_codes": sorted(set(reason_codes)),
    }
    receipt_id = _compute_identity(content_for_id)

    return MemoryGraphShadowActivationPreflight(
        receipt_id=receipt_id,
        schema_version=schema_version,
        subject_main_sha=subject_main_sha,
        candidate_image_revision=candidate_image_revision,
        db_path_safe=db_path_safe,
        db_integrity=db_integrity,
        foreign_key_violations=foreign_key_violations,
        graph_schema_classification=graph_schema_classification,
        backup_required=backup_required,
        rollback_proven=rollback_proven,
        shadow_mode_available=shadow_mode_available,
        serve_mode_available=serve_mode_available,
        graph_context_served_to_users=graph_context_served_to_users,
        production_activation_authorized=production_activation_authorized,
        verdict=verdict,
        reason_codes=sorted(set(reason_codes)),
    )


def evaluate_shadow_health(
    *,
    image_revision: str,
    canonical_main_sha: str,
    runtime_mode: str,
    observation_start_utc: str,
    observation_end_utc: str,
    runtime_status: str,
    integrity_block_count: int,
    cross_user_leakage: int,
    excluded_fact_leakage: int,
    unexpected_authoritative_db_mutation: int,
    worker_crash_loop: bool,
    queue_bounded: bool,
    source_churn_exhaustion_bounded: bool,
    baseline_memory_path_healthy: bool,
    gateway_healthy: bool,
    restart_count_stable: bool,
    sqlite_integrity: str,
    foreign_key_check: int,
    graph_serving_disabled_proof: bool,
) -> MemoryGraphShadowHealthReceipt:

    # Structural validations
    for counter in (integrity_block_count, cross_user_leakage, excluded_fact_leakage,
                    unexpected_authoritative_db_mutation, foreign_key_check):
        if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
            _fail("INVALID_COUNTER_TYPE_OR_VALUE")

    if observation_start_utc > observation_end_utc:
        _fail("INVALID_OBSERVATION_WINDOW")

    if not image_revision or not _is_valid_sha(canonical_main_sha):
        _fail("INVALID_PROVENANCE_IDENTITY")

    # Evaluate verdict
    if (
        runtime_mode == "shadow"
        and runtime_status == "RUNNING"
        and integrity_block_count == 0
        and cross_user_leakage == 0
        and excluded_fact_leakage == 0
        and unexpected_authoritative_db_mutation == 0
        and not worker_crash_loop
        and queue_bounded
        and source_churn_exhaustion_bounded
        and baseline_memory_path_healthy
        and gateway_healthy
        and restart_count_stable
        and sqlite_integrity == "ok"
        and foreign_key_check == 0
        and graph_serving_disabled_proof
    ):
        verdict = "PASS"
    else:
        verdict = "FAIL"

    content_for_id = {
        "image_revision": image_revision,
        "canonical_main_sha": canonical_main_sha,
        "runtime_mode": runtime_mode,
        "observation_start_utc": observation_start_utc,
        "observation_end_utc": observation_end_utc,
        "runtime_status": runtime_status,
        "integrity_block_count": integrity_block_count,
        "cross_user_leakage": cross_user_leakage,
        "excluded_fact_leakage": excluded_fact_leakage,
        "unexpected_authoritative_db_mutation": unexpected_authoritative_db_mutation,
        "worker_crash_loop": worker_crash_loop,
        "queue_bounded": queue_bounded,
        "source_churn_exhaustion_bounded": source_churn_exhaustion_bounded,
        "baseline_memory_path_healthy": baseline_memory_path_healthy,
        "gateway_healthy": gateway_healthy,
        "restart_count_stable": restart_count_stable,
        "sqlite_integrity": sqlite_integrity,
        "foreign_key_check": foreign_key_check,
        "graph_serving_disabled_proof": graph_serving_disabled_proof,
        "verdict": verdict,
    }
    receipt_id = _compute_identity(content_for_id)

    return MemoryGraphShadowHealthReceipt(
        receipt_id=receipt_id,
        image_revision=image_revision,
        canonical_main_sha=canonical_main_sha,
        runtime_mode=runtime_mode,
        observation_start_utc=observation_start_utc,
        observation_end_utc=observation_end_utc,
        runtime_status=runtime_status,
        integrity_block_count=integrity_block_count,
        cross_user_leakage=cross_user_leakage,
        excluded_fact_leakage=excluded_fact_leakage,
        unexpected_authoritative_db_mutation=unexpected_authoritative_db_mutation,
        worker_crash_loop=worker_crash_loop,
        queue_bounded=queue_bounded,
        source_churn_exhaustion_bounded=source_churn_exhaustion_bounded,
        baseline_memory_path_healthy=baseline_memory_path_healthy,
        gateway_healthy=gateway_healthy,
        restart_count_stable=restart_count_stable,
        sqlite_integrity=sqlite_integrity,
        foreign_key_check=foreign_key_check,
        graph_serving_disabled_proof=graph_serving_disabled_proof,
        verdict=verdict,
    )
