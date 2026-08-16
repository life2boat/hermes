import pytest

def test_source_invariant_keys_mismatch(tmp_path):
    from ops.secret_remediation_r1.source_invariant import verify_source_invariant, SourceState, SourceInvariantError
    from ops.secret_remediation_r1.constants import PROTECTED_NAMES
    
    legacy_env = tmp_path / "legacy.env"
    runtime_env = tmp_path / "runtime.env"
    secret_file = tmp_path / "secret.env"
    
    # Missing a key in runtime
    legacy_bytes = b"NORMAL=1\nTELEGRAM_BOT_TOKEN=secret\nDROPPED=2"
    legacy_env.write_bytes(legacy_bytes)
    runtime_env.write_bytes(b"NORMAL=1\n")
    secret_file.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\n")
    
    import os
    if os.name != 'nt':
        os.chmod(secret_file, 0o600)
    
    prestate = SourceState(legacy_env_bytes=legacy_bytes, dashscope_present_before=False)
    with pytest.raises(SourceInvariantError, match="Keys mismatch after split"):
        verify_source_invariant(prestate, str(legacy_env), str(runtime_env), str(secret_file))

def test_source_invariant_secret_keys_mismatch(tmp_path):
    from ops.secret_remediation_r1.source_invariant import verify_source_invariant, SourceState, SourceInvariantError
    from ops.secret_remediation_r1.constants import PROTECTED_NAMES
    
    legacy_env = tmp_path / "legacy.env"
    runtime_env = tmp_path / "runtime.env"
    secret_file = tmp_path / "secret.env"
    
    # Secret added that wasn't in legacy
    legacy_bytes = b"NORMAL=1\nTELEGRAM_BOT_TOKEN=secret\n"
    legacy_env.write_bytes(legacy_bytes)
    runtime_env.write_bytes(b"NORMAL=1\n")
    secret_file.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\nDASHSCOPE_API_KEY=new\n")
    
    import os
    if os.name != 'nt':
        os.chmod(secret_file, 0o600)
    
    prestate = SourceState(legacy_env_bytes=legacy_bytes, dashscope_present_before=False)
    with pytest.raises(SourceInvariantError, match="Keys mismatch after split"):
        verify_source_invariant(prestate, str(legacy_env), str(runtime_env), str(secret_file))
