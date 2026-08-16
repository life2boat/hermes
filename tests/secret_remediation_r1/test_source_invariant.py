import os
import stat
import pytest
from ops.secret_remediation_r1.source_invariant import verify_source_invariant, SourceState, SourceInvariantError

class SyntheticStat:
    def __init__(self, st_mode, st_uid):
        self.st_mode = st_mode
        self.st_uid = st_uid

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
    original_lstat = os.lstat
    def mock_lstat(path):
        if str(path) == str(secret_path):
            return SyntheticStat(st_mode=mode, st_uid=uid)
        return original_lstat(path)
    monkeypatch.setattr(os, "lstat", mock_lstat)
    monkeypatch.setattr(os, "name", "posix")

def test_verify_source_invariant_success(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)

    # mode = S_IFREG | 0600
    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    # Should not raise
    verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

def test_verify_source_invariant_legacy_mutated(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    legacy.write_bytes(b"MUTATED")

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Legacy .env was mutated"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

def test_verify_source_invariant_protected_in_runtime(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    runtime.write_bytes(b"TELEGRAM_BOT_TOKEN=leak\n")

    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Protected names in runtime env"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

def test_secret_uid_nonzero_rejected(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    valid_mode = stat.S_IFREG | 0o600
    _patch_lstat(monkeypatch, secret, mode=valid_mode, uid=1001)

    with pytest.raises(SourceInvariantError, match="Secret file uid=1001, expected 0"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

def test_secret_mode_not_0600_rejected(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    wrong_mode = stat.S_IFREG | 0o644
    _patch_lstat(monkeypatch, secret, mode=wrong_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Secret file mode=0o644, expected 0600"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

def test_secret_symlink_rejected(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    symlink_mode = stat.S_IFLNK | 0o600
    _patch_lstat(monkeypatch, secret, mode=symlink_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Secret file is a symlink"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

def test_secret_nonregular_rejected(tmp_path, monkeypatch):
    legacy, runtime, secret, prestate = _setup_files(tmp_path)
    dir_mode = stat.S_IFDIR | 0o600
    _patch_lstat(monkeypatch, secret, mode=dir_mode, uid=0)

    with pytest.raises(SourceInvariantError, match="Secret file is not a regular file"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))
