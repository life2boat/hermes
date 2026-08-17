"""Byte-preserving structural transformer for docker-compose.yml env_file node."""

from __future__ import annotations
import io
import os
from ops.secret_remediation_r1.constants import (
    PROD_LEGACY_ENV_PATH,
    PROD_RUNTIME_ENV_PATH,
    PROD_SECRET_FILE_PATH,
)
from ops.secret_remediation_r1.safe_fs import safe_open_source, SafeFsError


class ComposeTransformError(Exception):
    pass


_EXPECTED_OLD_ENTRY = PROD_LEGACY_ENV_PATH


def _find_env_file_span(lines: list[str]) -> tuple[int, int]:
    """
    Find start and end line indices (inclusive) of the env_file block
    for services.hermes-bot, and verify it contains ONLY the expected legacy path.
    Returns (start_idx, end_idx) of lines to replace.
    Raises ComposeTransformError if structure is unexpected.
    """
    in_services = False
    in_hermes_bot = False
    in_env_file = False
    env_file_indent: str = ""
    env_file_start: int = -1
    env_file_entries: list[str] = []
    entry_lines: list[int] = []

    for i, line in enumerate(lines):
        stripped = line.rstrip()

        if stripped == "services:":
            in_services = True
            continue

        if in_services and not in_hermes_bot:
            if line.startswith("  hermes-bot:"):
                in_hermes_bot = True
                continue

        if in_hermes_bot and not in_env_file:
            if stripped.startswith("  ") and stripped.endswith("env_file:"):
                in_env_file = True
                env_file_indent = line[: len(line) - len(line.lstrip())]
                env_file_start = i
                continue
            # If we hit another top-level key under hermes-bot or another service, stop
            if stripped and not stripped.startswith("  "):
                break

        if in_env_file:
            # Collect list items
            item_stripped = stripped.lstrip()
            if item_stripped.startswith("-"):
                entry_value = item_stripped.lstrip("- ").strip()
                env_file_entries.append(entry_value)
                entry_lines.append(i)
            elif stripped and not stripped.startswith("#"):
                # End of env_file block
                break

    if env_file_start == -1:
        raise ComposeTransformError("services.hermes-bot.env_file block not found")
    if not entry_lines:
        raise ComposeTransformError("env_file block has no entries")
    if env_file_entries != [_EXPECTED_OLD_ENTRY]:
        raise ComposeTransformError(
            f"Unexpected env_file entries: {env_file_entries!r}; "
            f"expected [{_EXPECTED_OLD_ENTRY!r}]"
        )

    return entry_lines[0], entry_lines[-1]


def transform_base_compose(
    source_path: str,
    destination_path: str,
) -> bytes:
    """
    Transform docker-compose.yml to replace the legacy .env with split files.
    Returns original bytes for rollback.
    Raises ComposeTransformError on structural mismatch.
    """
    try:
        src_fd, _ = safe_open_source(source_path)
        with os.fdopen(src_fd, "rb") as f:
            original_bytes = f.read()
    except Exception as exc:
        raise ComposeTransformError(f"Failed to read source: {exc}")

    lines = original_bytes.decode("utf-8").splitlines(keepends=True)
    start_idx, end_idx = _find_env_file_span(lines)

    # Determine indentation from the matching line
    match_line = lines[start_idx]
    list_indent = match_line[: len(match_line) - len(match_line.lstrip())]

    replacement_lines = [
        f"{list_indent}- {PROD_RUNTIME_ENV_PATH}\n",
        f"{list_indent}- path: {PROD_SECRET_FILE_PATH}\n",
        f"{list_indent}  format: raw\n",
    ]

    new_lines = lines[:start_idx] + replacement_lines + lines[end_idx + 1 :]
    new_bytes = "".join(new_lines).encode("utf-8")

    # Verify unrelated bytes: everything before start and after end must be identical
    before_orig = "".join(lines[:start_idx])
    after_orig = "".join(lines[end_idx + 1 :])
    before_new = "".join(new_lines[:start_idx])
    after_new = "".join(new_lines[start_idx + len(replacement_lines) :])

    if before_orig != before_new:
        raise ComposeTransformError("Bytes before transform span were mutated")
    if after_orig != after_new:
        raise ComposeTransformError("Bytes after transform span were mutated")

    from ops.secret_remediation_r1.safe_fs import publish_file, replace_existing_file

    try:
        if source_path == destination_path:
            replace_existing_file(destination_path, new_bytes)
        else:
            publish_file(destination_path, new_bytes, mode=0o644)
    except SafeFsError as exc:
        raise ComposeTransformError(f"Publication failed: {exc}") from exc

    return original_bytes
