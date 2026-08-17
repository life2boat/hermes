"""Split legacy .env into non-secret runtime artifact."""

from __future__ import annotations
import os
from ops.secret_remediation_r1.constants import PROTECTED_NAMES
from ops.secret_remediation_r1.safe_fs import (
    safe_open_source,
    publish_file,
    SafeFsError,
)


class EnvSplitError(Exception):
    pass


def _parse_env_lines(raw_bytes: bytes) -> list[tuple[str | None, bytes]]:
    """
    Parse .env file lines into (key_or_None, original_line_bytes) pairs.
    Comments and blank lines have key=None.
    Raises EnvSplitError on malformed/ambiguous records.
    """
    result: list[tuple[str | None, bytes]] = []
    seen_keys: set[str] = set()

    for line_bytes in raw_bytes.splitlines(keepends=True):
        stripped = line_bytes.strip()

        # Blank or comment
        if not stripped or stripped.startswith(b"#"):
            result.append((None, line_bytes))
            continue

        if b"=" not in stripped:
            raise EnvSplitError(f"Malformed record (no '='): {stripped[:40]!r}")

        key_bytes = stripped.split(b"=", 1)[0].strip()
        try:
            key = key_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvSplitError(f"Non-UTF-8 key: {exc}")

        if not key:
            raise EnvSplitError("Empty key")

        if key in seen_keys:
            raise EnvSplitError(f"Duplicate key: {key!r}")
        seen_keys.add(key)

        result.append((key, line_bytes))

    return result


def split_env(
    source_path: str,
    destination_path: str,
) -> None:
    """
    Read source .env, remove protected entries, publish non-secret artifact.
    Source is never modified.
    Performs in-memory comparison after publication.
    """
    # Safe-open source
    try:
        src_fd, _ = safe_open_source(source_path)
        with os.fdopen(src_fd, "rb") as f:
            source_bytes = f.read()
    except Exception as exc:
        raise EnvSplitError(f"Failed to read source: {exc}")

    parsed = _parse_env_lines(source_bytes)

    # Build output: exclude protected names
    out_lines = []
    for key, line in parsed:
        if key is not None and key in PROTECTED_NAMES:
            continue
        out_lines.append(line)

    dest_bytes = b"".join(out_lines)

    try:
        publish_file(destination_path, dest_bytes, mode=0o644)
    except SafeFsError as exc:
        raise EnvSplitError(f"Publication failed: {exc}") from exc

    # Re-open and verify
    try:
        dest_fd, _ = safe_open_source(destination_path)
        with os.fdopen(dest_fd, "rb") as f:
            actual_dest_bytes = f.read()
    except Exception as exc:
        raise EnvSplitError(f"Failed to re-read destination: {exc}")

    if actual_dest_bytes != dest_bytes:
        raise EnvSplitError("Destination byte verification failed")

    # Verify source unchanged
    try:
        src_fd2, _ = safe_open_source(source_path)
        with os.fdopen(src_fd2, "rb") as f:
            source_bytes_after = f.read()
    except Exception as exc:
        raise EnvSplitError(f"Failed to re-read source: {exc}")

    if source_bytes_after != source_bytes:
        raise EnvSplitError("Source was mutated during operation")

    # Verify no protected names in destination
    dest_parsed = _parse_env_lines(actual_dest_bytes)
    for key, _ in dest_parsed:
        if key and key in PROTECTED_NAMES:
            raise EnvSplitError(f"Protected name found in destination: {key}")
