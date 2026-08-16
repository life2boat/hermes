import os
import pytest
from ops.secret_remediation_r1.source_invariant import verify_source_invariant, SourceState, SourceInvariantError

def test_verify_source_invariant_success(tmp_path):
    legacy = tmp_path / "legacy.env"
    legacy_content = b"NORMAL=1\nTELEGRAM_BOT_TOKEN=a\n"
    legacy.write_bytes(legacy_content)
    
    runtime = tmp_path / "runtime.env"
    runtime.write_bytes(b"NORMAL=1\n")
    
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"TELEGRAM_BOT_TOKEN=a\n")
    
    prestate = SourceState(legacy_env_bytes=legacy_content, dashscope_present_before=False)
    
    # Should not raise
    verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

def test_verify_source_invariant_legacy_mutated(tmp_path):
    legacy = tmp_path / "legacy.env"
    legacy_content = b"NORMAL=1\nTELEGRAM_BOT_TOKEN=a\n"
    legacy.write_bytes(b"MUTATED")
    
    runtime = tmp_path / "runtime.env"
    runtime.write_bytes(b"NORMAL=1\n")
    
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"TELEGRAM_BOT_TOKEN=a\n")
    
    prestate = SourceState(legacy_env_bytes=legacy_content, dashscope_present_before=False)
    
    with pytest.raises(SourceInvariantError, match="Legacy .env was mutated"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

def test_verify_source_invariant_protected_in_runtime(tmp_path):
    legacy = tmp_path / "legacy.env"
    legacy_content = b"NORMAL=1\n"
    legacy.write_bytes(legacy_content)
    
    runtime = tmp_path / "runtime.env"
    runtime.write_bytes(b"TELEGRAM_BOT_TOKEN=leak\n")
    
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"TELEGRAM_BOT_TOKEN=a\n")
    
    prestate = SourceState(legacy_env_bytes=legacy_content, dashscope_present_before=False)
    
    with pytest.raises(SourceInvariantError, match="Protected names in runtime env"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))

@pytest.mark.skipif(os.name == "nt", reason="Linux-only uid/mode checks")
def test_verify_source_invariant_wrong_mode_uid(tmp_path):
    legacy = tmp_path / "legacy.env"
    legacy_content = b"NORMAL=1\n"
    legacy.write_bytes(legacy_content)
    
    runtime = tmp_path / "runtime.env"
    runtime.write_bytes(b"NORMAL=1\n")
    
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"TELEGRAM_BOT_TOKEN=a\n")
    # By default, uid will be the current user (which is likely not 0 if not root)
    # The check expects uid 0, so it will fail.
    
    prestate = SourceState(legacy_env_bytes=legacy_content, dashscope_present_before=False)
    
    with pytest.raises(SourceInvariantError, match="Secret file uid=.* expected 0"):
        verify_source_invariant(prestate, str(legacy), str(runtime), str(secret))
