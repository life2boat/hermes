"""Canonical artifact-byte verifier for EvidenceBundle artifacts.

This module proves trusted-root containment, file existence, and exact
SHA-256 binding for every artifact referenced by an EvidenceBundle.

IMPORTANT SCOPE BOUNDARY
========================
This verifier proves **byte-level binding**:
  - The referenced file exists inside the trusted evidence root.
  - The file is regular (not a symlink or directory).
  - The file's SHA-256 hash equals the recorded artifact_digest.
  - No path traversal or container-escape is possible.

It does NOT verify the semantic truth or validity of the artifact's
contents.  A file whose bytes are correctly bound may still contain
incorrect runtime observations.  Semantic adjudication is the
responsibility of EvidenceSufficiencyReview, not this verifier.
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_engineering.convergence import EvidenceBundle

ARTIFACT_VERIFIER_VERSION = 1


class ArtifactVerificationError(ValueError):
    """Fail-closed error with a stable error code string."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> Any:
    raise ArtifactVerificationError(code)


@dataclass(frozen=True, slots=True)
class ArtifactVerificationEntry:
    artifact_ref: str
    artifact_digest: str
    status: str  # "PASS" | "FAIL"
    error_code: str | None  # None when status==PASS
    resolved_path: str | None  # None on error


@dataclass(frozen=True, slots=True)
class ArtifactVerificationResult:
    schema_version: int
    bundle_id: str
    evidence_root: str
    overall: str  # "PASS" | "FAIL"
    entries: tuple[ArtifactVerificationEntry, ...]


def _safe_resolve(evidence_root: Path, artifact_ref: str) -> Path:
    """Resolve artifact_ref inside evidence_root with full escape prevention.

    Raises ArtifactVerificationError for:
    - Absolute paths in artifact_ref
    - Path traversal components ('..') anywhere in artifact_ref
    - Null bytes or other control characters
    - Resolved path outside evidence_root (symlink escapes)
    """
    # Reject null bytes and control chars
    if any(ord(c) < 32 for c in artifact_ref):
        _fail("ARTIFACT_REF_CONTROL_CHARS")

    # Reject absolute paths (both POSIX / and Windows drive letters / UNC)
    if posixpath.isabs(artifact_ref):
        _fail("ARTIFACT_REF_ABSOLUTE_PATH")
    # Windows-style absolute: C:\ or C:/
    if len(artifact_ref) >= 2 and artifact_ref[1] == ":":
        _fail("ARTIFACT_REF_ABSOLUTE_PATH")
    # UNC paths
    if artifact_ref.startswith("\\\\") or artifact_ref.startswith("//"):
        _fail("ARTIFACT_REF_ABSOLUTE_PATH")

    # Reject traversal components
    # Split on both forward and back slashes for portability
    parts = artifact_ref.replace("\\", "/").split("/")
    if ".." in parts:
        _fail("ARTIFACT_REF_PATH_TRAVERSAL")

    # Resolve inside root
    candidate = (evidence_root / artifact_ref).resolve()
    root_resolved = evidence_root.resolve()

    # Check containment — works on both Windows and POSIX
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        _fail("ARTIFACT_REF_OUTSIDE_ROOT")

    return candidate


