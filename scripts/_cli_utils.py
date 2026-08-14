"""Shared CLI utilities for Hermes Intent Control Plane tools.

Provides fail-closed file reading and output alias protections.
"""

from __future__ import annotations

import os
from pathlib import Path

_MAX_FILE_BYTES = 512 * 1024  # 512 KB


class SafeReadError(Exception):
    """Fail-closed error for unsafe file reads."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def safe_read(path: Path, tool_name: str) -> bytes:
    """Read a file with basic safety checks (no symlinks, bounded size)."""
    if path.is_symlink():
        raise SafeReadError(f"{tool_name}: UNSAFE_PATH: {path}")
    if not path.is_file():
        raise SafeReadError(f"{tool_name}: FILE_NOT_FOUND: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SafeReadError(f"{tool_name}: FILE_UNREADABLE: {exc}") from exc
    if len(raw) > _MAX_FILE_BYTES:
        raise SafeReadError(f"{tool_name}: FILE_TOO_LARGE: {path}")
    return raw


def resolve_path(p: Path) -> Path:
    """Resolve a path for alias comparison (absolute, no symlinks)."""
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


class OutputAliasError(Exception):
    """Fail-closed error for output path aliasing."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def check_output_alias(output: Path, inputs: dict[str, Path], tool_name: str) -> None:
    """Check that output path does not alias any input path.

    Inputs is a dict of {flag_name: path}, e.g. {"--intent": intent_path}.
    Raises OutputAliasError if an alias or potential alias is detected.
    """
    output_resolved = resolve_path(output)

    resolved_inputs = {flag: resolve_path(path) for flag, path in inputs.items()}

    for flag, input_resolved in resolved_inputs.items():
        if output_resolved == input_resolved:
            raise OutputAliasError(
                f"{tool_name}: SAFE_WRITE_VIOLATION: --output resolves to {flag} path"
            )

    if output_resolved.exists():
        for flag, input_resolved in resolved_inputs.items():
            if input_resolved.exists():
                try:
                    if os.path.samefile(output_resolved, input_resolved):
                        raise OutputAliasError(
                            f"{tool_name}: SAFE_WRITE_VIOLATION: --output aliases {flag} (samefile)"
                        )
                except OSError as exc:
                    raise OutputAliasError(
                        f"{tool_name}: SAFE_WRITE_CHECK_FAILED: could not check {flag} alias: {exc}"
                    )
