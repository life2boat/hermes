"""Race-safe /etc/hermes creation via /etc dirfd."""
from __future__ import annotations
import errno
import os
import stat
from ops.secret_remediation_r1.safe_fs import SafeFsError, _IS_LINUX, _O_DIRECTORY, _O_NOFOLLOW

PARENT_REQUIRED_UID = 0
PARENT_REQUIRED_GID = 0
PARENT_REQUIRED_MODE = 0o700


class ParentDirError(Exception):
    pass


def ensure_parent_directory(parent_path: str = "/etc/hermes") -> None:
    """
    Race-safe creation of parent_path using dirfd operations on the grand-parent.
    Raises ParentDirError on any failure.
    """
    grand_parent = os.path.dirname(parent_path) or "/"
    child_name = os.path.basename(parent_path)

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
            os.mkdir(parent_path, PARENT_REQUIRED_MODE)
            created = True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise ParentDirError(f"mkdir failed: {exc}")

        if stat.S_ISLNK(os.lstat(parent_path).st_mode):
            raise ParentDirError("Target is a symlink")

        # Open child to verify identity
        child_flags = os.O_RDONLY
        if _IS_LINUX:
            child_flags |= _O_DIRECTORY | _O_NOFOLLOW

        try:
            child_fd = os.open(parent_path, child_flags)
        except OSError as exc:
            raise ParentDirError(f"Failed to open child dir: {exc}")

        try:
            child_st = os.fstat(child_fd)
        finally:
            os.close(child_fd)

        if not stat.S_ISDIR(child_st.st_mode):
            raise ParentDirError("Target is not a directory")
        if child_st.st_uid != PARENT_REQUIRED_UID:
            raise ParentDirError(f"Wrong uid: {child_st.st_uid}")
        if child_st.st_gid != PARENT_REQUIRED_GID:
            raise ParentDirError(f"Wrong gid: {child_st.st_gid}")
        if (child_st.st_mode & 0o777) != PARENT_REQUIRED_MODE:
            if not created:
                raise ParentDirError(
                    f"Existing dir has wrong mode: {oct(child_st.st_mode & 0o777)}"
                )
            # If we just created it, set mode and chown
            os.chmod(parent_path, PARENT_REQUIRED_MODE)
            if hasattr(os, "chown"):
                os.chown(parent_path, PARENT_REQUIRED_UID, PARENT_REQUIRED_GID)

        if _IS_LINUX and created:
            os.fsync(gp_fd)

    finally:
        os.close(gp_fd)
