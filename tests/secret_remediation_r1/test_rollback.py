"""Tests for rollback module.

Verifies:
  - capture_prestate captures exact bytes and mode.
  - execute_rollback removes newly created env files via dirfd-safe operations.
  - execute_rollback refuses to remove the parent dir when non-empty.
  - execute_rollback does NOT mutate the legacy .env.
  - Restoring base and override writes the captured bytes.
"""
import os
import pytest
from ops.secret_remediation_r1.rollback import (
    capture_prestate,
    execute_rollback,
    RemediationPrestate,
    RollbackError,
)


def _mock_rollback_side_effects(monkeypatch, parent, runtime_env, secret_file):
    """Patch all runtime side-effects so execute_rollback can run without Docker."""
    import ops.secret_remediation_r1.constants as constants
    import ops.secret_remediation_r1.compose_command
    import ops.secret_remediation_r1.runtime_invariant
    import ops.secret_remediation_r1.poller_checker
    import ops.secret_remediation_r1.health

    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(
        ops.secret_remediation_r1.compose_command, "run_recreate", lambda: None
    )
    monkeypatch.setattr(
        ops.secret_remediation_r1.runtime_invariant,
        "verify_runtime_invariants",
        lambda docker=None: None,
    )
    monkeypatch.setattr(
        ops.secret_remediation_r1.poller_checker,
        "check_exactly_one_poller",
        lambda docker=None: None,
    )
    monkeypatch.setattr(
        ops.secret_remediation_r1.health, "check_health", lambda docker=None: None
    )


def test_rollback_restore_base_exact_bytes(tmp_path, monkeypatch):
    """execute_rollback must restore the base compose file to its captured bytes."""
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()

    import ops.secret_remediation_r1.constants as constants
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(b"legacy")
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy))

    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    _mock_rollback_side_effects(monkeypatch, parent, runtime_env, secret_file)

    prestate = capture_prestate(str(base), str(override), str(parent))
    assert prestate.base_compose_bytes == b"base"

    # Mutate and then restore via a minimal prestate + execute_rollback.
    base.write_bytes(b"mutated")
    execute_rollback(prestate)
    assert base.read_bytes() == b"base"


def test_rollback_restore_override_exact_bytes(tmp_path, monkeypatch):
    """execute_rollback must restore the override file to its captured bytes."""
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()

    import ops.secret_remediation_r1.constants as constants
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(b"legacy")
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy))

    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    _mock_rollback_side_effects(monkeypatch, parent, runtime_env, secret_file)

    prestate = capture_prestate(str(base), str(override), str(parent))
    assert prestate.override_bytes == b"override"

    override.write_bytes(b"mutated")
    execute_rollback(prestate)
    assert override.read_bytes() == b"override"


def test_rollback_remove_new_runtime_file(tmp_path, monkeypatch):
    """execute_rollback must remove newly created runtime.env and then the parent dir."""
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()

    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    _mock_rollback_side_effects(monkeypatch, parent, runtime_env, secret_file)

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

    # Simulate creation of the new runtime file.
    runtime_env.write_bytes(b"runtime")

    execute_rollback(prestate)

    assert not runtime_env.exists()
    assert not parent.exists()
    assert base.read_bytes() == b"base"


def test_rollback_parent_not_empty_fail(tmp_path, monkeypatch):
    """execute_rollback must fail if the parent dir contains unexpected children."""
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()

    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    _mock_rollback_side_effects(monkeypatch, parent, runtime_env, secret_file)

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

    # Unexpected file inside parent.
    unexpected = parent / "unexpected.txt"
    unexpected.write_bytes(b"not mine")

    with pytest.raises(RollbackError, match="Config restore failed.*rollback_parent_not_empty"):
        execute_rollback(prestate)

    assert parent.exists()
    assert unexpected.exists()
    assert base.read_bytes() == b"base"


def test_rollback_legacy_env_untouched(tmp_path, monkeypatch):
    """execute_rollback must not modify the legacy .env file."""
    base = tmp_path / "base.yml"
    base.write_bytes(b"base")
    override = tmp_path / "override.yml"
    override.write_bytes(b"override")
    parent = tmp_path / "parent"
    parent.mkdir()

    legacy_env = tmp_path / "legacy.env"
    legacy_content = b"# comment\nTELEGRAM_BOT_TOKEN=old\n\nNORMAL=val\n"
    legacy_env.write_bytes(legacy_content)

    import ops.secret_remediation_r1.constants as constants
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    _mock_rollback_side_effects(monkeypatch, parent, runtime_env, secret_file)

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
