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
    from ops.secret_remediation_r1.preflight import run_compose_preflight
    
    # Run preflight before any mutation
    try:
        run_compose_preflight()
    except Exception as exc:
        raise ExecutorError(f"Preflight failed: {exc}")

    # Capture prestate
    try:
        prestate = capture_prestate(base_compose_path, override_path, PROD_PARENT_DIR_PATH)
    except Exception as exc:
        raise ExecutorError(f"Prestate capture failed: {exc}")

    try:
        ensure_parent_directory(PROD_PARENT_DIR_PATH)
        split_env(PROD_LEGACY_ENV_PATH, PROD_RUNTIME_ENV_PATH)
        transfer_secrets(PROD_SECRET_FILE_PATH, docker=docker)
        from ops.secret_remediation_r1.process_identity import RealDockerBackend
        from ops.secret_remediation_r1.constants import CONTAINER_NAME
        from ops.secret_remediation_r1.candidate_image_guard import verify_legacy_image

        backend = docker or RealDockerBackend()
        cdata = backend.inspect(CONTAINER_NAME)
        if not cdata:
            raise ExecutorError(f"Pre-recreate: Container {CONTAINER_NAME} not found")
        labels = cdata[0].get("Config", {}).get("Labels", {})
        eff_image = labels.get("com.docker.compose.image", "") or cdata[0].get("Config", {}).get("Image", "")
        verify_legacy_image(eff_image)

        transform_base_compose(base_compose_path, base_compose_path)
        transform_override(override_path, override_path)
        run_recreate(COMPOSE_WORKDIR)

        cdata_post = backend.inspect(CONTAINER_NAME)
        if not cdata_post:
            raise ExecutorError(f"Post-recreate: Container {CONTAINER_NAME} not found")
        labels_post = cdata_post[0].get("Config", {}).get("Labels", {})
        eff_image_post = labels_post.get("com.docker.compose.image", "") or cdata_post[0].get("Config", {}).get("Image", "")
        verify_legacy_image(eff_image_post)

        # Use captured legacy bytes
        source_state = SourceState(
            legacy_env_bytes=prestate.legacy_env_bytes,
            dashscope_present_before="DASHSCOPE_API_KEY" in [l.split(b"=", 1)[0].decode() for l in prestate.legacy_env_bytes.splitlines() if b"=" in l]
        )
        verify_source_invariant(source_state, PROD_LEGACY_ENV_PATH, PROD_RUNTIME_ENV_PATH, PROD_SECRET_FILE_PATH)
        verify_runtime_invariants(docker=docker)
        check_exactly_one_poller(docker=docker)
        check_health(docker=docker)

        # H. Post-env process revalidation
        from ops.secret_remediation_r1.process_identity import resolve_poller_pid, read_poller_environ
        from ops.secret_remediation_r1.constants import PROTECTED_NAMES
        post_pid, post_identity = resolve_poller_pid(docker=docker)
        post_env_bytes = read_poller_environ(post_pid, post_identity, docker=docker)
        
        # Ensure NO raw secret strings in environment
        for line in post_env_bytes.split(b"\x00"):
            if b"=" in line:
                k = line.split(b"=", 1)[0].decode("utf-8", errors="ignore")
                if k in PROTECTED_NAMES:
                    raise ExecutorError(f"Post-recreate environment still contains secret string: {k}")

    except Exception as exc:
        try:
            execute_rollback(prestate, docker=docker)
        except RollbackError as rb_exc:
            raise ExecutorError(
                f"Remediation failed: {exc} | Rollback: FAILED: {rb_exc}"
            ) from exc
        raise ExecutorError(f"Remediation failed (rolled back): {exc}") from exc
