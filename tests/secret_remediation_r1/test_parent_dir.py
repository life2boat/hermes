import os
import pytest
from ops.secret_remediation_r1.parent_dir import ensure_parent_directory, ParentDirError

@pytest.mark.skipif(os.name == "nt", reason="Linux-only permissions")
def test_parent_atomic_create_success(tmp_path, monkeypatch):
    import ops.secret_remediation_r1.parent_dir
    monkeypatch.setattr(ops.secret_remediation_r1.parent_dir, "PARENT_REQUIRED_UID", os.getuid())
    monkeypatch.setattr(ops.secret_remediation_r1.parent_dir, "PARENT_REQUIRED_GID", os.getgid())
    
    target = tmp_path / "hermes"
    ensure_parent_directory(str(target))
    
    st = os.stat(target)
    assert st.st_mode & 0o777 == 0o700

@pytest.mark.skipif(os.name == "nt", reason="Linux-only permissions")
def test_parent_existing_wrong_metadata_reject(tmp_path, monkeypatch):
    import ops.secret_remediation_r1.parent_dir
    monkeypatch.setattr(ops.secret_remediation_r1.parent_dir, "PARENT_REQUIRED_UID", os.getuid())
    monkeypatch.setattr(ops.secret_remediation_r1.parent_dir, "PARENT_REQUIRED_GID", os.getgid())
    
    target = tmp_path / "hermes"
    target.mkdir(mode=0o755)
    
    with pytest.raises(ParentDirError, match="Existing dir has wrong mode"):
        ensure_parent_directory(str(target))

@pytest.mark.skipif(os.name == "nt", reason="Linux-only")
def test_parent_existing_symlink_reject(tmp_path):
    target = tmp_path / "hermes"
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, target)
    
    with pytest.raises(ParentDirError, match="Target is a symlink"):
        ensure_parent_directory(str(target))
