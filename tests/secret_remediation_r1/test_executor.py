import pytest
import ops.secret_remediation_r1.constants as constants
from ops.secret_remediation_r1.executor import run_remediation, ExecutorError


class MockDockerBackend:
    def inspect(self, name):
        return [
            {
                "Id": "12345",
                "Image": constants.LEGACY_IMAGE_ID,
                "State": {"Running": True},
                "Config": {
                    "Image": constants.LEGACY_IMAGE_REF,
                    "Labels": {
                        "com.docker.compose.image": constants.LEGACY_IMAGE_REF,
                    },
                },
            }
        ]


class MockImageBackend:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.inspected_refs = []

    def inspect_image(self, ref: str) -> dict:
        self.inspected_refs.append(ref)
        if self.should_fail:
            from ops.secret_remediation_r1.candidate_image_guard import (
                CandidateImageGuardError,
            )

            raise CandidateImageGuardError("Mock failure")
        return {"Id": constants.LEGACY_IMAGE_ID}


def test_executor_success(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(
        b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )
    override = tmp_path / "override.yml"
    override.write_bytes(
        b"services:\n  hermes-bot:\n    environment:\n      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n"
    )
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

    monkeypatch.setattr(
        secret_transfer,
        "resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )
    monkeypatch.setattr(
        secret_transfer,
        "read_poller_environ",
        lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00",
    )

    import ops.secret_remediation_r1.compose_command as compose_command

    monkeypatch.setattr(compose_command, "run_recreate", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor_module, "run_recreate", lambda *args, **kwargs: None)

    import ops.secret_remediation_r1.source_invariant as source_invariant

    monkeypatch.setattr(
        source_invariant, "verify_source_invariant", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        executor_module, "verify_source_invariant", lambda *args, **kwargs: None
    )

    import ops.secret_remediation_r1.runtime_invariant as runtime_invariant

    monkeypatch.setattr(
        runtime_invariant,
        "verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        executor_module,
        "verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )

    import ops.secret_remediation_r1.poller_checker as poller_checker

    monkeypatch.setattr(
        poller_checker,
        "check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        executor_module,
        "check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )

    import ops.secret_remediation_r1.health as health

    monkeypatch.setattr(health, "check_health", lambda expected=None, docker=None: None)
    monkeypatch.setattr(
        executor_module, "check_health", lambda expected=None, docker=None: None
    )

    import ops.secret_remediation_r1.candidate_image_guard as candidate_image_guard

    monkeypatch.setattr(executor_module, "ensure_parent_directory", lambda path: None)

    def mock_publish(
        dest, content, mode=None, uid=None, gid=None, override_mode=None, **kwargs
    ):
        import pathlib

        pathlib.Path(dest).write_bytes(content)
        return type(
            "PublishResult", (), {"path": dest, "uid": 0, "gid": 0, "mode": mode}
        )()

    import ops.secret_remediation_r1.safe_fs as safe_fs

    monkeypatch.setattr(safe_fs, "publish_file", mock_publish)
    monkeypatch.setattr(safe_fs, "replace_existing_file", mock_publish)

    import ops.secret_remediation_r1.override_transform as override_transform

    monkeypatch.setattr(override_transform, "publish_file", mock_publish)

    import ops.secret_remediation_r1.env_split as env_split

    monkeypatch.setattr(env_split, "publish_file", mock_publish)
    monkeypatch.setattr(secret_transfer, "publish_file", mock_publish)

    import ops.secret_remediation_r1.process_identity as process_identity

    monkeypatch.setattr(
        process_identity,
        "resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )
    monkeypatch.setattr(
        process_identity,
        "read_poller_environ",
        lambda pid, identity, docker=None: (
            b"TELEGRAM_BOT_TOKEN=synthetic_post_recreate_val\x00NORMAL=val\x00"
        ),
    )

    mock_image_backend = MockImageBackend()
    run_remediation(
        str(base),
        str(override),
        docker=MockDockerBackend(),
        image_backend=mock_image_backend,
    )
    assert mock_image_backend.inspected_refs == [
        constants.LEGACY_IMAGE_REF,
        constants.LEGACY_IMAGE_REF,
    ]


def test_executor_rollback_on_health_failure(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(
        b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )
    override = tmp_path / "override.yml"
    override.write_bytes(
        b"services:\n  hermes-bot:\n    environment:\n      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n"
    )
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

    monkeypatch.setattr(
        secret_transfer,
        "resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )
    monkeypatch.setattr(
        secret_transfer,
        "read_poller_environ",
        lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00",
    )

    import ops.secret_remediation_r1.compose_command as compose_command

    monkeypatch.setattr(compose_command, "run_recreate", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor_module, "run_recreate", lambda *args, **kwargs: None)

    import ops.secret_remediation_r1.source_invariant as source_invariant

    monkeypatch.setattr(
        source_invariant, "verify_source_invariant", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        executor_module, "verify_source_invariant", lambda *args, **kwargs: None
    )

    import ops.secret_remediation_r1.runtime_invariant as runtime_invariant

    monkeypatch.setattr(
        runtime_invariant,
        "verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        executor_module,
        "verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )

    import ops.secret_remediation_r1.poller_checker as poller_checker

    monkeypatch.setattr(
        poller_checker,
        "check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        executor_module,
        "check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )

    import ops.secret_remediation_r1.health as health

    def fail_health(*args, **kwargs):
        raise Exception("Health failed")

    monkeypatch.setattr(health, "check_health", fail_health)
    monkeypatch.setattr(executor_module, "check_health", fail_health)

    monkeypatch.setattr(executor_module, "ensure_parent_directory", lambda path: None)

    import ops.secret_remediation_r1.candidate_image_guard as candidate_image_guard

    def mock_publish(
        dest, content, mode=None, uid=None, gid=None, override_mode=None, **kwargs
    ):
        import pathlib

        pathlib.Path(dest).write_bytes(content)
        return type(
            "PublishResult", (), {"path": dest, "uid": 0, "gid": 0, "mode": mode}
        )()

    import ops.secret_remediation_r1.safe_fs as safe_fs

    monkeypatch.setattr(safe_fs, "publish_file", mock_publish)
    monkeypatch.setattr(safe_fs, "replace_existing_file", mock_publish)

    import ops.secret_remediation_r1.env_split as env_split

    monkeypatch.setattr(env_split, "publish_file", mock_publish)
    monkeypatch.setattr(secret_transfer, "publish_file", mock_publish)

    with pytest.raises(ExecutorError, match="Health failed"):
        run_remediation(
            str(base),
            str(override),
            docker=MockDockerBackend(),
            image_backend=MockImageBackend(),
        )

    assert (
        base.read_bytes()
        == b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )


def test_executor_post_runtime_nameset_missing(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(
        b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )
    override = tmp_path / "override.yml"
    override.write_bytes(
        b"services:\n  hermes-bot:\n    environment:\n      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n"
    )
    parent = tmp_path / "parent"
    parent.mkdir(parents=True, exist_ok=True)
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\nNORMAL=val\n")
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"

    import ops.secret_remediation_r1.executor as executor_module
    import ops.secret_remediation_r1.constants as constants

    monkeypatch.setattr(executor_module, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(executor_module, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(executor_module, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(executor_module, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy_env))

    monkeypatch.setattr(
        "ops.secret_remediation_r1.preflight.run_compose_preflight", lambda: None
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.read_poller_environ",
        lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00",
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.compose_command.run_recreate",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.run_recreate", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.source_invariant.verify_source_invariant",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.verify_source_invariant",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.runtime_invariant.verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.poller_checker.check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.health.check_health",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.check_health",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(executor_module, "ensure_parent_directory", lambda path: None)

    def mock_publish(
        dest, content, mode=None, uid=None, gid=None, override_mode=None, **kwargs
    ):
        import pathlib

        pathlib.Path(dest).write_bytes(content)
        return type(
            "PublishResult", (), {"path": dest, "uid": 0, "gid": 0, "mode": mode}
        )()

    monkeypatch.setattr("ops.secret_remediation_r1.safe_fs.publish_file", mock_publish)
    monkeypatch.setattr(
        "ops.secret_remediation_r1.safe_fs.replace_existing_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.override_transform.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.env_split.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.process_identity.resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )

    # Missing TELEGRAM_BOT_TOKEN
    monkeypatch.setattr(
        "ops.secret_remediation_r1.process_identity.read_poller_environ",
        lambda pid, identity, docker=None: b"NORMAL=val\x00",
    )

    with pytest.raises(
        ExecutorError, match="Post-recreate protected NAME set mismatch"
    ):
        run_remediation(
            str(base),
            str(override),
            docker=MockDockerBackend(),
            image_backend=MockImageBackend(),
        )

    # Verify rollback happened (base is restored)
    assert (
        base.read_bytes()
        == b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )


def test_executor_post_runtime_nameset_added(tmp_path, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_bytes(
        b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )
    override = tmp_path / "override.yml"
    override.write_bytes(
        b"services:\n  hermes-bot:\n    environment:\n      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n"
    )
    parent = tmp_path / "parent"
    parent.mkdir(parents=True, exist_ok=True)
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\n")
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"

    import ops.secret_remediation_r1.executor as executor_module
    import ops.secret_remediation_r1.constants as constants

    monkeypatch.setattr(executor_module, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(executor_module, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(executor_module, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(executor_module, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy_env))

    monkeypatch.setattr(
        "ops.secret_remediation_r1.preflight.run_compose_preflight", lambda: None
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.read_poller_environ",
        lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00",
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.compose_command.run_recreate",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.run_recreate", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.source_invariant.verify_source_invariant",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.verify_source_invariant",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.runtime_invariant.verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.poller_checker.check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.health.check_health",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.check_health",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(executor_module, "ensure_parent_directory", lambda path: None)

    def mock_publish(
        dest, content, mode=None, uid=None, gid=None, override_mode=None, **kwargs
    ):
        import pathlib

        pathlib.Path(dest).write_bytes(content)
        return type(
            "PublishResult", (), {"path": dest, "uid": 0, "gid": 0, "mode": mode}
        )()

    monkeypatch.setattr("ops.secret_remediation_r1.safe_fs.publish_file", mock_publish)
    monkeypatch.setattr(
        "ops.secret_remediation_r1.safe_fs.replace_existing_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.override_transform.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.env_split.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.process_identity.resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )

    # ADDED DASHSCOPE_API_KEY unexpectedly
    monkeypatch.setattr(
        "ops.secret_remediation_r1.process_identity.read_poller_environ",
        lambda pid, identity, docker=None: (
            b"TELEGRAM_BOT_TOKEN=synthetic\x00DASHSCOPE_API_KEY=synthetic2\x00"
        ),
    )

    with pytest.raises(
        ExecutorError, match="Post-recreate protected NAME set mismatch"
    ):
        run_remediation(
            str(base),
            str(override),
            docker=MockDockerBackend(),
            image_backend=MockImageBackend(),
        )

    assert (
        base.read_bytes()
        == b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )


@pytest.mark.parametrize(
    "failure_stage",
    [
        "secret_transfer",
        "pre_image_guard",
        "base_transform",
        "override_transform",
        "recreate",
        "post_image_guard",
        "effective_source_invariant",
        "runtime_invariant",
        "poller",
        "health",
        "post_runtime_name_set",
    ],
)
def test_executor_failure_matrix_rollback(tmp_path, monkeypatch, failure_stage):
    base = tmp_path / "base.yml"
    base.write_bytes(
        b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )
    override = tmp_path / "override.yml"
    override.write_bytes(
        b"services:\n  hermes-bot:\n    environment:\n      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n"
    )
    parent = tmp_path / "parent"
    parent.mkdir(parents=True, exist_ok=True)
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\n")
    runtime_env = parent / "runtime.env"
    secret_file = parent / "secret.env"

    import ops.secret_remediation_r1.executor as executor_module
    import ops.secret_remediation_r1.constants as constants

    monkeypatch.setattr(executor_module, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(constants, "PROD_RUNTIME_ENV_PATH", str(runtime_env))
    monkeypatch.setattr(executor_module, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(constants, "PROD_SECRET_FILE_PATH", str(secret_file))
    monkeypatch.setattr(executor_module, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(constants, "PROD_PARENT_DIR_PATH", str(parent))
    monkeypatch.setattr(executor_module, "PROD_LEGACY_ENV_PATH", str(legacy_env))
    monkeypatch.setattr(constants, "PROD_LEGACY_ENV_PATH", str(legacy_env))

    monkeypatch.setattr(
        "ops.secret_remediation_r1.preflight.run_compose_preflight", lambda: None
    )

    # default success mocks
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.read_poller_environ",
        lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00",
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.compose_command.run_recreate",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.run_recreate", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.source_invariant.verify_source_invariant",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.verify_source_invariant",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.runtime_invariant.verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.verify_runtime_invariants",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.poller_checker.check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.check_exactly_one_poller",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.health.check_health",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.executor.check_health",
        lambda expected=None, docker=None: None,
    )
    monkeypatch.setattr(executor_module, "ensure_parent_directory", lambda path: None)

    def mock_publish(
        dest, content, mode=None, uid=None, gid=None, override_mode=None, **kwargs
    ):
        import pathlib

        pathlib.Path(dest).write_bytes(content)
        return type(
            "PublishResult", (), {"path": dest, "uid": 0, "gid": 0, "mode": mode}
        )()

    monkeypatch.setattr("ops.secret_remediation_r1.safe_fs.publish_file", mock_publish)
    monkeypatch.setattr(
        "ops.secret_remediation_r1.safe_fs.replace_existing_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.override_transform.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.env_split.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.secret_transfer.publish_file", mock_publish
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.process_identity.resolve_poller_pid",
        lambda docker=None: (123, type("ID", (), {"container_id": "123"})),
    )
    monkeypatch.setattr(
        "ops.secret_remediation_r1.process_identity.read_poller_environ",
        lambda pid, identity, docker=None: b"TELEGRAM_BOT_TOKEN=secret\x00",
    )

    # Guard logic to simulate pre/post check properly
    guard_calls = []

    from ops.secret_remediation_r1.candidate_image_guard import (
        verify_legacy_image as real_verify_legacy_image,
    )

    def mock_verify_legacy(ref, backend=None):
        guard_calls.append(ref)
        real_verify_legacy_image(ref, backend=backend)

    monkeypatch.setattr(
        "ops.secret_remediation_r1.candidate_image_guard.verify_legacy_image",
        mock_verify_legacy,
    )

    class MatrixDockerBackend:
        def inspect(self, name):
            # return fake container data
            if failure_stage == "pre_image_guard" and len(guard_calls) == 0:
                return [
                    {
                        "Config": {
                            "Labels": {"com.docker.compose.image": "wrong_image_pre"}
                        }
                    }
                ]
            if failure_stage == "post_image_guard" and len(guard_calls) == 1:
                return [
                    {
                        "Config": {
                            "Labels": {"com.docker.compose.image": "wrong_image_post"}
                        }
                    }
                ]
            return [
                {
                    "Config": {
                        "Labels": {
                            "com.docker.compose.image": "healbite-hermes:pr99-main-273b0a6cccaf"
                        }
                    }
                }
            ]

    # Inject specific failures
    if failure_stage == "secret_transfer":

        def fail(*args, **kwargs):
            raise RuntimeError("secret_transfer failure")

        monkeypatch.setattr("ops.secret_remediation_r1.executor.transfer_secrets", fail)
    elif failure_stage == "base_transform":

        def fail(*args, **kwargs):
            raise RuntimeError("base_transform failure")

        monkeypatch.setattr(
            "ops.secret_remediation_r1.executor.transform_base_compose", fail
        )
    elif failure_stage == "override_transform":

        def fail(*args, **kwargs):
            raise RuntimeError("override_transform failure")

        monkeypatch.setattr(
            "ops.secret_remediation_r1.executor.transform_override", fail
        )
    elif failure_stage == "recreate":

        def fail(*args, **kwargs):
            raise RuntimeError("recreate failure")

        monkeypatch.setattr("ops.secret_remediation_r1.executor.run_recreate", fail)
    elif failure_stage == "effective_source_invariant":

        def fail(*args, **kwargs):
            raise RuntimeError("effective_source_invariant failure")

        monkeypatch.setattr(
            "ops.secret_remediation_r1.executor.verify_source_invariant", fail
        )
    elif failure_stage == "runtime_invariant":

        def fail(*args, **kwargs):
            raise RuntimeError("runtime_invariant failure")

        monkeypatch.setattr(
            "ops.secret_remediation_r1.executor.verify_runtime_invariants", fail
        )
    elif failure_stage == "poller":

        def fail(*args, **kwargs):
            raise RuntimeError("poller failure")

        monkeypatch.setattr(
            "ops.secret_remediation_r1.executor.check_exactly_one_poller", fail
        )
    elif failure_stage == "health":

        def fail(*args, **kwargs):
            raise RuntimeError("health failure")

        monkeypatch.setattr("ops.secret_remediation_r1.executor.check_health", fail)
    elif failure_stage == "post_runtime_name_set":
        # Simulating missing token in environment
        monkeypatch.setattr(
            "ops.secret_remediation_r1.process_identity.read_poller_environ",
            lambda pid, identity, docker=None: b"NORMAL=val\x00",
        )

    with pytest.raises(ExecutorError):
        run_remediation(
            str(base),
            str(override),
            docker=MatrixDockerBackend(),
            image_backend=MockImageBackend(),
        )

    # Assert rollback happened (base is restored)
    assert (
        base.read_bytes()
        == b"services:\n  hermes-bot:\n    env_file:\n      - /home/hermes/.hermes/.env\n"
    )

    # specific guard verifications
    if failure_stage == "pre_image_guard":
        assert len(guard_calls) == 1
    if failure_stage == "post_image_guard":
        assert len(guard_calls) == 2
