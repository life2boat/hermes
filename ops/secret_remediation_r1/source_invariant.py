"""Post-remediation source invariant checker."""
from __future__ import annotations
import os
import stat
from dataclasses import dataclass
from ops.secret_remediation_r1.constants import PROTECTED_NAMES
from ops.secret_remediation_r1.safe_fs import safe_open_source, SafeFsError


class SourceInvariantError(Exception):
    pass


@dataclass
class SourceState:
    legacy_env_bytes: bytes  # captured before mutation
    dashscope_present_before: bool


def _read_env_keys(path: str) -> set[str]:
    """Read variable names from a plain env file."""
    try:
        fd, _ = safe_open_source(path)
        with os.fdopen(fd, "rb") as f:
            content = f.read()
    except Exception as exc:
        raise SourceInvariantError(f"Failed to read {path}: {exc}")
    keys = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        if b"=" in stripped:
            keys.add(stripped.split(b"=", 1)[0].strip().decode("utf-8", errors="ignore"))
    return keys


def verify_source_invariant(
    prestate: SourceState,
    legacy_env_path: str,
    runtime_env_path: str,
    secret_file_path: str,
) -> None:
    """
    Verify all post-remediation source invariants.
    Raises SourceInvariantError on any violation.
    """
    # A. Legacy .env unchanged
    try:
        fd, _ = safe_open_source(legacy_env_path)
        with os.fdopen(fd, "rb") as f:
            current_legacy = f.read()
    except Exception as exc:
        raise SourceInvariantError(f"Legacy env read failed: {exc}")

    if current_legacy != prestate.legacy_env_bytes:
        raise SourceInvariantError("Legacy .env was mutated")

    # C. Runtime env file exists, regular, no protected names
    try:
        st = os.lstat(runtime_env_path)
    except OSError as exc:
        raise SourceInvariantError(f"Runtime env missing: {exc}")
    if stat.S_ISLNK(st.st_mode):
        raise SourceInvariantError("Runtime env is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise SourceInvariantError("Runtime env is not a regular file")
    runtime_keys = _read_env_keys(runtime_env_path)
    protected_in_runtime = runtime_keys & PROTECTED_NAMES
    if protected_in_runtime:
        raise SourceInvariantError(
            f"Protected names in runtime env: {protected_in_runtime}"
        )

    # D. Secret file exists, regular, uid=0, mode=0600
    try:
        secret_st = os.lstat(secret_file_path)
    except OSError as exc:
        raise SourceInvariantError(f"Secret file missing: {exc}")
    if stat.S_ISLNK(secret_st.st_mode):
        raise SourceInvariantError("Secret file is a symlink")
    if not stat.S_ISREG(secret_st.st_mode):
        raise SourceInvariantError("Secret file is not a regular file")
    if os.name != "nt":
        if secret_st.st_uid != 0:
            raise SourceInvariantError(f"Secret file uid={secret_st.st_uid}, expected 0")
        if (secret_st.st_mode & 0o777) != 0o600:
            raise SourceInvariantError(
                f"Secret file mode={oct(secret_st.st_mode & 0o777)}, expected 0600"
            )

    # Enforce no keys dropped or added during split
    def _parse_bytes_keys(b: bytes) -> set[str]:
        keys = set()
        for line in b.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(b"#"):
                continue
            if b"=" in stripped:
                keys.add(stripped.split(b"=", 1)[0].strip().decode("utf-8", errors="ignore"))
        return keys

    legacy_keys = _parse_bytes_keys(prestate.legacy_env_bytes)
    dashscope_before = "DASHSCOPE_API_KEY" in legacy_keys
    
    secret_keys = _read_env_keys(secret_file_path)
    combined_keys = runtime_keys | secret_keys
    
    if legacy_keys != combined_keys:
        missing = legacy_keys - combined_keys
        added = combined_keys - legacy_keys
        raise SourceInvariantError(f"Keys mismatch after split. Missing: {missing}, Added: {added}")
        
    dashscope_after = "DASHSCOPE_API_KEY" in combined_keys
    if prestate.dashscope_present_before != dashscope_after:
        raise SourceInvariantError("dashscope_present_before != dashscope_after")
        
    # Wait, secret_keys_before == secret_keys_after
    secret_keys_before = legacy_keys & PROTECTED_NAMES
    secret_keys_after = secret_keys & PROTECTED_NAMES
    if secret_keys_before != secret_keys_after:
        raise SourceInvariantError(f"Secret keys changed: {secret_keys_before} != {secret_keys_after}")

    # Enforce no keys dropped or added during split
    def _parse_bytes_keys(b: bytes) -> set[str]:
        keys = set()
        for line in b.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(b"#"):
                continue
            if b"=" in stripped:
                keys.add(stripped.split(b"=", 1)[0].strip().decode("utf-8", errors="ignore"))
        return keys

    legacy_keys = _parse_bytes_keys(prestate.legacy_env_bytes)
    dashscope_before = "DASHSCOPE_API_KEY" in legacy_keys
    
    secret_keys = _read_env_keys(secret_file_path)
    combined_keys = runtime_keys | secret_keys
    
    if legacy_keys != combined_keys:
        missing = legacy_keys - combined_keys
        added = combined_keys - legacy_keys
        raise SourceInvariantError(f"Keys mismatch after split. Missing: {missing}, Added: {added}")
        
    dashscope_after = "DASHSCOPE_API_KEY" in combined_keys
    if prestate.dashscope_present_before != dashscope_after:
        raise SourceInvariantError("dashscope_present_before != dashscope_after")
        
    # Wait, secret_keys_before == secret_keys_after
    secret_keys_before = legacy_keys & PROTECTED_NAMES
    secret_keys_after = secret_keys & PROTECTED_NAMES
    if secret_keys_before != secret_keys_after:
        raise SourceInvariantError(f"Secret keys changed: {secret_keys_before} != {secret_keys_after}")
