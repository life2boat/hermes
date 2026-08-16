import errno
import os
import stat
from types import SimpleNamespace

import pytest

import ops.secret_remediation_r1.parent_dir as parent_dir
from ops.secret_remediation_r1.parent_dir import ParentDirError, ensure_parent_directory


@pytest.mark.skipif(os.name == "nt", reason="Linux-only permissions")
def test_parent_atomic_create_success(tmp_path, monkeypatch):
    import ops.secret_remediation_r1.parent_dir

    monkeypatch.setattr(
        ops.secret_remediation_r1.parent_dir, "PARENT_REQUIRED_UID", os.getuid()
    )
    monkeypatch.setattr(
        ops.secret_remediation_r1.parent_dir, "PARENT_REQUIRED_GID", os.getgid()
    )

    target = tmp_path / "hermes"
    ensure_parent_directory(str(target))

    st = os.stat(target)
    assert st.st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Linux-only permissions")
def test_parent_existing_wrong_metadata_reject(tmp_path, monkeypatch):
    import ops.secret_remediation_r1.parent_dir

    monkeypatch.setattr(
        ops.secret_remediation_r1.parent_dir, "PARENT_REQUIRED_UID", os.getuid()
    )
    monkeypatch.setattr(
        ops.secret_remediation_r1.parent_dir, "PARENT_REQUIRED_GID", os.getgid()
    )

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


def _directory_stat(
    *,
    mode: int = 0o700,
    uid: int = 0,
    gid: int = 0,
    dev: int = 1,
    ino: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_uid=uid,
        st_gid=gid,
        st_dev=dev,
        st_ino=ino,
    )


