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
_O_BINARY = getattr(os, "O_BINARY", 0)


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

    flags = os.O_RDONLY | _O_BINARY
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

    if not _IS_LINUX:
        return -1, parent_lstat

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW

    try:
        dirfd = os.open(parent, flags)
    except OSError as exc:
        raise SafeFsError(f"open parent failed: {exc}")

    try:
        dirfd_fstat = os.fstat(dirfd)
    except OSError as exc:
        os.close(dirfd)
        raise SafeFsError(f"fstat parent failed: {exc}")

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
    if not basename or basename in (".", "..") or os.sep in basename or (os.altsep and os.altsep in basename):
        raise SafeFsError(f"Invalid destination basename: {destination!r}")

    parent = os.path.dirname(destination)
    if not parent:
        parent = "."

    dirfd, _dir_fstat = _open_parent_dirfd(parent)

    tmp_basename: Optional[str] = None
    fd: int = -1
    dest_published: bool = False

    # Store inode of the created temp file to verify no substitution occurs
    temp_inode: Optional[int] = None
    temp_dev: Optional[int] = None

    try:
        # Verify destination does not already exist via dirfd
        try:
            if _IS_LINUX:
                os.stat(basename, dir_fd=dirfd, follow_symlinks=False)
            else:
                os.lstat(destination)
            raise SafeFsError(f"Destination already exists: {basename}")
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise SafeFsError(f"Unexpected error checking destination: {exc}")

        # Generate random temp name
        rand_suffix = secrets.token_hex(16)
        tmp_basename = f".tmp_{rand_suffix}"
        tmp_path = os.path.join(parent, tmp_basename)

        # Create temp with O_CREAT|O_EXCL via dirfd
        creat_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY
        if _IS_LINUX:
            creat_flags |= _O_NOFOLLOW

        try:
            if _IS_LINUX:
                fd = os.open(tmp_basename, creat_flags, mode, dir_fd=dirfd)
            else:
                fd = os.open(tmp_path, creat_flags, mode)
        except FileExistsError:
            raise SafeFsError("Temp file collision (concurrent creation)")
        except OSError as exc:
            raise SafeFsError(f"Failed to create temp file: {exc}")

        try:
            # Complete write loop
            bytes_written = 0
            while bytes_written < len(content):
                written = os.write(fd, content[bytes_written:])
                if written == 0:
                    raise SafeFsError("Zero bytes written (no progress)")
                bytes_written += written

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

            temp_inode = tmp_fstat.st_ino
            temp_dev = tmp_fstat.st_dev

            os.close(fd)
            fd = -1

            # Verify temp file identity hasn't been substituted
            try:
                if _IS_LINUX:
                    tmp_lstat = os.stat(tmp_basename, dir_fd=dirfd, follow_symlinks=False)
                else:
                    tmp_lstat = os.lstat(tmp_path)
            except OSError as exc:
                raise SafeFsError(f"Failed to stat temp file before publish: {exc}")

            if tmp_lstat.st_ino != temp_inode or tmp_lstat.st_dev != temp_dev:
                raise SafeFsError("Temp file identity substituted before publication")

            # Exclusive publication via hard link
            try:
                if _IS_LINUX and hasattr(os, "link"):
                    os.link(
                        tmp_basename,
                        basename,
                        src_dir_fd=dirfd,
                        dst_dir_fd=dirfd,
                        follow_symlinks=False,
                    )
                else:
                    # Windows fallback: non-atomic but best-effort
                    if os.path.exists(destination):
                        raise SafeFsError(f"Destination appeared concurrently: {destination}")
                    os.replace(tmp_path, destination)
                    tmp_basename = None  # replaced, no longer at tmp_basename

                dest_published = True

                # Unlink temp (after link it's a separate inode)
                if tmp_basename:
                    try:
                        if _IS_LINUX:
                            os.unlink(tmp_basename, dir_fd=dirfd)
                        else:
                            os.unlink(tmp_path)
                    except OSError as exc:
                        if exc.errno != errno.ENOENT:
                            raise SafeFsError("Failed to unlink temp file", cleanup_incomplete=True) from exc
                tmp_basename = None

            except FileExistsError:
                raise SafeFsError("Concurrent destination creation detected")

            # fsync parent directory
            if _IS_LINUX:
                os.fsync(dirfd)

            # Final verification
            # Open destination for fstat verification via dirfd
            verify_flags = os.O_RDONLY | _O_BINARY
            if _IS_LINUX:
                verify_flags |= _O_NOFOLLOW
            try:
                if _IS_LINUX:
                    verify_fd = os.open(basename, verify_flags, dir_fd=dirfd)
                else:
                    verify_fd = os.open(destination, verify_flags)
            except OSError as exc:
                raise SafeFsError(f"Failed to open destination for verification: {exc}")
            try:
                verify_fstat = os.fstat(verify_fd)
            finally:
                os.close(verify_fd)

            if not stat.S_ISREG(verify_fstat.st_mode):
                raise SafeFsError("Destination verify fstat: not regular")

            if temp_inode is not None and (verify_fstat.st_ino != temp_inode or verify_fstat.st_dev != temp_dev):
                raise SafeFsError("Final verification inode identity mismatch")

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
            if fd != -1:
                try: os.close(fd); fd = -1
                except OSError: pass
            raise
        except Exception as exc:
            if fd != -1:
                try: os.close(fd); fd = -1
                except OSError: pass
            raise SafeFsError(str(exc)) from exc

    except SafeFsError as exc:
        # Cleanup: if destination was published, try to unlink it
        cleanup_incomplete = getattr(exc, 'cleanup_incomplete', False)

        if dest_published:
            try:
                if _IS_LINUX:
                    os.unlink(basename, dir_fd=dirfd)
                else:
                    os.unlink(destination)
            except OSError as rm_exc:
                if rm_exc.errno != errno.ENOENT:
                    cleanup_incomplete = True

        if tmp_basename is not None:
            try:
                if _IS_LINUX:
                    os.unlink(tmp_basename, dir_fd=dirfd)
                else:
                    os.unlink(os.path.join(parent, tmp_basename))
            except OSError as rm_exc:
                if rm_exc.errno != errno.ENOENT:
                    cleanup_incomplete = True

        if _IS_LINUX:
            try:
                os.fsync(dirfd)
            except OSError:
                pass # Sync failure on cleanup isn't itself an incomplete state if unlink succeeded

        if cleanup_incomplete and not getattr(exc, 'cleanup_incomplete', False):
            raise SafeFsError(f"{exc} | CLEANUP_INCOMPLETE=true", cleanup_incomplete=True) from exc

        raise
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if dirfd != -1:
            try:
                os.close(dirfd)
            except OSError:
                pass
