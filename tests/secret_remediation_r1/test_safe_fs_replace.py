import os
import stat
import errno
import pytest
from ops.secret_remediation_r1.safe_fs import (
    replace_existing_file,
    SafeFsError,
    _IS_LINUX,
)


def test_replace_existing_file_success(tmp_path):
    dest = tmp_path / "target.txt"
    dest.write_text("old content")

    res = replace_existing_file(str(dest), b"new content")
    assert res.path == str(dest)
    assert dest.read_bytes() == b"new content"


def test_replace_existing_file_nonexistent_reject(tmp_path):
    dest = tmp_path / "nonexistent.txt"
    with pytest.raises(SafeFsError, match="Destination does not exist"):
        replace_existing_file(str(dest), b"new content")


def test_replace_existing_file_symlink_reject(tmp_path):
    dest = tmp_path / "link.txt"
    target = tmp_path / "target.txt"
    target.write_text("content")

    # create symlink
    try:
        os.symlink(str(target), str(dest))
    except OSError:
        pytest.skip("Symlinks not supported on this platform/user")

    with pytest.raises(SafeFsError, match="Destination is not a regular file"):
        replace_existing_file(str(dest), b"new content")


@pytest.mark.skipif(os.name == "nt", reason="Linux-only mode checks")
def test_replace_existing_file_preserves_metadata(tmp_path):
    dest = tmp_path / "target.txt"
    dest.write_text("old content")

    # Try to set mode to 0o600
    os.chmod(str(dest), stat.S_IFREG | 0o600)

    res = replace_existing_file(str(dest), b"new content")
    assert (res.mode & 0o777) == 0o600

    if _IS_LINUX:
        st = os.stat(str(dest))
        assert (st.st_mode & 0o777) == 0o600


def test_replace_existing_file_cleanup_on_write_fail(tmp_path, monkeypatch):
    dest = tmp_path / "target.txt"
    dest.write_text("old content")

    def fake_write(fd, b):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", fake_write)

    with pytest.raises(SafeFsError, match="No space left on device"):
        replace_existing_file(str(dest), b"new content")

    assert dest.read_text() == "old content"
    assert not any(p.name.startswith(".tmp_") for p in tmp_path.iterdir())


@pytest.mark.skipif(not _IS_LINUX, reason="Linux-only parent binding")
def test_replace_existing_parent_symlink_rejected(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    (real_parent / "target.txt").write_text("old content")
    linked_parent = tmp_path / "linked"
    os.symlink(real_parent, linked_parent)

    with pytest.raises(SafeFsError, match="Parent directory is a symlink"):
        replace_existing_file(str(linked_parent / "target.txt"), b"new content")


@pytest.mark.skipif(not _IS_LINUX, reason="Linux-only parent binding")
def test_replace_existing_parent_identity_mismatch_rejected(tmp_path, monkeypatch):
    dest = tmp_path / "target.txt"
    dest.write_text("old content")
    original_fstat = os.fstat
    parent_fd = None

    def mismatched_parent_fstat(fd):
        nonlocal parent_fd
        result = original_fstat(fd)
        if parent_fd is None and stat.S_ISDIR(result.st_mode):
            parent_fd = fd
        if fd == parent_fd:
            values = list(result)
            values[1] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "fstat", mismatched_parent_fstat)

    with pytest.raises(SafeFsError, match="Parent lstat/fstat identity mismatch"):
        replace_existing_file(str(dest), b"new content")

    assert dest.read_text() == "old content"


@pytest.mark.skipif(not _IS_LINUX, reason="Linux-only parent binding")
def test_replace_existing_uses_nofollow_parent_binding(tmp_path, monkeypatch):
    dest = tmp_path / "target.txt"
    dest.write_text("old content")
    original_open = os.open
    parent_open_flags = []

    def recording_open(path, flags, *args, **kwargs):
        if path == str(tmp_path) and "dir_fd" not in kwargs:
            parent_open_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    replace_existing_file(str(dest), b"new content")

    assert len(parent_open_flags) == 1
    assert parent_open_flags[0] & os.O_DIRECTORY
    assert parent_open_flags[0] & os.O_NOFOLLOW
