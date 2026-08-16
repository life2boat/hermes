"""Tests for source_invariant.verify_source_invariant.

Covers:
  - uid=0 success (root uid)
  - non-root uid rejection
  - mode 0600 success
  - wrong mode rejection
  - symlink rejection
  - non-regular file rejection
  - legacy env mutated detection
  - protected name in runtime env detection
  - protected name set changed after remediation
  - DASHSCOPE preservation check
"""
import os
import stat
import pytest
from ops.secret_remediation_r1.source_invariant import (
    verify_source_invariant,
    SourceState,
    SourceInvariantError,
)


def _real_stat_with_overrides(path: str, *, st_mode: int, st_uid: int) -> os.stat_result:
    """Return a real os.stat_result with st_mode and st_uid replaced.

    All other fields (st_ino, st_dev, st_size, st_atime, st_mtime, st_ctime,
    st_nlink, st_gid) come from the real lstat, so that safe_open_source()'s
    lstat/fstat identity check (st_ino + st_dev) sees consistent values on Linux.
    """
    real = os.lstat(path)
    return os.stat_result((
        st_mode,
        real.st_ino,
        real.st_dev,
        real.st_nlink,
        st_uid,
        real.st_gid,
        real.st_size,
        real.st_atime,
        real.st_mtime,
        real.st_ctime,
    ))


def _setup_files(tmp_path):
    legacy = tmp_path / "legacy.env"
    legacy_content = b"NORMAL=1\nTELEGRAM_BOT_TOKEN=a\n"
    legacy.write_bytes(legacy_content)

    runtime = tmp_path / "runtime.env"
    runtime.write_bytes(b"NORMAL=1\n")

    secret = tmp_path / "secret.env"
    secret.write_bytes(b"TELEGRAM_BOT_TOKEN=a\n")

    prestate = SourceState(legacy_env_bytes=legacy_content, dashscope_present_before=False)
    return legacy, runtime, secret, prestate


def _patch_lstat(monkeypatch, secret_path, mode, uid):
    """Patch os.lstat to return a real-stat-compatible result for the secret path.

    The wrapper delegates st_ino and st_dev from the real stat so that
    safe_open_source()'s TOCTOU identity check succeeds on Linux.
    """
    original_lstat = os.lstat

    def mock_lstat(path, *, dir_fd=None, follow_symlinks=True):
        if str(path) == str(secret_path):
            return _real_stat_with_overrides(str(secret_path), st_mode=mode, st_uid=uid)
        # Delegate everything else to the real lstat.
        return original_lstat(path)

    monkeypatch.setattr(os, "lstat", mock_lstat)
    monkeypatch.setattr(os, "name", "posix")


# ─── uid / mode success path ──────────────────────────────────────────────

def test_verify_source_invariant_success(tmp_path, monkeypatch):
    """Root uid + mode 0600 → no exception raised."""
    legacy, runtime, secret, prestate = _setup_files(tmp_path)

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    # Should not raise
    verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


# ─── Legacy env mutation detection ──────────────────────────────────────

def test_verify_source_invariant_legacy_mutated(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    legacy.write_bytes(b"MUTATED")

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Legacy .env was mutated"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


# ─── Protected name in runtime env ──────────────────────────────────────

def test_verify_source_invariant_protected_in_runtime(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    runtime.write_bytes(b"TELEGRAM_BOT_TOKEN=leak\n")

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Protected names in runtime env"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


# ─── Non-root uid rejection ───────────────────────────────────────────

def test_secret_uid_nonzero_rejected(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=1001)

    with pytest.raises(SourceInvariantError, match="Secret file uid=1001, expected 0"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


# ─── Wrong mode rejection ────────────────────────────────────────────

def test_secret_mode_not_0600_rejected(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    wrong_mode = stat.S_IFREG | 0o644
    _patch_lstat(monkeypatch, secret, mode=wrong_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Secret file mode=0o644, expected 0600"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


# ─── Symlink rejection ───────────────────────────────────────────────

def test_secret_symlink_rejected(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    symlink_mode = stat.S_IFLNK | 0o600
    _patch_lstat(monkeypatch, secret, mode=symlink_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Secret file is a symlink"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


# ─── Non-regular file rejection ─────────────────────────────────────

def test_secret_nonregular_rejected(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    dir_mode = stat.S_IFDIR | 0o600
    _patch_lstat(monkeypatch, secret, mode=dir_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Secret file is not a regular file"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


# ─── Protected name set changed after remediation ───────────────────

def test_protected_name_set_added(tmp_path, monkeypatch):
    """Secret file contains a protected name not present before → rejected."""
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    # Prestate has only TELEGRAM_BOT_TOKEN; secret file now also has DASHSCOPE
    secret.write_bytes(b"TELEGRAM_BOT_TOKEN=a\nDASHSCOPE_API_KEY=x\n")

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Protected name set changed"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


def test_protected_name_set_removed(tmp_path, monkeypatch):
    """Secret file missing a protected name that was present before → rejected."""
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    # Prestate has TELEGRAM_BOT_TOKEN; secret file is empty
    secret.write_bytes(b"UNRELATED=x\n")

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Protected name set changed"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


# ─── DASHSCOPE preservation ──────────────────────────────────────────

def test_dashscope_appeared_rejected(tmp_path, monkeypatch):
    """DASHSCOPE was absent before but present after → rejected."""
    legacy_content = b"NORMAL=1\nTELEGRAM_BOT_TOKEN=a\n"
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(legacy_content)
    runtime = tmp_path / "runtime.env"
    runtime.write_bytes(b"NORMAL=1\n")
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"TELEGRAM_BOT_TOKEN=a\nDASHSCOPE_API_KEY=new\n")

    # dashscope_present_before=False, but it appears in secret → violation
    prestate = SourceState(legacy_env_bytes=legacy_content, dashscope_present_before=False)

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    with pytest.raises(SourceInvariantError):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))


def test_dashscope_preserved_when_present(tmp_path, monkeypatch):
    """DASHSCOPE present before and after → no error."""
    legacy_content = b"NORMAL=1\nTELEGRAM_BOT_TOKEN=a\nDASHSCOPE_API_KEY=x\n"
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(legacy_content)
    runtime = tmp_path / "runtime.env"
    runtime.write_bytes(b"NORMAL=1\n")
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"TELEGRAM_BOT_TOKEN=a\nDASHSCOPE_API_KEY=x\n")

    prestate = SourceState(legacy_env_bytes=legacy_content, dashscope_present_before=True)

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    # Should not raise
    verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))
