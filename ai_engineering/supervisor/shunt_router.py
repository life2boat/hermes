"""shunt_router.py - Evidence category routing for supervisor pipeline.

Routes artifact_type strings to EvidenceCategory buckets deterministically.
Unknown types are preserved as GENERIC and MUST NOT be discarded.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ai_engineering.supervisor.worker_result import ArtifactManifestEntry


class EvidenceCategory(str, Enum):
    """Stable taxonomy of evidence categories for the supervisor pipeline."""

    REPOSITORY_PROVENANCE = "REPOSITORY_PROVENANCE"
    DIFF_CHANGE = "DIFF_CHANGE"
    TESTS = "TESTS"
    LINT_STATIC = "LINT_STATIC"
    CI = "CI"
    LOG = "LOG"
    GENERIC = "GENERIC"


# Deterministic mapping: artifact_type -> EvidenceCategory
_TYPE_TO_CATEGORY: dict[str, EvidenceCategory] = {
    # Repository provenance
    "git_sha_evidence": EvidenceCategory.REPOSITORY_PROVENANCE,
    "repository_provenance": EvidenceCategory.REPOSITORY_PROVENANCE,
    # Diff / patch
    "git_diff": EvidenceCategory.DIFF_CHANGE,
    "patch": EvidenceCategory.DIFF_CHANGE,
    # Tests
    "test_result": EvidenceCategory.TESTS,
    "pytest_result": EvidenceCategory.TESTS,
    "test_output": EvidenceCategory.TESTS,
    # Lint / static analysis
    "lint_result": EvidenceCategory.LINT_STATIC,
    "ruff_result": EvidenceCategory.LINT_STATIC,
    "typecheck_result": EvidenceCategory.LINT_STATIC,
    "static_analysis": EvidenceCategory.LINT_STATIC,
    # CI
    "ci_result": EvidenceCategory.CI,
    "ci_log": EvidenceCategory.CI,
    "workflow_run": EvidenceCategory.CI,
    # Logs
    "log": EvidenceCategory.LOG,
    "execution_log": EvidenceCategory.LOG,
}


@dataclass(frozen=True, slots=True)
class ShuntedArtifact:
    """An artifact after category routing.

    category=GENERIC means the artifact cannot satisfy required deterministic
    gates (e.g. a test gate must have test_result, not a generic artifact).
    This invariant is documented here; enforcement is in the validator.
    """

    artifact_type: str
    category: EvidenceCategory
    artifact_ref: str      # relative_path from the manifest entry
    sha256: str
    producer_note: str


def shunt_route(artifacts: list[ArtifactManifestEntry]) -> list[ShuntedArtifact]:
    """Route a list of ArtifactManifestEntry objects to ShuntedArtifact.

    Deterministic: same input always produces same output.
    Unknown artifact_types are preserved as GENERIC and MUST NOT be discarded.
    """
    result: list[ShuntedArtifact] = []
    for entry in artifacts:
        category = _TYPE_TO_CATEGORY.get(entry.artifact_type, EvidenceCategory.GENERIC)
        result.append(
            ShuntedArtifact(
                artifact_type=entry.artifact_type,
                category=category,
                artifact_ref=entry.relative_path,
                sha256=entry.sha256,
                producer_note=entry.producer_note,
            )
        )
    return result
