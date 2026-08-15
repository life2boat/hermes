import os
import pytest
from ops.secret_remediation_r1.safe_fs import safe_open_source, publish_file, SafeFsError

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

def test_safe_fs_exclusive_temp(tmp_path, monkeypatch):
    import ops.secret_remediation_r1.safe_fs
    orig_open = os.open
    
    temp_names = []
    def mock_open(path, flags, *args):
        if ".tmp_" in str(path):
            temp_names.append(os.path.basename(path))
        return orig_open(path, flags, *args)
        
    monkeypatch.setattr(os, "open", mock_open)
    dest = tmp_path / "dest.txt"
    publish_file(str(dest), b"content")
    
    assert len(temp_names) == 1
    assert temp_names[0].startswith(".tmp_")
    assert not temp_names[0].endswith(".tmp")

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

def test_safe_fs_final_metadata(tmp_path):
    dest = tmp_path / "dest.txt"
    res = publish_file(str(dest), b"content", mode=0o600)
    if os.name != "nt":
        assert res.mode & 0o777 == 0o600
