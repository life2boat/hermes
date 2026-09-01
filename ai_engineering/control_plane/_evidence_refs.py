"""Shared evidence-reference validation for handoffs and validation evidence.

An evidence reference is either a pure evidence identifier (no path
separators, no drive letters) or a strictly repository-relative path
validated by the canonical snapshot contract. Everything else --
absolute POSIX paths, Windows drive paths in either slash style, UNC
paths, and traversal components -- is rejected.
"""

from __future__ import annotations

import re
from typing import NoReturn

from ai_engineering.workspaces.snapshot_contracts import validate_repository_relative_path

_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")

_REJECTION_CODE = "CONTROL_PLANE_HANDOFF_INCOMPLETE"


def _raise(error_type: type[Exception], message: str) -> NoReturn:
    if error_type is Exception or not hasattr(error_type, "__init__"):
        raise error_type(message)
    try:
        raise error_type(_REJECTION_CODE, message)
    except TypeError:
        raise error_type(message) from None


def validate_evidence_ref(ref: object, error_type: type[Exception] = Exception) -> str:
    """Validate one evidence reference, raising ``error_type`` on failure."""
    if not isinstance(ref, str):
        _raise(error_type, f"Evidence ref must be a string, got {ref!r}")
    if _EVIDENCE_ID_RE.match(ref):
        return ref
    try:
        return validate_repository_relative_path(ref)
    except Exception as exc:  # noqa: BLE001 - fail closed on any rejection
        _raise(
            error_type,
            f"Evidence refs must be evidence IDs or strictly repository-relative "
            f"paths; rejected: {ref!r} ({exc})",
        )
        raise  # unreachable
