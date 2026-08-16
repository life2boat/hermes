import os
import stat
import errno
import pytest
from ops.secret_remediation_r1.safe_fs import safe_open_source, publish_file, SafeFsError, _IS_LINUX

def test_safe_fs_source_symlink_reject(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("hello")
    symlink = tmp_path / "symlink.txt"
    try:
        os.symlink(target, symlink)
    except OSError:
        pytest.skip("Symlinks not supported on this OS")

    with pytest.raises(SafeFsError, match="Source is a symlink"):
        safe_open_source(str(symlink))

def test_safe_fs_source_nonregular_reject(tmp_path):
    with pytest.raises(SafeFsError, match="not a regular file"):
        safe_open_source(str(tmp_path))

def test_safe_fs_destination_preexists_reject(tmp_path):
    dest = tmp_path / "dest.txt"
    dest.write_text("hello")
    with pytest.raises(SafeFsError, match="Destination already exists"):
        publish_file(str(dest), b"content")

def test_safe_fs_write_failure_cleanup(tmp_path, monkeypatch):
    def mock_write(*args):
        raise OSError("Disk full")

    monkeypatch.setattr(os, "write", mock_write)
    dest = tmp_path / "dest.txt"
    with pytest.raises(SafeFsError, match="Disk full"):
        publish_file(str(dest), b"content")

    # verify cleanup
    assert not list(tmp_path.glob(".tmp_*"))

def test_safe_fs_publish_success(tmp_path):
    dest = tmp_path / "dest.txt"
    res = publish_file(str(dest), b"success", mode=0o644)
    assert res.path == str(dest)
    assert dest.read_text() == "success"

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only dirfd tests")
def test_safe_fs_destination_check_dirfd_relative(tmp_path, monkeypatch):
    orig_stat = os.stat
    calls = []
    def mock_stat(path, *args, **kwargs):
        if "dir_fd" in kwargs and kwargs["dir_fd"] is not None:
            calls.append((path, kwargs["dir_fd"]))
        return orig_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", mock_stat)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")

    # Destination check should use basename
    assert ("dest.txt", calls[0][1]) in calls

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only dirfd tests")
def test_safe_fs_temp_creation_dirfd_relative(tmp_path, monkeypatch):
    orig_open = os.open
    calls = []
    def mock_open(path, flags, mode=0o777, *args, **kwargs):
        if "dir_fd" in kwargs and kwargs["dir_fd"] is not None:
            calls.append((path, flags, kwargs["dir_fd"]))
        return orig_open(path, flags, mode, *args, **kwargs)
    monkeypatch.setattr(os, "open", mock_open)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")

    temp_calls = [c for c in calls if str(c[0]).startswith(".tmp_")]
    assert len(temp_calls) == 1
    assert temp_calls[0][1] & os.O_CREAT
    assert temp_calls[0][1] & getattr(os, "O_NOFOLLOW", 0)

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only flags test")
def test_safe_fs_temp_open_flags(tmp_path, monkeypatch):
    orig_open = os.open
    flags_used = []
    def mock_open(path, flags, mode=0o777, *args, **kwargs):
        if str(path).startswith(".tmp_"):
            flags_used.append(flags)
        return orig_open(path, flags, mode, *args, **kwargs)
    monkeypatch.setattr(os, "open", mock_open)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")

    assert len(flags_used) == 1
    f = flags_used[0]
    assert f & os.O_CREAT
    assert f & os.O_EXCL
    assert f & getattr(os, "O_NOFOLLOW", 0)

def test_safe_fs_short_write_completed(tmp_path, monkeypatch):
    orig_write = os.write
    write_calls = []
    def mock_write(fd, data):
        write_calls.append(len(data))
        # force short write
        return orig_write(fd, data[:1])
    monkeypatch.setattr(os, "write", mock_write)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"hello")
    assert dest.read_text() == "hello"
    assert len(write_calls) == 5

