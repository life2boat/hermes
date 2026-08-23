"""Memory Graph Shadow Activation Readiness & Canary Contracts."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ai_engineering.production_readiness_evidence import _compute_receipt_id


class MemoryGraphActivationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise MemoryGraphActivationError(code)


@dataclass(frozen=True, slots=True)
class MemoryGraphShadowActivationPreflight:
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


def serialize_preflight(preflight: MemoryGraphShadowActivationPreflight) -> bytes:
    content = {
        "schema_version": preflight.schema_version,
        "subject_main_sha": preflight.subject_main_sha,
        "candidate_image_revision": preflight.candidate_image_revision,
        "db_path_safe": preflight.db_path_safe,
        "db_integrity": preflight.db_integrity,
        "foreign_key_violations": preflight.foreign_key_violations,
        "graph_schema_classification": preflight.graph_schema_classification,
        "backup_required": preflight.backup_required,
        "rollback_proven": preflight.rollback_proven,
        "shadow_mode_available": preflight.shadow_mode_available,
        "serve_mode_available": preflight.serve_mode_available,
        "graph_context_served_to_users": preflight.graph_context_served_to_users,
        "production_activation_authorized": preflight.production_activation_authorized,
        "verdict": preflight.verdict,
        "reason_codes": sorted(set(preflight.reason_codes)),
    }
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MemoryGraphShadowHealthReceipt:
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
    content = {
        "image_revision": receipt.image_revision,
        "canonical_main_sha": receipt.canonical_main_sha,
        "runtime_mode": receipt.runtime_mode,
        "observation_start_utc": receipt.observation_start_utc,
        "observation_end_utc": receipt.observation_end_utc,
        "runtime_status": receipt.runtime_status,
        "integrity_block_count": receipt.integrity_block_count,
        "cross_user_leakage": receipt.cross_user_leakage,
        "excluded_fact_leakage": receipt.excluded_fact_leakage,
        "unexpected_authoritative_db_mutation": receipt.unexpected_authoritative_db_mutation,
        "worker_crash_loop": receipt.worker_crash_loop,
        "queue_bounded": receipt.queue_bounded,
        "source_churn_exhaustion_bounded": receipt.source_churn_exhaustion_bounded,
        "baseline_memory_path_healthy": receipt.baseline_memory_path_healthy,
        "gateway_healthy": receipt.gateway_healthy,
        "restart_count_stable": receipt.restart_count_stable,
        "sqlite_integrity": receipt.sqlite_integrity,
        "foreign_key_check": receipt.foreign_key_check,
        "graph_serving_disabled_proof": receipt.graph_serving_disabled_proof,
        "verdict": receipt.verdict,
    }
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

def check_activation_readiness(
    *,
    subject_main_sha: str,
    candidate_image_revision: str,
    db_path_safe: bool,
    db_integrity: str,
    foreign_key_violations: int,
    graph_schema_classification: str,
    backup_required: bool,
    rollback_proven: bool,
    shadow_mode_available: bool,
    serve_mode_available: bool,
    graph_context_served_to_users: bool,
    production_activation_authorized: bool,
    expected_subject_main_sha: str,
    schema_version: int = 1,
) -> MemoryGraphShadowActivationPreflight:
    reason_codes: list[str] = []

    if subject_main_sha != expected_subject_main_sha:
        reason_codes.append("CANONICAL_SHA_MISMATCH")
    if not db_path_safe:
        reason_codes.append("UNSAFE_DB_PATH")
    if db_integrity != "ok":
        reason_codes.append("DATABASE_INTEGRITY_FAILURE")
    if foreign_key_violations > 0:
        reason_codes.append("FOREIGN_KEY_VIOLATION")
    if graph_schema_classification == "INCOMPATIBLE":
        reason_codes.append("GRAPH_SCHEMA_INCOMPATIBLE")
    if not rollback_proven:
        reason_codes.append("ROLLBACK_NOT_PROVEN")
    if serve_mode_available:
        reason_codes.append("SERVE_MODE_UNEXPECTEDLY_ENABLED")
    if graph_context_served_to_users:
        reason_codes.append("GRAPH_SERVING_NOT_DISABLED")

    verdict = "PASS" if not reason_codes else "BLOCKED"

    return MemoryGraphShadowActivationPreflight(
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