def _install_dirfd_mocks(monkeypatch, *, mkdir_error: OSError | None = None):
    calls: dict[str, list[tuple[tuple[object, ...], dict[str, object]]]] = {
        "close": [],
        "fchmod": [],
        "fchown": [],
        "fsync": [],
        "mkdir": [],
        "open": [],
        "stat": [],
    }
    monkeypatch.setattr(parent_dir, "_IS_LINUX", True)
    monkeypatch.setattr(parent_dir, "PARENT_REQUIRED_UID", 0)
    monkeypatch.setattr(parent_dir, "PARENT_REQUIRED_GID", 0)

    def fake_open(*args, **kwargs):
        calls["open"].append((args, kwargs))
        return 101 if len(calls["open"]) == 1 else 102

    def fake_mkdir(*args, **kwargs):
        calls["mkdir"].append((args, kwargs))
        if mkdir_error is not None:
            raise mkdir_error

    def fake_stat(*args, **kwargs):
        calls["stat"].append((args, kwargs))
        return _directory_stat()

    def fake_close(*args, **kwargs):
        calls["close"].append((args, kwargs))

    monkeypatch.setattr(parent_dir.os, "open", fake_open)
    monkeypatch.setattr(parent_dir.os, "mkdir", fake_mkdir)
    monkeypatch.setattr(parent_dir.os, "stat", fake_stat)
    monkeypatch.setattr(parent_dir.os, "fstat", lambda _fd: _directory_stat())
    monkeypatch.setattr(parent_dir.os, "close", fake_close)
    monkeypatch.setattr(
        parent_dir.os,
        "fchmod",
        lambda *args, **kwargs: calls["fchmod"].append((args, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        parent_dir.os,
        "fchown",
        lambda *args, **kwargs: calls["fchown"].append((args, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        parent_dir.os,
        "fsync",
        lambda *args, **kwargs: calls["fsync"].append((args, kwargs)),
    )
    return calls


def test_parent_creation_uses_dirfd(monkeypatch):
    calls = _install_dirfd_mocks(monkeypatch)

    ensure_parent_directory("/etc/hermes")

    assert calls["mkdir"] == [(("hermes", 0o700), {"dir_fd": 101})]
    assert calls["stat"] == [(("hermes",), {"dir_fd": 101, "follow_symlinks": False})]
    assert calls["open"][1][0][0] == "hermes"
    assert calls["open"][1][1]["dir_fd"] == 101


def test_parent_existing_child_open_uses_same_dirfd(monkeypatch):
    calls = _install_dirfd_mocks(
        monkeypatch,
        mkdir_error=FileExistsError(errno.EEXIST, "exists"),
    )

    ensure_parent_directory("/etc/hermes")

    assert calls["stat"] == [(("hermes",), {"dir_fd": 101, "follow_symlinks": False})]
    assert calls["open"][1][0][0] == "hermes"
    assert calls["open"][1][1]["dir_fd"] == 101
    assert calls["fsync"] == []


def test_parent_symlink_substitution_fails_closed(monkeypatch):
    calls = _install_dirfd_mocks(
        monkeypatch,
        mkdir_error=FileExistsError(errno.EEXIST, "exists"),
    )
    monkeypatch.setattr(
        parent_dir.os,
        "stat",
        lambda *_args, **_kwargs: SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o777,
            st_dev=1,
            st_ino=1,
        ),
    )

    with pytest.raises(ParentDirError, match="symlink"):
        ensure_parent_directory("/etc/hermes")

    assert len(calls["open"]) == 1
    assert [args[0] for args, _kwargs in calls["close"]] == [101]


def test_parent_race_replace_between_stat_and_open_fails_closed(monkeypatch):
    _install_dirfd_mocks(
        monkeypatch, mkdir_error=FileExistsError(errno.EEXIST, "exists")
    )
    monkeypatch.setattr(parent_dir.os, "fstat", lambda _fd: _directory_stat(ino=2))

    with pytest.raises(ParentDirError, match="identity changed"):
        ensure_parent_directory("/etc/hermes")


def test_parent_new_child_metadata_verified_via_fd(monkeypatch):
    calls = _install_dirfd_mocks(monkeypatch)
    stats = iter([_directory_stat(mode=0o755), _directory_stat()])
    monkeypatch.setattr(parent_dir.os, "fstat", lambda _fd: next(stats))

    ensure_parent_directory("/etc/hermes")

    assert calls["fchmod"] == [((102, 0o700), {})]
    assert calls["fchown"] == []


def test_parent_preexisting_wrong_metadata_not_repaired(monkeypatch):
    calls = _install_dirfd_mocks(
        monkeypatch,
        mkdir_error=FileExistsError(errno.EEXIST, "exists"),
    )
    monkeypatch.setattr(parent_dir.os, "fstat", lambda _fd: _directory_stat(mode=0o755))

    with pytest.raises(ParentDirError, match="wrong mode"):
        ensure_parent_directory("/etc/hermes")

    assert calls["fchmod"] == []
    assert calls["fchown"] == []


def test_parent_created_metadata_adjustment_fd_based(monkeypatch):
    calls = _install_dirfd_mocks(monkeypatch)
    stats = iter([_directory_stat(uid=123, gid=456), _directory_stat()])
    monkeypatch.setattr(parent_dir.os, "fstat", lambda _fd: next(stats))

    ensure_parent_directory("/etc/hermes")

    assert calls["fchown"] == [((102, 0, 0), {})]
    assert calls["fchmod"] == []


def test_parent_fsync_after_creation(monkeypatch):
    calls = _install_dirfd_mocks(monkeypatch)

    ensure_parent_directory("/etc/hermes")

    assert calls["fsync"] == [((101,), {})]


def test_parent_fd_cleanup_on_failure(monkeypatch):
    calls = _install_dirfd_mocks(monkeypatch)
    monkeypatch.setattr(
        parent_dir.os,
        "fstat",
        lambda _fd: (_ for _ in ()).throw(OSError("boom")),
    )

    with pytest.raises(OSError, match="boom"):
        ensure_parent_directory("/etc/hermes")

    assert [args[0] for args, _kwargs in calls["close"]] == [102, 101]