def test_safe_fs_zero_write_fails(tmp_path, monkeypatch):
    def mock_write(fd, data):
        return 0
    monkeypatch.setattr(os, "write", mock_write)

    dest = tmp_path / "dest.txt"
    with pytest.raises(SafeFsError, match="Zero bytes written"):
        publish_file(str(dest), b"hello")

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only link test")
def test_safe_fs_publication_dirfd_relative(tmp_path, monkeypatch):
    orig_link = getattr(os, "link", None)
    calls = []
    def mock_link(src, dst, *args, **kwargs):
        calls.append((src, dst, kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd")))
        return orig_link(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, "link", mock_link)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")

    assert len(calls) == 1
    src, dst, src_dir_fd, dst_dir_fd = calls[0]
    assert str(src).startswith(".tmp_")
    assert dst == "dest.txt"
    assert src_dir_fd is not None
    assert src_dir_fd == dst_dir_fd

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_concurrent_destination_creation_fails(tmp_path, monkeypatch):
    orig_stat = os.stat
    dest = tmp_path / "dest.txt"

    def mock_stat(path, *args, **kwargs):
        if path == "dest.txt" and "dir_fd" in kwargs:
            # It's checking if destination exists.
            # Let it pass the precheck, but immediately create it to simulate race.
            dest.write_text("concurrent")
        return orig_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", mock_stat)

    # Mock link to raise FileExistsError to simulate race after precheck
    if _IS_LINUX:
        orig_link = os.link
        def mock_link(*args, **kwargs):
            raise FileExistsError("concurrent")
        monkeypatch.setattr(os, "link", mock_link)

        dest2 = tmp_path / "dest2.txt"
        with pytest.raises(SafeFsError, match="Concurrent destination creation"):
            publish_file(str(dest2), b"content")

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only identity test")
def test_safe_fs_temp_substitution_fails(tmp_path, monkeypatch):
    orig_stat = os.stat
    def mock_stat(path, *args, **kwargs):
        res = orig_stat(path, *args, **kwargs)
        if str(path).startswith(".tmp_") and kwargs.get("follow_symlinks") is False:
            # modify stat result to simulate substitution
            res_list = list(res)
            res_list[1] += 1 # st_ino is index 1
            return os.stat_result(res_list)
        return res
    monkeypatch.setattr(os, "stat", mock_stat)

    dest = tmp_path / "dest.txt"
    with pytest.raises(SafeFsError, match="identity substituted"):
        publish_file(str(dest), b"content")

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only flags test")
def test_safe_fs_final_open_dirfd_relative(tmp_path, monkeypatch):
    orig_open = os.open
    calls = []
    def mock_open(path, flags, mode=0o777, *args, **kwargs):
        if path == "dest.txt" and kwargs.get("dir_fd") is not None:
            calls.append((path, flags, kwargs["dir_fd"]))
        return orig_open(path, flags, mode, *args, **kwargs)
    monkeypatch.setattr(os, "open", mock_open)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")

    assert len(calls) == 1
    assert calls[0][1] & getattr(os, "O_NOFOLLOW", 0)

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only")
def test_safe_fs_final_inode_identity(tmp_path, monkeypatch):
    orig_fstat = os.fstat
    call_count = 0
    def mock_fstat(fd):
        nonlocal call_count
        res = orig_fstat(fd)
        call_count += 1
        if call_count == 3: # The final fstat
            res_list = list(res)
            res_list[1] += 1 # st_ino is index 1
            return os.stat_result(res_list)
        return res
    monkeypatch.setattr(os, "fstat", mock_fstat)

    dest = tmp_path / "dest.txt"
    with pytest.raises(SafeFsError, match="inode identity mismatch"):
        publish_file(str(dest), b"content")

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_temp_cleanup_failure_propagates(tmp_path, monkeypatch):
    orig_unlink = os.unlink
    def mock_unlink(path, *args, **kwargs):
        if str(path).startswith(".tmp_"):
            raise OSError(errno.EPERM, "Permission denied")
        return orig_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", mock_unlink)

    dest = tmp_path / "dest.txt"
    with pytest.raises(SafeFsError) as excinfo:
        publish_file(str(dest), b"content")
    assert excinfo.value.cleanup_incomplete is True
    assert "CLEANUP_INCOMPLETE" in str(excinfo.value)

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_destination_cleanup_failure_propagates(tmp_path, monkeypatch):
    orig_unlink = os.unlink
    orig_fstat = os.fstat

    def mock_unlink(path, *args, **kwargs):
        if path == "dest.txt":
            raise OSError(errno.EPERM, "Permission denied")
        return orig_unlink(path, *args, **kwargs)

    call_count = 0
    def mock_fstat(fd):
        nonlocal call_count
        res = orig_fstat(fd)
        call_count += 1
        if call_count == 3: # fail final verification to trigger cleanup
            res_list = list(res)
            res_list[1] += 1 # st_ino
            return os.stat_result(res_list)
        return res

    monkeypatch.setattr(os, "unlink", mock_unlink)
    monkeypatch.setattr(os, "fstat", mock_fstat)

    dest = tmp_path / "dest.txt"
    with pytest.raises(SafeFsError) as excinfo:
        publish_file(str(dest), b"content")

    assert excinfo.value.cleanup_incomplete is True

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_cleanup_uses_dirfd(tmp_path, monkeypatch):
    orig_unlink = os.unlink
    calls = []
    def mock_unlink(path, *args, **kwargs):
        calls.append((path, kwargs.get("dir_fd")))
        return orig_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", mock_unlink)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")

    assert len(calls) >= 1
    assert str(calls[0][0]).startswith(".tmp_")
    assert calls[0][1] is not None

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_parent_fsync_after_publication(tmp_path, monkeypatch):
    orig_fsync = os.fsync
    calls = []
    def mock_fsync(fd):
        calls.append(fd)
        return orig_fsync(fd)
    monkeypatch.setattr(os, "fsync", mock_fsync)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")

    assert len(calls) == 2 # 1 for temp file, 1 for parent dir

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_parent_fsync_after_cleanup(tmp_path, monkeypatch):
    orig_fsync = os.fsync
    calls = []
    def mock_fsync(fd):
        calls.append(fd)
        return orig_fsync(fd)

    def mock_write(*args):
        raise OSError("Disk full")

    monkeypatch.setattr(os, "fsync", mock_fsync)
    monkeypatch.setattr(os, "write", mock_write)

    dest = tmp_path / "dest.txt"
    with pytest.raises(SafeFsError):
        publish_file(str(dest), b"content")

    if _IS_LINUX:
        assert len(calls) == 1 # 1 for parent dir cleanup
    else:
        assert len(calls) == 0

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_fd_cleanup_success(tmp_path, monkeypatch):
    orig_close = os.close
    closes = list()
    def mock_close(fd):
        closes.append(fd)
        return orig_close(fd)
    monkeypatch.setattr(os, "close", mock_close)

    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")

    # We can't easily assert exactly which fd, but we know multiple fds are closed
    assert len(closes) >= 3 # parent dirfd, temp fd, final verify fd

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_fd_cleanup_failure_path(tmp_path, monkeypatch):
    orig_close = os.close
    closes = list()
    def mock_close(fd):
        closes.append(fd)
        return orig_close(fd)

    def mock_write(*args):
        raise OSError("Disk full")

    monkeypatch.setattr(os, "close", mock_close)
    monkeypatch.setattr(os, "write", mock_write)

    dest = tmp_path / "dest.txt"
    with pytest.raises(SafeFsError):
        publish_file(str(dest), b"content")

    assert len(closes) >= 2 # parent dirfd, temp fd

def test_safe_fs_existing_destination_symlink_rejected(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("hello")
    symlink = tmp_path / "dest.txt"
    try:
        os.symlink(target, symlink)
    except OSError:
        pytest.skip("Symlinks not supported")

    with pytest.raises(SafeFsError, match="already exists"):
        publish_file(str(symlink), b"content")

def test_safe_fs_existing_destination_regular_rejected(tmp_path):
    dest = tmp_path / "dest.txt"
    dest.write_text("hello")
    with pytest.raises(SafeFsError, match="already exists"):
        publish_file(str(dest), b"content")

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_metadata_verification_failure_cleans_publication(tmp_path, monkeypatch):
    dest = tmp_path / "dest.txt"

    orig_fstat = os.fstat
    call_count = 0
    def mock_fstat(fd):
        nonlocal call_count
        res = orig_fstat(fd)
        call_count += 1
        if call_count == 3: # final verification
            res_list = list(res)
            res_list[0] = stat.S_IFDIR | 0o755 # force non-regular (st_mode is index 0)
            return os.stat_result(res_list)
        return res
    monkeypatch.setattr(os, "fstat", mock_fstat)

    with pytest.raises(SafeFsError, match="not regular"):
        publish_file(str(dest), b"content")

    assert not dest.exists()

@pytest.mark.skipif(not _IS_LINUX, reason="Linux only test")
def test_safe_fs_cleanup_fsync_failure_propagates(tmp_path, monkeypatch):
    dest = tmp_path / "dest.txt"

    # Force an earlier controlled failure to enter cleanup
    def mock_write(*args):
        raise OSError("Controlled write failure")
    monkeypatch.setattr(os, "write", mock_write)

    # Allow required cleanup unlink(s)
    orig_unlink = os.unlink
    def mock_unlink(path, *args, **kwargs):
        return orig_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", mock_unlink)

    # Force parent-directory fsync to raise OSError
    orig_fsync = os.fsync
    def mock_fsync(fd):
        # We only want to fail during the cleanup fsync, but since write fails,
        # the normal tmp_fd fsync is never reached anyway.
        # We fail all fsyncs to ensure the directory fsync fails.
        raise OSError("Fsync failed during cleanup")
    monkeypatch.setattr(os, "fsync", mock_fsync)

    with pytest.raises(SafeFsError) as excinfo:
        publish_file(str(dest), b"content")

    assert excinfo.value.cleanup_incomplete is True
    assert "CLEANUP_INCOMPLETE" in str(excinfo.value)
