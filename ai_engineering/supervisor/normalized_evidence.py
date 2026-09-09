"""normalized_evidence.py - Normalized, supervisor-controlled evidence record.

After a WorkerResultBundle passes all collection checks, it is normalized into a
NormalizedEvidence record that is:
  - bound to a specific task_id and attempt_id (provenance guard)
  - bound to a specific base_sha + head_sha combination (stale head guard)
  - given a content-bound SHA256 evidence_id

A NormalizedEvidence record from attempt A MUST NOT satisfy attempt B.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ai_engineering.supervisor.worker_result import GateClaim, WorkerResultBundle
from ai_engineering.supervisor.shunt_router import ShuntedArtifact

NORMALIZED_EVIDENCE_SCHEMA_VERSION = "hermes.normalized-evidence.v1"


@dataclass(frozen=True, slots=True)
class NormalizedEvidence:
    """Supervisor-controlled, content-bound normalized evidence record.

    This is NOT the raw worker claim.  It is produced by the Collector after
    all validation, path checking, and digest verification has passed.
    """

    schema_version: str
    evidence_id: str          # SHA256 content-bound digest
    task_id: str
    attempt_id: str
    intent_digest: str
    repository: str
    canonical_remote: str
    base_sha: str
    head_sha: str
    worker_id: str
    artifacts: tuple[ShuntedArtifact, ...]
    gate_claims: tuple[GateClaim, ...]
    produced_at_utc: str
    collection_receipt_id: str | None
    integrity_verified: bool


def compute_evidence_id(
    task_id: str,
    attempt_id: str,
    intent_digest: str,
    base_sha: str,
    head_sha: str,
    repository: str,
    canonical_remote: str,
    worker_id: str,
    produced_at_utc: str,
) -> str:
    """SHA256 of canonical JSON of the identifying fields.

    This binds the evidence_id to the specific attempt so that evidence from
    attempt A cannot be replayed to satisfy attempt B.
    """
    payload = json.dumps(
        {
            "attempt_id": attempt_id,
            "base_sha": base_sha,
            "canonical_remote": canonical_remote,
            "head_sha": head_sha,
            "intent_digest": intent_digest,
            "produced_at_utc": produced_at_utc,
            "repository": repository,
            "schema_version": NORMALIZED_EVIDENCE_SCHEMA_VERSION,
            "task_id": task_id,
            "worker_id": worker_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_worker_result(
    bundle: WorkerResultBundle,
    shunted: list[ShuntedArtifact],
    *,
    collection_receipt_id: str | None = None,
    integrity_verified: bool = False,
) -> NormalizedEvidence:
    """Convert a validated WorkerResultBundle + shunted artifacts to NormalizedEvidence."""
    evidence_id = compute_evidence_id(
        task_id=bundle.task_id,
        attempt_id=bundle.attempt_id,
        intent_digest=bundle.intent_digest,
        base_sha=bundle.base_sha,
        head_sha=bundle.head_sha,
        repository=bundle.repository,
        canonical_remote=bundle.canonical_remote,
        worker_id=bundle.worker_id,
        produced_at_utc=bundle.produced_at_utc,
    )
    return NormalizedEvidence(
        schema_version=NORMALIZED_EVIDENCE_SCHEMA_VERSION,
        evidence_id=evidence_id,
        task_id=bundle.task_id,
        attempt_id=bundle.attempt_id,
        intent_digest=bundle.intent_digest,
        repository=bundle.repository,
        canonical_remote=bundle.canonical_remote,
        base_sha=bundle.base_sha,
        head_sha=bundle.head_sha,
        worker_id=bundle.worker_id,
        artifacts=tuple(shunted),
        gate_claims=bundle.gate_claims,
        produced_at_utc=bundle.produced_at_utc,
        collection_receipt_id=collection_receipt_id,
        integrity_verified=integrity_verified,
    )


def _ne_to_dict(ne: NormalizedEvidence) -> dict:
    return {
        "artifacts": [
            {
                "artifact_ref": a.artifact_ref,
                "artifact_type": a.artifact_type,
                "category": a.category.value,
                "producer_note": a.producer_note,
                "sha256": a.sha256,
            }
            for a in ne.artifacts
        ],
        "attempt_id": ne.attempt_id,
        "base_sha": ne.base_sha,
        "canonical_remote": ne.canonical_remote,
        "collection_receipt_id": ne.collection_receipt_id,
        "evidence_id": ne.evidence_id,
        "gate_claims": [
            {
                "claimed_status": gc.claimed_status.value,
                "evidence_refs": list(gc.evidence_refs),
                "gate_name": gc.gate_name,
                "reason_code": gc.reason_code,
            }
            for gc in ne.gate_claims
        ],
        "head_sha": ne.head_sha,
        "integrity_verified": ne.integrity_verified,
        "intent_digest": ne.intent_digest,
        "produced_at_utc": ne.produced_at_utc,
        "repository": ne.repository,
        "schema_version": ne.schema_version,
        "task_id": ne.task_id,
        "worker_id": ne.worker_id,
    }


def canonical_serialize_normalized_evidence(ne: NormalizedEvidence) -> str:
    return json.dumps(
        _ne_to_dict(ne),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def normalized_evidence_digest(ne: NormalizedEvidence) -> str:
    return hashlib.sha256(
        canonical_serialize_normalized_evidence(ne).encode("utf-8")
    ).hexdigest()
