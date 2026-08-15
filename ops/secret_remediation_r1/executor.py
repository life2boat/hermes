"""End-to-end remediation executor."""
from __future__ import annotations
from ops.secret_remediation_r1.constants import (
    PROD_LEGACY_ENV_PATH, PROD_SECRET_FILE_PATH, PROD_RUNTIME_ENV_PATH,
    PROD_PARENT_DIR_PATH, COMPOSE_FILES, COMPOSE_WORKDIR,
)
from ops.secret_remediation_r1.rollback import capture_prestate, execute_rollback, RollbackError
from ops.secret_remediation_r1.parent_dir import ensure_parent_directory, ParentDirError
from ops.secret_remediation_r1.env_split import split_env, EnvSplitError
from ops.secret_remediation_r1.secret_transfer import transfer_secrets, SecretTransferError
from ops.secret_remediation_r1.compose_transform import transform_base_compose, ComposeTransformError
from ops.secret_remediation_r1.override_transform import transform_override, OverrideTransformError
from ops.secret_remediation_r1.compose_command import run_recreate, ComposeCommandError
from ops.secret_remediation_r1.source_invariant import verify_source_invariant, SourceState, SourceInvariantError
from ops.secret_remediation_r1.runtime_invariant import verify_runtime_invariants, RuntimeInvariantError
from ops.secret_remediation_r1.poller_checker import check_exactly_one_poller, PollerCheckerError
from ops.secret_remediation_r1.health import check_health, HealthCheckError


class ExecutorError(Exception):
    pass


def run_remediation(
    base_compose_path: str = COMPOSE_FILES[0],
    override_path: str = COMPOSE_FILES[3],
    docker=None,
) -> None:
    """
    Execute the full remediation sequence with rollback on any failure.
    """
    # Capture prestate
    try:
        prestate = capture_prestate(base_compose_path, override_path, PROD_PARENT_DIR_PATH)
    except Exception as exc:
        raise ExecutorError(f"Prestate capture failed: {exc}")

    try:
        ensure_parent_directory(PROD_PARENT_DIR_PATH)
        split_env(PROD_LEGACY_ENV_PATH, PROD_RUNTIME_ENV_PATH)
        transfer_secrets(PROD_SECRET_FILE_PATH, docker=docker)
        transform_base_compose(base_compose_path, base_compose_path)
        transform_override(override_path, override_path)
        run_recreate(COMPOSE_WORKDIR)

        # Read legacy bytes before verifying
        import os
        with open(PROD_LEGACY_ENV_PATH, "rb") as f:
            legacy_bytes = f.read()
        source_state = SourceState(
            legacy_env_bytes=legacy_bytes,
            dashscope_present_before="DASHSCOPE_API_KEY" in [l.split(b"=", 1)[0].decode() for l in legacy_bytes.splitlines() if b"=" in l]
        )
        verify_source_invariant(source_state, PROD_LEGACY_ENV_PATH, PROD_RUNTIME_ENV_PATH, PROD_SECRET_FILE_PATH)
        verify_runtime_invariants(docker=docker)
        check_exactly_one_poller(docker=docker)
        check_health(docker=docker)

    except Exception as exc:
        try:
            execute_rollback(prestate, docker=docker)
        except RollbackError as rb_exc:
            raise ExecutorError(
                f"Remediation failed: {exc} | Rollback: FAILED: {rb_exc}"
            ) from exc
        raise ExecutorError(f"Remediation failed (rolled back): {exc}") from exc
