"""Race-safe /etc/hermes creation via /etc dirfd."""
from __future__ import annotations
import errno
import os
import stat
from ops.secret_remediation_r1.safe_fs import _IS_LINUX, _O_DIRECTORY, _O_NOFOLLOW

PARENT_REQUIRED_UID = 0
PARENT_REQUIRED_GID = 0
PARENT_REQUIRED_MODE = 0o700


class ParentDirError(Exception):
    pass


def _validate_child_name(child_name: str) -> None:
    """Reject names which cannot safely be addressed relative to a dirfd."""
    separators = {"/", os.sep}
    if os.altsep is not None:
        separators.add(os.altsep)
    if (
        not child_name
        or child_name in {".", ".."}
        or any(separator in child_name for separator in separators)
    ):
        raise ParentDirError("Invalid child directory name")


def _metadata_error(child_st: os.stat_result) -> str | None:
    if not stat.S_ISDIR(child_st.st_mode):
        return "Target is not a directory"
    if child_st.st_uid != PARENT_REQUIRED_UID:
        return f"Wrong uid: {child_st.st_uid}"
    if child_st.st_gid != PARENT_REQUIRED_GID:
        return f"Wrong gid: {child_st.st_gid}"
    if (child_st.st_mode & 0o777) != PARENT_REQUIRED_MODE:
        return f"Existing dir has wrong mode: {oct(child_st.st_mode & 0o777)}"
    return None


def ensure_parent_directory(parent_path: str = "/etc/hermes") -> None:
    """
    Race-safe creation of parent_path using dirfd operations on the grand-parent.
    Raises ParentDirError on any failure.
    """
    grand_parent = os.path.dirname(parent_path) or "/"
    child_name = os.path.basename(parent_path)

    _validate_child_name(child_name)
    # Open grand-parent safely
    gp_flags = os.O_RDONLY
    if _IS_LINUX:
        gp_flags |= _O_DIRECTORY | _O_NOFOLLOW

    try:
        gp_fd = os.open(grand_parent, gp_flags)
    except OSError as exc:
        raise ParentDirError(f"Failed to open grand-parent {grand_parent}: {exc}")

    try:
        created = False
        try:
            os.mkdir(child_name, PARENT_REQUIRED_MODE, dir_fd=gp_fd)
            created = True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise ParentDirError(f"mkdir failed: {exc}")

        try:
            child_lstat = os.stat(
                child_name,
                dir_fd=gp_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ParentDirError(f"Failed to inspect child dir: {exc}")

        if stat.S_ISLNK(child_lstat.st_mode):
            raise ParentDirError("Target is a symlink")
        if not stat.S_ISDIR(child_lstat.st_mode):
            raise ParentDirError("Target is not a directory")

        # Open child to verify identity
        child_flags = os.O_RDONLY
        if _IS_LINUX:
            child_flags |= _O_DIRECTORY | _O_NOFOLLOW

        try:
            child_fd = os.open(child_name, child_flags, dir_fd=gp_fd)
        except OSError as exc:
            raise ParentDirError(f"Failed to open child dir: {exc}")

        try:
            child_st = os.fstat(child_fd)
            if (
                child_lstat.st_dev != child_st.st_dev
                or child_lstat.st_ino != child_st.st_ino
            ):
                raise ParentDirError("Child identity changed between stat and open")

            metadata_error = _metadata_error(child_st)
            if metadata_error is not None:
                if not created:
                    raise ParentDirError(metadata_error)

                try:
                    if (child_st.st_mode & 0o777) != PARENT_REQUIRED_MODE:
                        os.fchmod(child_fd, PARENT_REQUIRED_MODE)
                    if (
                        child_st.st_uid != PARENT_REQUIRED_UID
                        or child_st.st_gid != PARENT_REQUIRED_GID
                    ):
                        if not hasattr(os, "fchown"):
                            raise ParentDirError("fchown is unavailable")
                        os.fchown(
                            child_fd,
                            PARENT_REQUIRED_UID,
                            PARENT_REQUIRED_GID,
                        )
                except OSError as exc:
                    raise ParentDirError(f"Failed to set created dir metadata: {exc}")

                metadata_error = _metadata_error(os.fstat(child_fd))
                if metadata_error is not None:
                    raise ParentDirError(
                        "Created directory metadata did not match required values: "
                        f"{metadata_error}"
                    )
        finally:
            os.close(child_fd)

        if _IS_LINUX and created:
            os.fsync(gp_fd)

    finally:
        os.close(gp_fd)