def _verify_single(
    evidence_root: Path,
    artifact_ref: str,
    artifact_digest: str,
) -> ArtifactVerificationEntry:
    """Verify one observation's artifact. Returns an entry with status."""
    try:
        resolved = _safe_resolve(evidence_root, artifact_ref)
    except ArtifactVerificationError as e:
        return ArtifactVerificationEntry(
            artifact_ref=artifact_ref,
            artifact_digest=artifact_digest,
            status="FAIL",
            error_code=e.code,
            resolved_path=None,
        )

    raw_path = evidence_root / artifact_ref
    curr = raw_path
    root_resolved = evidence_root.resolve()
    while True:
        if curr.is_symlink():
            return ArtifactVerificationEntry(
                artifact_ref=artifact_ref,
                artifact_digest=artifact_digest,
                status="FAIL",
                error_code="ARTIFACT_IS_SYMLINK",
                resolved_path=str(raw_path),
            )
        if curr.resolve() == root_resolved or curr == curr.parent:
            break
        curr = curr.parent

    if not resolved.exists():
        return ArtifactVerificationEntry(
            artifact_ref=artifact_ref,
            artifact_digest=artifact_digest,
            status="FAIL",
            error_code="ARTIFACT_NOT_FOUND",
            resolved_path=None,
        )

    # Reject symlinks (symlink escape)
    if resolved.is_symlink():
        return ArtifactVerificationEntry(
            artifact_ref=artifact_ref,
            artifact_digest=artifact_digest,
            status="FAIL",
            error_code="ARTIFACT_IS_SYMLINK",
            resolved_path=str(resolved),
        )

    if not resolved.is_file():
        return ArtifactVerificationEntry(
            artifact_ref=artifact_ref,
            artifact_digest=artifact_digest,
            status="FAIL",
            error_code="ARTIFACT_NOT_REGULAR_FILE",
            resolved_path=str(resolved),
        )

    # SHA-256 check
    actual_digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual_digest != artifact_digest:
        return ArtifactVerificationEntry(
            artifact_ref=artifact_ref,
            artifact_digest=artifact_digest,
            status="FAIL",
            error_code="ARTIFACT_DIGEST_MISMATCH",
            resolved_path=str(resolved),
        )

    return ArtifactVerificationEntry(
        artifact_ref=artifact_ref,
        artifact_digest=artifact_digest,
        status="PASS",
        error_code=None,
        resolved_path=str(resolved),
    )


def verify_evidence_bundle_artifacts(
    bundle: EvidenceBundle,
    evidence_root: str | Path,
) -> ArtifactVerificationResult:
    """Verify every artifact referenced by the EvidenceBundle.

    Parameters
    ----------
    bundle:
        A validated EvidenceBundle.  Only observations with non-empty
        artifact_ref values are verified (observations may reference
        logical identifiers that are not file paths; those are skipped).
    evidence_root:
        A trusted directory that all artifact files must reside within.
        Must be an existing directory.  Symlinks in the root path itself
        are resolved before containment checks.

    Returns
    -------
    ArtifactVerificationResult with overall="PASS" only when every
    artifact passes.  Fails are fail-closed: any single failure yields
    overall="FAIL".

    Scope boundary
    --------------
    This function proves byte-level binding:
      - trusted-root containment
      - exact file bytes
      - SHA-256 equality to artifact_digest
    It does NOT verify the semantic truth of the artifact contents.
    """
    root = Path(evidence_root)
    if not root.exists():
        _fail("EVIDENCE_ROOT_NOT_FOUND")
    if not root.is_dir():
        _fail("EVIDENCE_ROOT_NOT_A_DIRECTORY")

    entries: list[ArtifactVerificationEntry] = []
    seen_refs: set[str] = set()

    for obs in bundle.observations:
        ref = obs.artifact_ref
        digest = obs.artifact_digest

        # Skip logical/non-file refs (e.g. empty or marker strings)
        if not ref or not ref.strip():
            continue

        # Track duplicate refs — same ref+digest pair is fine;
        # same ref with different digest would indicate a bundle anomaly.
        key = (ref, digest)
        if key in seen_refs:
            continue
        seen_refs.add(key)

        entry = _verify_single(root, ref, digest)
        entries.append(entry)

    overall = "PASS" if all(e.status == "PASS" for e in entries) else "FAIL"

    return ArtifactVerificationResult(
        schema_version=ARTIFACT_VERIFIER_VERSION,
        bundle_id=bundle.bundle_id,
        evidence_root=str(root.resolve()),
        overall=overall,
        entries=tuple(entries),
    )


def serialize_verification_result(result: ArtifactVerificationResult) -> dict:
    return {
        "schema_version": result.schema_version,
        "bundle_id": result.bundle_id,
        "evidence_root": result.evidence_root,
        "overall": result.overall,
        "entries": [
            {
                "artifact_ref": e.artifact_ref,
                "artifact_digest": e.artifact_digest,
                "status": e.status,
                "error_code": e.error_code,
                "resolved_path": e.resolved_path,
            }
            for e in result.entries
        ],
    }
