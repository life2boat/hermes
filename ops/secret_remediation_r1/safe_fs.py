"""Fail-closed Linux-safe filesystem publication primitive."""
from __future__ import annotations
import errno
import os
import secrets
import stat
from dataclasses import dataclass
from typing import Optional

_IS_LINUX = os.name == "posix" and hasattr(os, "O_NOFOLLOW")

# Portable flag fallbacks for cross-platform unit-test running
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_PATH = getattr(os, "O_PATH", 0)  # Linux-only


class SafeFsError(Exception):
    """Raised when a safe-fs operation fails."""
    cleanup_incomplete: bool = False

    def __init__(self, message: str, *, cleanup_incomplete: bool = False) -> None:
        super().__init__(message)
        self.cleanup_incomplete = cleanup_incomplete


@dataclass
class PublishResult:
    path: str
    uid: int
    gid: int
    mode: int  # raw st_mode


def safe_open_source(path: str) -> tuple[int, os.stat_result]:
    """
    Open *path* for reading with safety guarantees.
    Returns (fd, fstat_result).
    Raises SafeFsError on symlink, non-regular, or open failure.
    """
    try:
        st_lstat = os.lstat(path)
    except OSError as exc:
        raise SafeFsError(f"lstat failed: {exc}")

    if stat.S_ISLNK(st_lstat.st_mode):
        raise SafeFsError(f"Source is a symlink: {path}")
    if not stat.S_ISREG(st_lstat.st_mode):
        raise SafeFsError(f"Source is not a regular file: {path}")

    flags = os.O_RDONLY
    if _IS_LINUX:
        flags |= _O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SafeFsError(f"open failed: {exc}")

    try:
        st_fstat = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise SafeFsError(f"fstat after open failed: {exc}")

    if not stat.S_ISREG(st_fstat.st_mode):
        os.close(fd)
        raise SafeFsError("fstat shows non-regular file after open")

    if _IS_LINUX:
        if st_lstat.st_ino != st_fstat.st_ino or st_lstat.st_dev != st_fstat.st_dev:
            os.close(fd)
            raise SafeFsError("lstat/fstat identity mismatch (TOCTOU)")

    return fd, st_fstat


def _open_parent_dirfd(parent: str) -> tuple[int, os.stat_result]:
    """Open parent directory via O_DIRECTORY|O_NOFOLLOW, returning (dirfd, fstat)."""
    try:
        parent_lstat = os.lstat(parent)
    except OSError as exc:
        raise SafeFsError(f"parent lstat failed: {exc}")

    if stat.S_ISLNK(parent_lstat.st_mode):
        raise SafeFsError(f"Parent directory is a symlink: {parent}")
    if not stat.S_ISDIR(parent_lstat.st_mode):
        raise SafeFsError(f"Parent is not a directory: {parent}")

    if _IS_LINUX:
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    else:
        flags = os.O_RDONLY

    try:
        dirfd = os.open(parent, flags)
    except OSError as exc:
        raise SafeFsError(f"open parent failed: {exc}")

    try:
        dirfd_fstat = os.fstat(dirfd)
    except OSError as exc:
        os.close(dirfd)
        raise SafeFsError(f"fstat parent failed: {exc}")

    if _IS_LINUX:
        if parent_lstat.st_ino != dirfd_fstat.st_ino or parent_lstat.st_dev != dirfd_fstat.st_dev:
            os.close(dirfd)
            raise SafeFsError("Parent lstat/fstat identity mismatch")
        if not stat.S_ISDIR(dirfd_fstat.st_mode):
            os.close(dirfd)
            raise SafeFsError("Parent fstat shows non-directory")

    return dirfd, dirfd_fstat


