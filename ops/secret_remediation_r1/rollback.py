"""Fail-closed rollback state machine for HealBite secret remediation R1.

All security-relevant file mutations use dirfd-relative operations to prevent
TOCTOU races and pathname-based attacks.  No ``os.path.exists``, ``os.unlink``,
``os.listdir``, or ``os.rmdir`` on security-sensitive paths; all such operations
are performed relative to a bound parent ``dirfd``.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass

from ops.secret_remediation_r1.process_identity import DockerBackend
from ops.secret_remediation_r1.safe_fs import (
    _IS_LINUX,
    replace_existing_file,
)


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
    """Capture exact bytes and metadata of mutable artifacts before any mutation.

    Fails if any expected artifact is missing or if new artifacts already exist.
    """
    from ops.secret_remediation_r1.constants import (
        PROD_LEGACY_ENV_PATH,
        PROD_RUNTIME_ENV_PATH,
        PROD_SECRET_FILE_PATH,
    )
    from ops.secret_remediation_r1.safe_fs import safe_open_source

    # Verify expected-absent files are indeed absent (path-only check is safe here;
    # we are not doing a security-sensitive mutation, just a pre-condition check).
    for path in [PROD_SECRET_FILE_PATH, PROD_RUNTIME_ENV_PATH]:
        try:
            os.lstat(path)
            raise RollbackError(f"Expected-absent file already exists: {path}")
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise RollbackError(f"lstat pre-check failed for {path}: {exc}")

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

    # Determine whether /etc/hermes already exists.
    try:
        os.lstat(parent_dir_path)
        parent_created = False
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            parent_created = True
        else:
            raise RollbackError(f"lstat parent_dir failed: {exc}")

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


def _dirfd_unlink(parent_dirfd: int, basename: str, parent_path: str) -> None:
    """Unlink *basename* relative to *parent_dirfd*, then fsync the parent.

    On non-Linux platforms (where dirfd-relative operations are unavailable)
    falls back to a pathname-based unlink within the already-verified parent.
    Raises RollbackError on failure; never swallows errors silently.
    """
    if _IS_LINUX:
        # Verify the child is a regular file before removing it.
        try:
            child_st = os.stat(basename, dir_fd=parent_dirfd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return  # already absent; nothing to do
            raise RollbackError(
                f"dirfd stat of {basename!r} before unlink failed: {exc}"
            )
        if not stat.S_ISREG(child_st.st_mode):
            raise RollbackError(f"Refusing to unlink non-regular file: {basename!r}")
        try:
            os.unlink(basename, dir_fd=parent_dirfd)
        except OSError as exc:
            raise RollbackError(f"dirfd unlink of {basename!r} failed: {exc}")
        try:
            os.fsync(parent_dirfd)
        except OSError as exc:
            raise RollbackError(
                f"fsync parent after unlink of {basename!r} failed: {exc}"
            )
    else:
        # Non-Linux: plain unlink within the verified parent path.
        full_path = os.path.join(parent_path, basename)
        try:
            os.unlink(full_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RollbackError(f"unlink of {full_path!r} failed: {exc}")


def _dirfd_rmdir_if_empty(
    parent_dirfd: int,
    parent_path: str,
    dir_basename: str,
    dir_full_path: str,
) -> None:
    """Remove the directory ``dir_basename`` relative to ``parent_dirfd`` if empty.

    Requires the directory to be confirmed empty before removal; raises
    RollbackError if it is non-empty.  Never silently swallows errors.
    """
    if _IS_LINUX:
        try:
            st = os.stat(dir_basename, dir_fd=parent_dirfd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise RollbackError(
                    f"Failed to open {dir_basename!r} for empty-check: not a directory or is a symlink"
                )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            dir_fd = os.open(
                dir_basename,
                flags,
                dir_fd=parent_dirfd,
            )
            fst = os.fstat(dir_fd)
            if st.st_ino != fst.st_ino or st.st_dev != fst.st_dev:
                os.close(dir_fd)
                raise RollbackError(
                    f"Failed to open {dir_basename!r} for empty-check: identity mismatch"
                )
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return
            raise RollbackError(
                f"Failed to open {dir_basename!r} for empty-check: {exc}"
            )
        try:
            # Read the directory to confirm it has no children.
            try:
                children = os.listdir(dir_fd)
            except OSError as exc:
                raise RollbackError(
                    f"Failed to list {dir_basename!r} for empty-check: {exc}"
                )
            if children:
                raise RollbackError(f"rollback_parent_not_empty: {children!r}")
        finally:
            os.close(dir_fd)

        try:
            os.rmdir(dir_basename, dir_fd=parent_dirfd)
        except OSError as exc:
            raise RollbackError(f"dirfd rmdir of {dir_basename!r} failed: {exc}")
        try:
            os.fsync(parent_dirfd)
        except OSError as exc:
            raise RollbackError(
                f"fsync parent after rmdir of {dir_basename!r} failed: {exc}"
            )
    else:
        # Non-Linux: pathname-based rmdir.
        try:
            children = os.listdir(dir_full_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RollbackError(f"listdir {dir_full_path!r} failed: {exc}")
        if children:
            raise RollbackError(f"rollback_parent_not_empty: {children!r}")
        try:
            os.rmdir(dir_full_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RollbackError(f"rmdir {dir_full_path!r} failed: {exc}")


def execute_rollback(
    prestate: RemediationPrestate,
    runtime_prestate: "RuntimePrestate",
    docker: DockerBackend | None = None,
) -> None:
    """Restore all mutated artifacts to prestate and recreate the container.

    Claims ROLLED_BACK only after ALL post-rollback invariants pass.
    Raises RollbackError with complete=False if any step fails.
    """
    from ops.secret_remediation_r1.compose_command import run_recreate
    from ops.secret_remediation_r1.constants import (
        PROD_PARENT_DIR_PATH,
        PROD_RUNTIME_ENV_PATH,
        PROD_SECRET_FILE_PATH,
    )
    from ops.secret_remediation_r1.health import check_health
    from ops.secret_remediation_r1.poller_checker import check_exactly_one_poller
    from ops.secret_remediation_r1.runtime_invariant import (
        verify_runtime_invariants,
        RuntimePrestate,
        RuntimePrestate,
    )

    errors: list[str] = []

    def _try(desc: str, fn):
        try:
            fn()
        except Exception as exc:
            errors.append(f"{desc}: {exc}")

    # 1. Restore base compose (atomic replacement via replace_existing_file).
    _try(
        "restore_base_compose",
        lambda: replace_existing_file(
            prestate.base_compose_path,
            prestate.base_compose_bytes,
            override_mode=prestate.base_compose_mode,
        ),
    )

    # 2. Restore override.
    _try(
        "restore_override",
        lambda: replace_existing_file(
            prestate.override_path,
            prestate.override_bytes,
            override_mode=prestate.override_mode,
        ),
    )

    # 3. Remove newly created env files using dirfd-relative operations.
    parent_path = PROD_PARENT_DIR_PATH

    # Open the parent dirfd once for both unlink operations.
    parent_dirfd: int = -1
    if _IS_LINUX:
        try:
            st = os.lstat(parent_path)
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                errors.append(
                    "open_parent_dirfd: parent path is not a directory or is a symlink"
                )
            else:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                parent_dirfd = os.open(parent_path, flags)
                fst = os.fstat(parent_dirfd)
                if st.st_ino != fst.st_ino or st.st_dev != fst.st_dev:
                    errors.append("open_parent_dirfd: parent identity mismatch")
                    os.close(parent_dirfd)
                    parent_dirfd = -1
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                errors.append(f"open_parent_dirfd: {exc}")

    try:
        for full_path in [PROD_RUNTIME_ENV_PATH, PROD_SECRET_FILE_PATH]:
            basename = os.path.basename(full_path)
            _try(
                f"remove_{basename}",
                lambda b=basename: _dirfd_unlink(parent_dirfd, b, parent_path),
            )
    finally:
        if parent_dirfd != -1:
            try:
                os.close(parent_dirfd)
            except OSError as exc:
                errors.append(f"close_parent_dirfd: {exc}")

    # 4. Remove /etc/hermes if we created it and it is now empty.
    if prestate.created_parent_dir:
        etc_path = os.path.dirname(parent_path)  # /etc
        etc_dirfd: int = -1
        parent_basename = os.path.basename(parent_path)

        if _IS_LINUX:
            try:
                st = os.lstat(etc_path)
                if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    errors.append(
                        "open_etc_dirfd: etc path is not a directory or is a symlink"
                    )
                else:
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    etc_dirfd = os.open(etc_path, flags)
                    fst = os.fstat(etc_dirfd)
                    if st.st_ino != fst.st_ino or st.st_dev != fst.st_dev:
                        errors.append("open_etc_dirfd: etc identity mismatch")
                        os.close(etc_dirfd)
                        etc_dirfd = -1
            except OSError as exc:
                errors.append(f"open_etc_dirfd: {exc}")

        try:
            _try(
                "remove_parent_dir",
                lambda: _dirfd_rmdir_if_empty(
                    etc_dirfd, etc_path, parent_basename, parent_path
                ),
            )
        finally:
            if etc_dirfd != -1:
                try:
                    os.fsync(etc_dirfd)
                    os.close(etc_dirfd)
                except OSError as exc:
                    errors.append(f"fsync_or_close_etc_dirfd: {exc}")

    if errors:
        raise RollbackError(
            "Config restore failed: " + "; ".join(errors), complete=False
        )

    # 5. Compose recreate.
    _try("compose_recreate", lambda: run_recreate())

    # 6. Post-rollback invariants.
    _try(
        "runtime_invariant",
        lambda: verify_runtime_invariants(expected=runtime_prestate, docker=docker),
    )
    _try("poller_checker", lambda: check_exactly_one_poller(docker=docker))
    _try("health", lambda: check_health(docker=docker))

    if errors:
        raise RollbackError(
            "Post-rollback invariants failed: " + "; ".join(errors),
            complete=False,
        )
