import os
import pytest
from ops.secret_remediation_r1.rollback import capture_prestate, _restore_file, RollbackError

def test_rollback_restore_base_exact_bytes(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()
    
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(b"legacy")
    import ops.secret_remediation_r1.constants as constants
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy))
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(b"legacy")
    import ops.secret_remediation_r1.constants as constants
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy))
    prestate = capture_prestate(str(base), str(override), str(parent))
    
    base.write_bytes(b"mutated")
    _restore_file(str(base), prestate.base_compose_bytes, prestate.base_compose_mode)
    
    assert base.read_bytes() == b"base"

def test_rollback_restore_override_exact_bytes(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()
    
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(b"legacy")
    import ops.secret_remediation_r1.constants as constants
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy))
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(b"legacy")
    import ops.secret_remediation_r1.constants as constants
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy))
    prestate = capture_prestate(str(base), str(override), str(parent))
    
    override.write_bytes(b"mutated")
    _restore_file(str(override), prestate.override_bytes, prestate.override_mode)
    
    assert override.read_bytes() == b"override"

def test_rollback_remove_new_runtime_file(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()
    
    # Mock constants
    import ops.secret_remediation_r1.constants as constants
    from ops.secret_remediation_r1.rollback import execute_rollback, RemediationPrestate
    
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    
    # Mock runtime components
    import ops.secret_remediation_r1.compose_command
    import ops.secret_remediation_r1.runtime_invariant
    import ops.secret_remediation_r1.poller_checker
    import ops.secret_remediation_r1.health
    
    monkeypatch.setattr(ops.secret_remediation_r1.compose_command, "run_recreate", lambda: None)
    monkeypatch.setattr(ops.secret_remediation_r1.runtime_invariant, "verify_runtime_invariants", lambda docker=None: None)
    monkeypatch.setattr(ops.secret_remediation_r1.poller_checker, "check_exactly_one_poller", lambda docker=None: None)
    monkeypatch.setattr(ops.secret_remediation_r1.health, "check_health", lambda docker=None: None)
    
    # Mock open and other things to avoid test fail
    
    prestate = RemediationPrestate(
        legacy_env_bytes=b"legacy",
        base_compose_bytes=b"base",
        base_compose_path=str(base),
        override_bytes=b"override",
        override_path=str(override),
        base_compose_mode=0o644,
        override_mode=0o644,
        created_parent_dir=True,
    )
    
    # Simulate creation of new runtime file
    runtime_env.write_bytes(b"runtime")
    
    execute_rollback(prestate)
    
    assert not runtime_env.exists()
    assert not parent.exists()
    assert base.read_bytes() == b"base"

def test_rollback_parent_not_empty_fail(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()
    
    # Mock constants
    import ops.secret_remediation_r1.constants as constants
    from ops.secret_remediation_r1.rollback import execute_rollback, RemediationPrestate, RollbackError
    
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    
    prestate = RemediationPrestate(
        legacy_env_bytes=b"legacy",
        base_compose_bytes=b"base",
        base_compose_path=str(base),
        override_bytes=b"override",
        override_path=str(override),
        base_compose_mode=0o644,
        override_mode=0o644,
        created_parent_dir=True,
    )
    
    # Unexpected file inside parent
    unexpected = parent / "unexpected.txt"
    unexpected.write_bytes(b"not mine")
    
    with pytest.raises(RollbackError, match="Config restore failed: rollback_parent_not_empty"):
        execute_rollback(prestate)
        
    assert parent.exists()
    assert unexpected.exists()
    assert base.read_bytes() == b"base"

def test_rollback_legacy_env_untouched(tmp_path, monkeypatch):
    # The current execution doesn't mutate legacy .env during rollback, but let's test it anyway.
    import ops.secret_remediation_r1.constants as constants
    from ops.secret_remediation_r1.rollback import execute_rollback, RemediationPrestate
    
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()
    
    legacy_env = tmp_path / "legacy.env"
    legacy_content = b"# comment\nTELEGRAM_BOT_TOKEN=old\n\nNORMAL=val\n"
    legacy_env.write_bytes(legacy_content)
    
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    
    # Mock runtime components
    import ops.secret_remediation_r1.compose_command
    import ops.secret_remediation_r1.runtime_invariant
    import ops.secret_remediation_r1.poller_checker
    import ops.secret_remediation_r1.health
    
    monkeypatch.setattr(ops.secret_remediation_r1.compose_command, "run_recreate", lambda: None)
    monkeypatch.setattr(ops.secret_remediation_r1.runtime_invariant, "verify_runtime_invariants", lambda docker=None: None)
    monkeypatch.setattr(ops.secret_remediation_r1.poller_checker, "check_exactly_one_poller", lambda docker=None: None)
    monkeypatch.setattr(ops.secret_remediation_r1.health, "check_health", lambda docker=None: None)
    
    prestate = RemediationPrestate(
        legacy_env_bytes=b"legacy",
        base_compose_bytes=b"base",
        base_compose_path=str(base),
        override_bytes=b"override",
        override_path=str(override),
        base_compose_mode=0o644,
        override_mode=0o644,
        created_parent_dir=False,
    )
    
    execute_rollback(prestate)
    
    assert legacy_env.read_bytes() == legacy_content
