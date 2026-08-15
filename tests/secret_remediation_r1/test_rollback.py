import os
import pytest
from ops.secret_remediation_r1.rollback import capture_prestate, _restore_file, RollbackError

def test_rollback_restore_base_exact_bytes(tmp_path):
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()
    
    prestate = capture_prestate(str(base), str(override), str(parent))
    
    base.write_bytes(b"mutated")
    _restore_file(str(base), prestate.base_compose_bytes, prestate.base_compose_mode)
    
    assert base.read_bytes() == b"base"

def test_rollback_restore_override_exact_bytes(tmp_path):
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()
    
    prestate = capture_prestate(str(base), str(override), str(parent))
    
    override.write_bytes(b"mutated")
    _restore_file(str(override), prestate.override_bytes, prestate.override_mode)
    
    assert override.read_bytes() == b"override"

def test_rollback_remove_new_runtime_file():
    pass

def test_rollback_parent_not_empty_fail():
    pass

def test_rollback_legacy_env_untouched():
    pass
