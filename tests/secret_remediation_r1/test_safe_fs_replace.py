import os
import stat
import errno
import pytest
from ops.secret_remediation_r1.safe_fs import replace_existing_file, SafeFsError, _IS_LINUX

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

