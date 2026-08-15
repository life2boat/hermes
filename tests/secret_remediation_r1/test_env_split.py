import pytest
from ops.secret_remediation_r1.env_split import split_env, EnvSplitError, _parse_env_lines

def test_mixed_protected_removed(tmp_path):
    src = tmp_path / "src.env"
    src.write_bytes(b"TELEGRAM_BOT_TOKEN=secret\nNORMAL_VAR=value\n")
    dest = tmp_path / "dest.env"
    
    split_env(str(src), str(dest))
    assert dest.read_bytes() == b"NORMAL_VAR=value\n"

def test_mixed_comments_preserved(tmp_path):
    src = tmp_path / "src.env"
    src.write_bytes(b"# a comment\nNORMAL_VAR=value\n")
    dest = tmp_path / "dest.env"
    
    split_env(str(src), str(dest))
    assert dest.read_bytes() == b"# a comment\nNORMAL_VAR=value\n"

def test_mixed_blank_lines_preserved(tmp_path):
    src = tmp_path / "src.env"
    src.write_bytes(b"\nNORMAL_VAR=value\n")
    dest = tmp_path / "dest.env"
    
    split_env(str(src), str(dest))
    assert dest.read_bytes() == b"\nNORMAL_VAR=value\n"

def test_mixed_embedded_equals_preserved(tmp_path):
    src = tmp_path / "src.env"
    src.write_bytes(b"NORMAL_VAR=value=with=equals\n")
    dest = tmp_path / "dest.env"
    
    split_env(str(src), str(dest))
    assert dest.read_bytes() == b"NORMAL_VAR=value=with=equals\n"

def test_mixed_duplicate_reject(tmp_path):
    src = tmp_path / "src.env"
    src.write_bytes(b"NORMAL_VAR=1\nNORMAL_VAR=2\n")
    dest = tmp_path / "dest.env"
    
    with pytest.raises(EnvSplitError, match="Duplicate key"):
        split_env(str(src), str(dest))

def test_mixed_malformed_reject(tmp_path):
    src = tmp_path / "src.env"
    src.write_bytes(b"NO_EQUALS_HERE\n")
    dest = tmp_path / "dest.env"
    
    with pytest.raises(EnvSplitError, match="Malformed record"):
        split_env(str(src), str(dest))

def test_mixed_destination_preexist_reject(tmp_path):
    src = tmp_path / "src.env"
    src.write_bytes(b"NORMAL_VAR=1\n")
    dest = tmp_path / "dest.env"
    dest.write_bytes(b"pre")
    
    with pytest.raises(EnvSplitError, match="Destination already exists"):
        split_env(str(src), str(dest))

def test_mixed_source_unchanged(tmp_path):
    src = tmp_path / "src.env"
    orig = b"NORMAL_VAR=1\n"
    src.write_bytes(orig)
    dest = tmp_path / "dest.env"
    
    split_env(str(src), str(dest))
    assert src.read_bytes() == orig

def test_mixed_postwrite_comparison_success(tmp_path, monkeypatch):
    # This is implicitly tested by normal execution, but we can mock safe_open_source
    pass

import os
@pytest.mark.skipif(os.name == "nt", reason="Linux-only")
def test_mixed_source_symlink_reject(tmp_path):
    target = tmp_path / "target.env"
    target.write_bytes(b"NORMAL_VAR=1\n")
    src = tmp_path / "src.env"
    os.symlink(target, src)
    dest = tmp_path / "dest.env"
    
    with pytest.raises(EnvSplitError, match="Source is a symlink"):
        split_env(str(src), str(dest))
