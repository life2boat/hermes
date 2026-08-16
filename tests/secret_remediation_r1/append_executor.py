
def test_executor_rollback_on_health_failure(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n")
    override = tmp_path / "override.yml"
    override.write_bytes(b"services:\n  hermes-bot:\n    environment:\n      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n")
    parent = tmp_path / "parent"
    
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\nNORMAL=val\n")
    
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    
    import ops.secret_remediation_r1.preflight as preflight
    monkeypatch.setattr(preflight, "run_compose_preflight", lambda: None)
    
    import ops.secret_remediation_r1.secret_transfer as secret_transfer
    monkeypatch.setattr(secret_transfer, "resolve_poller_pid", lambda docker=None: (123, type("ID", (), {"container_id": "123"})))
    monkeypatch.setattr(secret_transfer, "read_poller_environ", lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00")
    
    import ops.secret_remediation_r1.compose_command as compose_command
    monkeypatch.setattr(compose_command, "run_recreate", lambda: None)
    
    import ops.secret_remediation_r1.source_invariant as source_invariant
    monkeypatch.setattr(source_invariant, "verify_source_invariant", lambda *args, **kwargs: None)
    
    import ops.secret_remediation_r1.runtime_invariant as runtime_invariant
    monkeypatch.setattr(runtime_invariant, "verify_runtime_invariants", lambda docker=None: None)
    
    import ops.secret_remediation_r1.poller_checker as poller_checker
    monkeypatch.setattr(poller_checker, "check_exactly_one_poller", lambda docker=None: None)
    
    # Force health check to fail
    import ops.secret_remediation_r1.health as health
    def fail_health(*args, **kwargs):
        raise Exception("Health failed")
    monkeypatch.setattr(health, "check_health", fail_health)
    
    # We expect ExecutorError which encapsulates the failure and triggers rollback
    with pytest.raises(ExecutorError, match="Health failed"):
        run_remediation(str(base), str(override), docker=MockDockerBackend())
    
    # Since rollback ran, the runtime files should not exist and base/override should be restored.
    assert not runtime_env.exists()
    assert not secret_file.exists()
    assert base.read_bytes() == b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