def publish_file(
    destination: str,
    content: bytes,
    *,
    mode: int = 0o600,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    require_uid: Optional[int] = None,
    require_gid: Optional[int] = None,
    require_mode: Optional[int] = None,
) -> PublishResult:
    """
    Publish *content* to *destination* using fail-closed Linux primitives.

    Guarantees:
    - Destination must not pre-exist; fails if it does.
    - Temp file uses cryptographically random basename in same directory.
    - Exclusive publication; concurrent creation causes FAIL.
    - Failure after publication unlinks the published inode.
    - Cleanup failure propagates CLEANUP_INCOMPLETE.
    """
    basename = os.path.basename(destination)
    if not basename or os.sep in basename or (os.altsep and os.altsep in basename):
        raise SafeFsError(f"Invalid destination basename: {destination!r}")

    parent = os.path.dirname(destination)
    if not parent:
        parent = "."

    dirfd, _dir_fstat = _open_parent_dirfd(parent)

    tmp_path: Optional[str] = None
    fd: int = -1
    dest_published: bool = False

    try:
        # Verify destination does not already exist
        try:
            os.lstat(destination)
            raise SafeFsError(f"Destination already exists: {destination}")
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise SafeFsError(f"Unexpected error checking destination: {exc}")

        # Generate random temp name
        rand_suffix = secrets.token_hex(16)
        tmp_path = os.path.join(parent, f".tmp_{rand_suffix}")

        # Create temp with O_CREAT|O_EXCL
        creat_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if _IS_LINUX:
            creat_flags |= _O_NOFOLLOW

        try:
            fd = os.open(tmp_path, creat_flags, mode)
        except FileExistsError:
            raise SafeFsError("Temp file collision (concurrent creation)")
        except OSError as exc:
            raise SafeFsError(f"Failed to create temp file: {exc}")

        try:
            # Write content
            os.write(fd, content)

            # fchmod
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)

            # fchown
            if uid is not None and gid is not None and hasattr(os, "fchown"):
                os.fchown(fd, uid, gid)

            # fsync
            os.fsync(fd)

            # fstat temp
            tmp_fstat = os.fstat(fd)
            if not stat.S_ISREG(tmp_fstat.st_mode):
                raise SafeFsError("Temp file is not regular after write")

            os.close(fd)
            fd = -1

            # Exclusive publication via hard link
            try:
                if _IS_LINUX and hasattr(os, "link"):
                    os.link(
                        tmp_path,
                        destination,
                        src_dir_fd=dirfd,
                        dst_dir_fd=dirfd,
                        follow_symlinks=False,
                    )
                else:
                    # Windows fallback: non-atomic but best-effort
                    if os.path.exists(destination):
                        raise SafeFsError(f"Destination appeared concurrently: {destination}")
                    os.replace(tmp_path, destination)
                    tmp_path = None  # replaced, no longer at tmp_path

                dest_published = True

                # Unlink temp (after link it's a separate inode)
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass  # temp cleanup failure is not critical
                tmp_path = None

            except FileExistsError:
                raise SafeFsError("Concurrent destination creation detected")

            # fsync parent directory
            if _IS_LINUX:
                os.fsync(dirfd)

            # Final verification: lstat destination
            try:
                dest_lstat = os.lstat(destination)
            except OSError as exc:
                raise SafeFsError(f"Destination lstat after publish failed: {exc}")

            if stat.S_ISLNK(dest_lstat.st_mode):
                raise SafeFsError("Published destination is a symlink")
            if not stat.S_ISREG(dest_lstat.st_mode):
                raise SafeFsError("Published destination is not a regular file")

            # Open destination for fstat verification
            verify_flags = os.O_RDONLY
            if _IS_LINUX:
                verify_flags |= _O_NOFOLLOW
            try:
                verify_fd = os.open(destination, verify_flags)
            except OSError as exc:
                raise SafeFsError(f"Failed to open destination for verification: {exc}")
            try:
                verify_fstat = os.fstat(verify_fd)
            finally:
                os.close(verify_fd)

            if not stat.S_ISREG(verify_fstat.st_mode):
                raise SafeFsError("Destination verify fstat: not regular")

            if require_uid is not None and verify_fstat.st_uid != require_uid:
                raise SafeFsError(f"Destination uid {verify_fstat.st_uid} != required {require_uid}")
            if require_gid is not None and verify_fstat.st_gid != require_gid:
                raise SafeFsError(f"Destination gid {verify_fstat.st_gid} != required {require_gid}")
            if require_mode is not None:
                actual_perm = verify_fstat.st_mode & 0o777
                if actual_perm != require_mode:
                    raise SafeFsError(f"Destination mode {oct(actual_perm)} != required {oct(require_mode)}")

            return PublishResult(
                path=destination,
                uid=verify_fstat.st_uid,
                gid=verify_fstat.st_gid,
                mode=verify_fstat.st_mode,
            )

        except SafeFsError:
            raise
        except Exception as exc:
            raise SafeFsError(str(exc)) from exc

    except SafeFsError as exc:
        # Cleanup: if destination was published, try to unlink it
        if dest_published and os.path.exists(destination):
            try:
                os.unlink(destination)
            except OSError:
                raise SafeFsError(
                    f"{exc} | CLEANUP_INCOMPLETE=true",
                    cleanup_incomplete=True,
                ) from exc
        raise
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
        os.close(dirfd)
