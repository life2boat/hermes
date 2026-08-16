"""End-to-end remediation executor."""

from __future__ import annotations

from ops.secret_remediation_r1.compose_command import run_recreate
from ops.secret_remediation_r1.compose_transform import (
    transform_base_compose,
)
from ops.secret_remediation_r1.constants import (
    COMPOSE_FILES,
    COMPOSE_WORKDIR,
    PROD_LEGACY_ENV_PATH,
    PROD_PARENT_DIR_PATH,
    PROD_RUNTIME_ENV_PATH,
    PROD_SECRET_FILE_PATH,
)
from ops.secret_remediation_r1.env_split import split_env
from ops.secret_remediation_r1.health import check_health
from ops.secret_remediation_r1.override_transform import (
    transform_override,
)
from ops.secret_remediation_r1.parent_dir import ensure_parent_directory
from ops.secret_remediation_r1.poller_checker import (
    check_exactly_one_poller,
)
from ops.secret_remediation_r1.rollback import (
    RollbackError,
    capture_prestate,
    execute_rollback,
)
from ops.secret_remediation_r1.runtime_invariant import (
    verify_runtime_invariants,
    capture_runtime_prestate,
)
from ops.secret_remediation_r1.secret_transfer import (
    transfer_secrets,
)
from ops.secret_remediation_r1.source_invariant import (
    SourceState,
    verify_source_invariant,
)


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
        runtime_prestate = capture_runtime_prestate(docker=docker)
        prestate = capture_prestate(
            base_compose_path, override_path, PROD_PARENT_DIR_PATH
        )
    except Exception as exc:
        raise ExecutorError(f"Prestate capture failed: {exc}")

    try:
        ensure_parent_directory(PROD_PARENT_DIR_PATH)
        split_env(PROD_LEGACY_ENV_PATH, PROD_RUNTIME_ENV_PATH)
        transfer_secrets(PROD_SECRET_FILE_PATH, docker=docker)
        from ops.secret_remediation_r1.candidate_image_guard import verify_legacy_image
        from ops.secret_remediation_r1.constants import CONTAINER_NAME
        from ops.secret_remediation_r1.process_identity import RealDockerBackend

        backend = docker or RealDockerBackend()
        cdata = backend.inspect(CONTAINER_NAME)
        if not cdata:
            raise ExecutorError(f"Pre-recreate: Container {CONTAINER_NAME} not found")
        labels = cdata[0].get("Config", {}).get("Labels", {})
        eff_image = labels.get("com.docker.compose.image", "") or cdata[0].get(
            "Config", {}
        ).get("Image", "")
        verify_legacy_image(eff_image)

        transform_base_compose(base_compose_path, base_compose_path)
        transform_override(override_path, override_path)
        run_recreate(COMPOSE_WORKDIR)

        cdata_post = backend.inspect(CONTAINER_NAME)
        if not cdata_post:
            raise ExecutorError(f"Post-recreate: Container {CONTAINER_NAME} not found")
        labels_post = cdata_post[0].get("Config", {}).get("Labels", {})
        eff_image_post = labels_post.get("com.docker.compose.image", "") or cdata_post[
            0
        ].get("Config", {}).get("Image", "")
        verify_legacy_image(eff_image_post)

        # Build SourceState from captured prestate bytes.
        # SourceState.__post_init__ auto-derives legacy_env_name_set and
        # pre_remediation_effective_protected_name_set from legacy_env_bytes.
        from ops.secret_remediation_r1.source_invariant import _parse_env_keys

        _legacy_keys = _parse_env_keys(prestate.legacy_env_bytes)
        source_state = SourceState(
            legacy_env_bytes=prestate.legacy_env_bytes,
            dashscope_present_before="DASHSCOPE_API_KEY" in _legacy_keys,
        )
        verify_source_invariant(
            source_state,
            PROD_LEGACY_ENV_PATH,
            PROD_RUNTIME_ENV_PATH,
            PROD_SECRET_FILE_PATH,
            COMPOSE_FILES,
            COMPOSE_WORKDIR,
        )
        verify_runtime_invariants(expected=runtime_prestate, docker=docker)
        check_exactly_one_poller(docker=docker)
        check_health(docker=docker)

        # H. Post-env process revalidation: verify the effective protected NAME SET.
        # Protected values are expected in process env (from /etc/hermes/hermes-production.env).
        # The invariant is: the exact expected protected NAME set is present.
        # Values are NEVER read, logged, hashed, or compared.
        from ops.secret_remediation_r1.constants import PROTECTED_NAMES
        from ops.secret_remediation_r1.process_identity import (
            read_poller_environ,
            resolve_poller_pid,
        )

        post_pid, post_identity = resolve_poller_pid(docker=docker)
        post_env_bytes = read_poller_environ(post_pid, post_identity, docker=docker)

        # Extract names only — never extract or compare values.
        post_env_names: set[str] = set()
        for line in post_env_bytes.split(b"\x00"):
            if b"=" in line:
                k = line.split(b"=", 1)[0].decode("utf-8", errors="ignore")
                if k in PROTECTED_NAMES:
                    post_env_names.add(k)

        # Build the expected protected name set from prestate.
        pre_protected_names = source_state.pre_remediation_effective_protected_name_set
        if post_env_names != pre_protected_names:
            raise ExecutorError(
                f"Post-recreate protected NAME set mismatch: "
                f"expected={pre_protected_names!r}, found={post_env_names!r}"
            )

    except Exception as exc:
        try:
            execute_rollback(prestate, runtime_prestate=runtime_prestate, docker=docker)
        except RollbackError as rb_exc:
            raise ExecutorError(
                f"Remediation failed: {exc} | Rollback: FAILED: {rb_exc}"
            ) from exc
        raise ExecutorError(f"Remediation failed (rolled back): {exc}") from exc
