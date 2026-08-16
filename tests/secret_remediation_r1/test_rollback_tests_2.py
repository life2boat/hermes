
import os
import stat
import errno
import pytest
from ops.secret_remediation_r1.rollback import execute_rollback, RemediationPrestate, RollbackError
from ops.secret_remediation_r1 import rollback as rb_module
from ops.secret_remediation_r1.constants import PROD_PARENT_DIR_PATH

def test_rollback_linux_parent_symlink(monkeypatch):
    monkeypatch.setattr(rb_module, "_IS_LINUX", True)
    
    class DummyStat:
        def __init__(self, mode, ino, dev):
            self.st_mode = mode
            self.st_ino = ino
            self.st_dev = dev
            
    def mock_lstat(path):
        return DummyStat(stat.S_IFLNK | 0o777, 1, 1)
        
    monkeypatch.setattr(os, "lstat", mock_lstat)
    
    prestate = RemediationPrestate(b"", "", b"", "", b"", 0o644, 0o644, False)
    
    with pytest.raises(RollbackError, match="open_parent_dirfd: parent path is not a directory or is a symlink"):
        execute_rollback(prestate)

def test_rollback_linux_parent_inode_mismatch(monkeypatch):
    monkeypatch.setattr(rb_module, "_IS_LINUX", True)
    
    class DummyStat:
        def __init__(self, mode, ino, dev):
            self.st_mode = mode
            self.st_ino = ino
            self.st_dev = dev
            
    def mock_lstat(path):
        return DummyStat(stat.S_IFDIR | 0o777, 1, 1)
    
    def mock_fstat(fd):
        return DummyStat(stat.S_IFDIR | 0o777, 2, 1)
        
    monkeypatch.setattr(os, "lstat", mock_lstat)
    monkeypatch.setattr(os, "open", lambda p, f: 99)
    monkeypatch.setattr(os, "fstat", mock_fstat)
    monkeypatch.setattr(os, "close", lambda fd: None)
    
    prestate = RemediationPrestate(b"", "", b"", "", b"", 0o644, 0o644, False)
    
    with pytest.raises(RollbackError, match="open_parent_dirfd: parent identity mismatch"):
        execute_rollback(prestate)

def test_rollback_linux_fsync_failure(monkeypatch):
    monkeypatch.setattr(rb_module, "_IS_LINUX", True)
    
    class DummyStat:
        def __init__(self, mode, ino, dev):
            self.st_mode = mode
            self.st_ino = ino
            self.st_dev = dev
            
    def mock_lstat(path):
        return DummyStat(stat.S_IFDIR | 0o777, 1, 1)
    
    def mock_fstat(fd):
        return DummyStat(stat.S_IFDIR | 0o777, 1, 1)
        
    def mock_stat(path, dir_fd, follow_symlinks):
        return DummyStat(stat.S_IFREG | 0o777, 3, 1)
        
    monkeypatch.setattr(os, "lstat", mock_lstat)
    monkeypatch.setattr(os, "open", lambda p, f: 99)
    monkeypatch.setattr(os, "fstat", mock_fstat)
    monkeypatch.setattr(os, "stat", mock_stat)
    monkeypatch.setattr(os, "unlink", lambda p, dir_fd: None)
    
    def mock_fsync(fd):
        raise OSError(errno.EIO, "I/O error")
        
    monkeypatch.setattr(os, "fsync", mock_fsync)
    monkeypatch.setattr(os, "close", lambda fd: None)
    
    prestate = RemediationPrestate(b"", "", b"", "", b"", 0o644, 0o644, False)
    
    with pytest.raises(RollbackError, match="fsync parent after unlink"):
        execute_rollback(prestate)

def test_rollback_linux_close_error_propagation(monkeypatch):
    monkeypatch.setattr(rb_module, "_IS_LINUX", True)
    
    class DummyStat:
        def __init__(self, mode, ino, dev):
            self.st_mode = mode
            self.st_ino = ino
            self.st_dev = dev
            
    def mock_lstat(path):
        return DummyStat(stat.S_IFDIR | 0o777, 1, 1)
    
    def mock_fstat(fd):
        return DummyStat(stat.S_IFDIR | 0o777, 1, 1)
        
    def mock_stat(path, dir_fd, follow_symlinks):
        return DummyStat(stat.S_IFREG | 0o777, 3, 1)
        
    monkeypatch.setattr(os, "lstat", mock_lstat)
    monkeypatch.setattr(os, "open", lambda p, f: 99)
    monkeypatch.setattr(os, "fstat", mock_fstat)
    monkeypatch.setattr(os, "stat", mock_stat)
    monkeypatch.setattr(os, "unlink", lambda p, dir_fd: None)
    monkeypatch.setattr(os, "fsync", lambda fd: None)
    
    def mock_close(fd):
        raise OSError(errno.EBADF, "Bad file descriptor")
        
    monkeypatch.setattr(os, "close", mock_close)
    
    prestate = RemediationPrestate(b"", "", b"", "", b"", 0o644, 0o644, False)
    
    with pytest.raises(RollbackError, match="close_parent_dirfd: .*Bad file descriptor"):
        execute_rollback(prestate)

def test_rollback_linux_child_directory_symlink_fails(monkeypatch):
    monkeypatch.setattr(rb_module, "_IS_LINUX", True)
    
    class DummyStat:
        def __init__(self, mode, ino, dev):
            self.st_mode = mode
            self.st_ino = ino
            self.st_dev = dev
            
    def mock_lstat(path):
        return DummyStat(stat.S_IFDIR | 0o777, 1, 1)
    
    def mock_fstat(fd):
        return DummyStat(stat.S_IFDIR | 0o777, 1, 1)
        
    def mock_stat(path, dir_fd=None, follow_symlinks=True):
        if path == "hermes":
            return DummyStat(stat.S_IFLNK | 0o777, 3, 1)
        return DummyStat(stat.S_IFREG | 0o777, 3, 1)
        
    monkeypatch.setattr(os, "lstat", mock_lstat)
    monkeypatch.setattr(os, "open", lambda p, f, dir_fd=None: 99)
    monkeypatch.setattr(os, "fstat", mock_fstat)
    monkeypatch.setattr(os, "stat", mock_stat)
    monkeypatch.setattr(os, "unlink", lambda p, dir_fd: None)
    monkeypatch.setattr(os, "fsync", lambda fd: None)
    monkeypatch.setattr(os, "close", lambda fd: None)
    
    prestate = RemediationPrestate(b"", "", b"", "", b"", 0o644, 0o644, True)
    
    with pytest.raises(RollbackError, match="empty-check: not a directory or is a symlink"):
        execute_rollback(prestate)

