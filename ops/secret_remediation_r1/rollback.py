"""Fail-closed rollback state machine for HealBite secret remediation R1."""
from __future__ import annotations
import os
import stat
from dataclasses import dataclass
from typing import Optional
from ops.secret_remediation_r1.safe_fs import SafeFsError
from ops.secret_remediation_r1.process_identity import DockerBackend


class RollbackError(Exception):
    complete: bool = False

    def __init__(self, message: str, *, complete: bool = False) -> None:
        super().__init__(message)
        self.complete = complete


@dataclass
class RemediationPrestate:
    base_compose_bytes: bytes
    base_compose_path: str
    override_bytes: bytes
    override_path: str
    legacy_env_bytes: bytes
    base_compose_mode: int
    override_mode: int
    created_parent_dir: bool  # True if /etc/hermes was newly created


def capture_prestate(
    base_compose_path: str,
    override_path: str,
    parent_dir_path: str,
) -> RemediationPrestate:
    """
    Capture exact bytes and metadata of mutable artifacts before any mutation.
    Fails if any expected artifact is missing or if new artifacts already exist.
    """
    from ops.secret_remediation_r1.safe_fs import safe_open_source
    from ops.secret_remediation_r1.constants import PROD_SECRET_FILE_PATH, PROD_RUNTIME_ENV_PATH, PROD_LEGACY_ENV_PATH

    # Verify expected-absent files are indeed absent
    for path in [PROD_SECRET_FILE_PATH, PROD_RUNTIME_ENV_PATH]:
        if os.path.exists(path):
            raise RollbackError(f"Expected-absent file already exists: {path}")

    # Capture legacy env
    try:
        with open(PROD_LEGACY_ENV_PATH, "rb") as f:
            legacy_env_bytes = f.read()
    except Exception as exc:
        raise RollbackError(f"Failed to capture legacy env: {exc}")

    # Capture base compose
    try:
        fd, fst = safe_open_source(base_compose_path)
        with os.fdopen(fd, "rb") as f:
            base_bytes = f.read()
        base_mode = fst.st_mode & 0o777
    except Exception as exc:
        raise RollbackError(f"Failed to capture base compose: {exc}")

    # Capture override
    try:
        fd2, fst2 = safe_open_source(override_path)
        with os.fdopen(fd2, "rb") as f2:
            override_bytes = f2.read()
        override_mode = fst2.st_mode & 0o777
    except Exception as exc:
        raise RollbackError(f"Failed to capture override: {exc}")

    parent_created = not os.path.exists(parent_dir_path)

    return RemediationPrestate(
        base_compose_bytes=base_bytes,
        base_compose_path=base_compose_path,
        override_bytes=override_bytes,
        override_path=override_path,
        legacy_env_bytes=legacy_env_bytes,
        base_compose_mode=base_mode,
        override_mode=override_mode,
        created_parent_dir=parent_created,
    )


def execute_rollback(
    prestate: RemediationPrestate,
    docker: DockerBackend | None = None,
) -> None:
    """
    Restore all mutated artifacts to prestate and recreate the container.
    Claims ROLLED_BACK only after ALL post-rollback invariants pass.
    Raises RollbackError with complete=False if any step fails.
    """
    from ops.secret_remediation_r1.constants import (
        PROD_SECRET_FILE_PATH, PROD_RUNTIME_ENV_PATH, PROD_PARENT_DIR_PATH,
        COMPOSE_WORKDIR
    )
    from ops.secret_remediation_r1.compose_command import run_recreate
    from ops.secret_remediation_r1.runtime_invariant import verify_runtime_invariants
    from ops.secret_remediation_r1.poller_checker import check_exactly_one_poller
    from ops.secret_remediation_r1.health import check_health

    errors: list[str] = []

    def _try(desc: str, fn):
        try:
            fn()
        except Exception as exc:
            errors.append(f"{desc}: {exc}")

    # 1. Restore base compose
    _try("restore_base_compose", lambda: _restore_file(
        prestate.base_compose_path,
        prestate.base_compose_bytes,
        prestate.base_compose_mode,
    ))

    # 2. Restore override
    _try("restore_override", lambda: _restore_file(
        prestate.override_path,
        prestate.override_bytes,
        prestate.override_mode,
    ))

    # 3. Remove newly created env files
    for path in [PROD_RUNTIME_ENV_PATH, PROD_SECRET_FILE_PATH]:
        if os.path.exists(path):
            _try(f"remove_{path}", lambda p=path: os.unlink(p))

    # 4. Remove /etc/hermes if we created it and it's empty
    if prestate.created_parent_dir and os.path.exists(PROD_PARENT_DIR_PATH):
        try:
            children = os.listdir(PROD_PARENT_DIR_PATH)
            if children:
                errors.append(f"rollback_parent_not_empty: {children}")
            else:
                os.rmdir(PROD_PARENT_DIR_PATH)
                if os.name == "posix":
                    etc_fd = os.open("/etc", os.O_RDONLY)
                    try:
                        os.fsync(etc_fd)
                    finally:
                        os.close(etc_fd)
        except Exception as exc:
            errors.append(f"remove_parent_dir: {exc}")

    if errors:
        raise RollbackError("Config restore failed: " + "; ".join(errors), complete=False)

    # 5. Compose recreate
    _try("compose_recreate", lambda: run_recreate())

    # 6. Post-rollback invariants
    _try("runtime_invariant", lambda: verify_runtime_invariants(docker=docker))
    _try("poller_checker", lambda: check_exactly_one_poller(docker=docker))
    _try("health", lambda: check_health(docker=docker))

    if errors:
        raise RollbackError(
            "Post-rollback invariants failed: " + "; ".join(errors),
            complete=False
        )


from ops.secret_remediation_r1.safe_fs import replace_existing_file

def _restore_file(path: str, content: bytes, mode: int) -> None:
    replace_existing_file(path, content, override_mode=mode)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
