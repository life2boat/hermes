"""collector.py - Trusted result collection pipeline step.

The ResultCollector is the boundary between untrusted worker output and the
supervisor pipeline.  It:
  1. Parses and validates the WorkerResultBundle (fail closed)
  2. Verifies task_id and intent_digest binding
  3. Verifies attempt_id is present
  4. Resolves artifacts inside evidence_root using _safe_resolve logic
  5. SHA256 verifies each artifact (reject mismatches)
  6. Applies resource bounds
  7. Runs shunt_route() on artifacts
  8. Returns NormalizedEvidence

SECURITY INVARIANTS:
  - MUST NOT execute commands from worker result
  - MUST NOT import from worker data
  - MUST NOT follow arbitrary paths outside evidence_root
"""

from __future__ import annotations

import hashlib
import posixpath
from pathlib import Path
from typing import NoReturn

from ai_engineering.task_intent import TaskIntent, intent_digest
from ai_engineering.supervisor.worker_result import (
    ArtifactManifestEntry,
    WorkerResultBundle,
    WorkerResultError,
    deserialize_worker_result,
)
from ai_engineering.supervisor.shunt_router import shunt_route, ShuntedArtifact
from ai_engineering.supervisor.normalized_evidence import (
    NormalizedEvidence,
    normalize_worker_result,
)


class CollectorError(ValueError):
    """Fail-closed collector error with stable .code attribute."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise CollectorError(code)


def _safe_resolve_artifact(evidence_root: Path, artifact_ref: str) -> Path:
    """Resolve artifact_ref inside evidence_root, rejecting any escape attempt.

    Mirrors evidence_verifier._safe_resolve logic but raises CollectorError.
    """
    # Reject control characters
    if any(ord(c) < 32 for c in artifact_ref):
        _fail("PATH_TRAVERSAL_FORBIDDEN")

    # Reject absolute POSIX paths
    if posixpath.isabs(artifact_ref):
        _fail("ABSOLUTE_PATH_FORBIDDEN")

    # Reject Windows drive paths
    if len(artifact_ref) >= 2 and artifact_ref[1] == ":":
        _fail("DRIVE_PATH_FORBIDDEN")

    # Reject UNC paths
    if artifact_ref.startswith("\\\\") or artifact_ref.startswith("//"):
        _fail("UNC_PATH_FORBIDDEN")

    # Reject traversal components
    parts = artifact_ref.replace("\\", "/").split("/")
    if ".." in parts:
        _fail("PATH_TRAVERSAL_FORBIDDEN")

    candidate = (evidence_root / artifact_ref).resolve()
    root_resolved = evidence_root.resolve()

    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        _fail("ARTIFACT_REF_OUTSIDE_ROOT")

    return candidate


def _verify_artifact_digest(path: Path, expected_sha256: str) -> None:
    """Verify file exists, is regular, not a symlink, and SHA256 matches."""
    if path.is_symlink():
        _fail("ARTIFACT_IS_SYMLINK")
    if not path.exists():
        _fail("ARTIFACT_NOT_FOUND")
    if not path.is_file():
        _fail("ARTIFACT_NOT_REGULAR_FILE")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        _fail("ARTIFACT_DIGEST_MISMATCH")


class ResultCollector:
    """Trusted boundary that converts raw worker output to NormalizedEvidence."""

    def collect(
        self,
        result_bundle_json: str | bytes,
        evidence_root: str | Path,
        intent: TaskIntent,
        *,
        collection_receipt_id: str | None = None,
    ) -> NormalizedEvidence:
        """Parse, validate, verify, and normalize a worker result.

        Parameters
        ----------
        result_bundle_json:
            Raw JSON from the worker (untrusted).
        evidence_root:
            Trusted directory that all artifact files must reside within.
        intent:
            The TaskIntent this result is claimed to satisfy.
        collection_receipt_id:
            Optional receipt ID for audit trail.

        Returns
        -------
        NormalizedEvidence with integrity_verified=True if all artifact digests
        were successfully verified against their on-disk files.

        Raises
        ------
        CollectorError with stable .code on any validation or verification failure.
        """
        # Step 1: Parse and validate worker result bundle (fail closed)
        try:
            bundle = deserialize_worker_result(result_bundle_json)
        except WorkerResultError as exc:
            raise CollectorError(exc.code) from exc

        # Step 2: Verify task_id and intent_digest binding
        expected_digest = intent_digest(intent)
        if bundle.task_id != intent.task_id:
            _fail("TASK_MISMATCH")
        if bundle.intent_digest != expected_digest:
            _fail("INTENT_DIGEST_MISMATCH")

        # Step 3: Verify attempt_id is present
        if not bundle.attempt_id or not bundle.attempt_id.strip():
            _fail("ATTEMPT_MISSING")

        # Step 4 + 5: Resolve artifacts and verify SHA256
        root = Path(evidence_root)
        if not root.exists():
            _fail("EVIDENCE_ROOT_NOT_FOUND")
        if not root.is_dir():
            _fail("EVIDENCE_ROOT_UNSAFE")

        # Check root itself is not a symlink chain leading outside
        try:
            root_resolved = root.resolve()
        except Exception:
            _fail("EVIDENCE_ROOT_UNSAFE")

        for entry in bundle.artifacts:
            raw_path = root / entry.relative_path
            # Check for symlink on the raw (unresolved) path first
            if raw_path.is_symlink():
                _fail("ARTIFACT_IS_SYMLINK")
            resolved = _safe_resolve_artifact(root, entry.relative_path)
            _verify_artifact_digest(resolved, entry.sha256)

        # Step 6: Resource bounds already enforced during deserialization

        # Step 7: Shunt route artifacts
        shunted: list[ShuntedArtifact] = shunt_route(list(bundle.artifacts))

        # Step 8: Normalize
        ne = normalize_worker_result(
            bundle,
            shunted,
            collection_receipt_id=collection_receipt_id,
            integrity_verified=True,
        )
        return ne
