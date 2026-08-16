import pytest
import ops.secret_remediation_r1.constants as constants
from ops.secret_remediation_r1.executor import run_remediation, ExecutorError

class MockDockerBackend:
    def inspect(self, name):
        return [{
            "Id": "12345",
            "Image": constants.LEGACY_IMAGE_ID,
            "State": {"Running": True},
            "Config": {
                "Image": constants.LEGACY_IMAGE_REF,
                "Labels": {
                    "com.docker.compose.image": constants.LEGACY_IMAGE_REF,
                }
            }
        }]

def test_executor_success(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n")
    override = tmp_path / "override.yml"
    override.write_bytes(b"services:\n  hermes-bot:\n    environment:\n      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n")
    parent = tmp_path / "parent"
    parent.mkdir(parents=True, exist_ok=True)
    
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\nNORMAL=val\n")
    
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    
    import ops.secret_remediation_r1.executor as executor_module
    monkeypatch.setattr(executor_module, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(executor_module, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(executor_module, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(executor_module, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    
    import ops.secret_remediation_r1.preflight as preflight
    monkeypatch.setattr(preflight, "run_compose_preflight", lambda: None)
    
    import ops.secret_remediation_r1.secret_transfer as secret_transfer
    monkeypatch.setattr(secret_transfer, "resolve_poller_pid", lambda docker=None: (123, type("ID", (), {"container_id": "123"})))
    monkeypatch.setattr(secret_transfer, "read_poller_environ", lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00")
    
    import ops.secret_remediation_r1.compose_command as compose_command
    monkeypatch.setattr(compose_command, "run_recreate", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor_module, "run_recreate", lambda *args, **kwargs: None)
    
    import ops.secret_remediation_r1.source_invariant as source_invariant
    monkeypatch.setattr(source_invariant, "verify_source_invariant", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor_module, "verify_source_invariant", lambda *args, **kwargs: None)
    
    import ops.secret_remediation_r1.runtime_invariant as runtime_invariant
    monkeypatch.setattr(runtime_invariant, "verify_runtime_invariants", lambda docker=None: None)
    monkeypatch.setattr(executor_module, "verify_runtime_invariants", lambda docker=None: None)
    
    import ops.secret_remediation_r1.poller_checker as poller_checker
    monkeypatch.setattr(poller_checker, "check_exactly_one_poller", lambda docker=None: None)
    monkeypatch.setattr(executor_module, "check_exactly_one_poller", lambda docker=None: None)
    
    import ops.secret_remediation_r1.health as health
    monkeypatch.setattr(health, "check_health", lambda docker=None: None)
    monkeypatch.setattr(executor_module, "check_health", lambda docker=None: None)
    
    import ops.secret_remediation_r1.candidate_image_guard as candidate_image_guard
    monkeypatch.setattr(candidate_image_guard, "verify_legacy_image", lambda *args, **kwargs: None)
    
    monkeypatch.setattr(executor_module, "ensure_parent_directory", lambda path: None)
    
    def mock_publish(dest, content, mode=None, uid=None, gid=None, override_mode=None, **kwargs):
        import pathlib
        pathlib.Path(dest).write_bytes(content)
        return type("PublishResult", (), {"path": dest, "uid": 0, "gid": 0, "mode": mode})()
        
    import ops.secret_remediation_r1.safe_fs as safe_fs
    monkeypatch.setattr(safe_fs, "publish_file", mock_publish)
    monkeypatch.setattr(safe_fs, "replace_existing_file", mock_publish)
    
    import ops.secret_remediation_r1.override_transform as override_transform
    monkeypatch.setattr(override_transform, "publish_file", mock_publish)
    
    import ops.secret_remediation_r1.env_split as env_split
    monkeypatch.setattr(env_split, "publish_file", mock_publish)
    monkeypatch.setattr(secret_transfer, "publish_file", mock_publish)

    import ops.secret_remediation_r1.process_identity as process_identity
    monkeypatch.setattr(process_identity, "resolve_poller_pid", lambda docker=None: (123, type("ID", (), {"container_id": "123"})))
    monkeypatch.setattr(process_identity, "read_poller_environ", lambda pid, identity, docker=None: b"NORMAL=val\x00")
    
    run_remediation(str(base), str(override), docker=MockDockerBackend())
    # Should complete without error


def test_executor_rollback_on_health_failure(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n")
    override = tmp_path / "override.yml"
    override.write_bytes(b"services:\n  hermes-bot:\n    environment:\n      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n")
    parent = tmp_path / "parent"
    parent.mkdir(parents=True, exist_ok=True)
    
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\nNORMAL=val\n")
    
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"
    
    import ops.secret_remediation_r1.executor as executor_module
    monkeypatch.setattr(executor_module, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(executor_module, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(executor_module, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(executor_module, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    
    import ops.secret_remediation_r1.preflight as preflight
    monkeypatch.setattr(preflight, "run_compose_preflight", lambda: None)
    
    import ops.secret_remediation_r1.secret_transfer as secret_transfer
    monkeypatch.setattr(secret_transfer, "resolve_poller_pid", lambda docker=None: (123, type("ID", (), {"container_id": "123"})))
    monkeypatch.setattr(secret_transfer, "read_poller_environ", lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00")
    
    import ops.secret_remediation_r1.compose_command as compose_command
    monkeypatch.setattr(compose_command, "run_recreate", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor_module, "run_recreate", lambda *args, **kwargs: None)
    
    import ops.secret_remediation_r1.source_invariant as source_invariant
    monkeypatch.setattr(source_invariant, "verify_source_invariant", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor_module, "verify_source_invariant", lambda *args, **kwargs: None)
    
    import ops.secret_remediation_r1.runtime_invariant as runtime_invariant
    monkeypatch.setattr(runtime_invariant, "verify_runtime_invariants", lambda docker=None: None)
    monkeypatch.setattr(executor_module, "verify_runtime_invariants", lambda docker=None: None)
    
    import ops.secret_remediation_r1.poller_checker as poller_checker
    monkeypatch.setattr(poller_checker, "check_exactly_one_poller", lambda docker=None: None)
    monkeypatch.setattr(executor_module, "check_exactly_one_poller", lambda docker=None: None)
    
    import ops.secret_remediation_r1.health as health
    def fail_health(*args, **kwargs):
        raise Exception("Health failed")
    monkeypatch.setattr(health, "check_health", fail_health)
    monkeypatch.setattr(executor_module, "check_health", fail_health)
    
    monkeypatch.setattr(executor_module, "ensure_parent_directory", lambda path: None)
    
    import ops.secret_remediation_r1.candidate_image_guard as candidate_image_guard
    monkeypatch.setattr(candidate_image_guard, "verify_legacy_image", lambda *args, **kwargs: None)

    def mock_publish(dest, content, mode=None, uid=None, gid=None, override_mode=None, **kwargs):
        import pathlib
        pathlib.Path(dest).write_bytes(content)
        return type("PublishResult", (), {"path": dest, "uid": 0, "gid": 0, "mode": mode})()
        
    import ops.secret_remediation_r1.safe_fs as safe_fs
    monkeypatch.setattr(safe_fs, "publish_file", mock_publish)
    monkeypatch.setattr(safe_fs, "replace_existing_file", mock_publish)
    
    import ops.secret_remediation_r1.env_split as env_split
    monkeypatch.setattr(env_split, "publish_file", mock_publish)
    monkeypatch.setattr(secret_transfer, "publish_file", mock_publish)
    
    with pytest.raises(ExecutorError, match="Health failed"):
        run_remediation(str(base), str(override), docker=MockDockerBackend())
    
    assert base.read_bytes() == b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"