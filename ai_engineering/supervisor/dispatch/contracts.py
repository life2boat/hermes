import json
import hashlib
from dataclasses import dataclass, asdict
from enum import StrEnum
from typing import Mapping

from ai_engineering.contracts import EffectClass, StopBoundary

DISPATCH_ENVELOPE_SCHEMA_VERSION = "hermes.dispatch-envelope.v1"
DISPATCH_RECEIPT_SCHEMA_VERSION = "hermes.dispatch-receipt.v1"
SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION = "hermes.source-transition-receipt.v1"

class DispatchStatus(StrEnum):
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    RESULT_AVAILABLE = "RESULT_AVAILABLE"
    COLLECTING = "COLLECTING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"

@dataclass(frozen=True, slots=True)
class DispatchEnvelope:
    schema_version: str
    dispatch_id: str
    run_id: str
    task_id: str
    attempt_id: str
    intent_digest: str
    intent_revision: int

    source_repository: str
    source_main_ref: str
    source_base_sha: str

    task_intent_digest: str
    policy_receipt_id: str
    policy_receipt_digest: str

    worker_kind: str
    worker_profile: str
    worker_model: str

    required_result_schema: str
    required_gates: tuple[str, ...]

    created_at_utc: str

@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    schema_version: str
    receipt_id: str
    dispatch_id: str
    task_id: str
    attempt_id: str
    policy_receipt_id: str
    worker_kind: str
    worker_identity: str
    dispatched_at_utc: str
    expected_result_channel: str
    status: DispatchStatus
    reason_code: str | None

@dataclass(frozen=True, slots=True)
class SourceTransitionReceipt:
    schema_version: str
    receipt_id: str
    repository: str
    canonical_remote: str

    previous_canonical_main_sha: str

    pr_number: int | None
    pr_head_sha: str | None

    merge_method: str | None
    merge_commit_sha: str | None

    new_canonical_main_sha: str

    reconciliation_evidence: str

    created_at_utc: str

def compute_deterministic_digest(data: Mapping[str, object]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def compute_dispatch_id(run_id: str, task_id: str, attempt_id: str, intent_digest: str) -> str:
    """Idempotent identity for dispatch."""
    payload = {
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "intent_digest": intent_digest
    }
    return compute_deterministic_digest(payload)
