"""Byte-span transformer for hermes-secrets-override.yml environment section."""
from __future__ import annotations
import os
import re
from ops.secret_remediation_r1.constants import PROTECTED_NAMES
from ops.secret_remediation_r1.safe_fs import safe_open_source, publish_file, SafeFsError


class OverrideTransformError(Exception):
    pass


def _find_environment_block(lines: list[str]) -> tuple[int, int]:
    """
    Find services.hermes-bot.environment list items for canonical protected names.
    Returns (first_protected_line, last_protected_line) indices.
    Raises OverrideTransformError if structure not recognized.
    """
    in_services = False
    in_hermes_bot = False
    in_environment = False
    protected_line_indices: list[int] = []

    for i, line in enumerate(lines):
        stripped = line.rstrip()

        if stripped == "services:":
            in_services = True
            continue

        if in_services and not in_hermes_bot:
            if line.startswith("  hermes-bot:"):
                in_hermes_bot = True
                continue

        if in_hermes_bot and not in_environment:
            if stripped.strip() == "environment:":
                in_environment = True
                continue
            if stripped and not stripped.startswith("  "):
                break

        if in_environment:
            item_stripped = stripped.lstrip("- ").strip()
            if stripped.startswith("      -") or stripped.startswith("    -"):
                # Could be KEY=value or KEY:
                key = item_stripped.split("=", 1)[0].split(":", 1)[0].strip()
                if key in PROTECTED_NAMES:
                    protected_line_indices.append(i)
            elif stripped:
                indent = len(line) - len(line.lstrip())
                if indent <= 4 and not stripped.lstrip().startswith("-"):
                    break

    return protected_line_indices


def transform_override(
    source_path: str,
    destination_path: str,
) -> bytes:
    """
    Remove canonical protected environment entries from the override file.
    Returns original bytes for rollback.
    """
    try:
        src_fd, _ = safe_open_source(source_path)
        with os.fdopen(src_fd, "rb") as f:
            original_bytes = f.read()
    except Exception as exc:
        raise OverrideTransformError(f"Failed to read source: {exc}")

    lines = original_bytes.decode("utf-8").splitlines(keepends=True)
    protected_indices = set(_find_environment_block(lines))

    new_lines = [line for i, line in enumerate(lines) if i not in protected_indices]
    new_bytes = "".join(new_lines).encode("utf-8")

    # Verify unrelated bytes are identical
    non_protected_orig = [l for i, l in enumerate(lines) if i not in protected_indices]
    if non_protected_orig != new_lines:
        raise OverrideTransformError("Unrelated byte mutation detected")

    from ops.secret_remediation_r1.safe_fs import publish_file, replace_existing_file
    try:
        if source_path == destination_path:
            replace_existing_file(destination_path, new_bytes)
        else:
            publish_file(destination_path, new_bytes, mode=0o644)
    except SafeFsError as exc:
        raise OverrideTransformError(f"Publication failed: {exc}") from exc

    return original_bytes
