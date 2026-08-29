#!/usr/bin/env python3
"""Pure fail-closed gates used by the canonical ordinary-deploy CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
LEASE_RECOVERY_CONFIRMATION = "RECOVER_EXPIRED_DEPLOYMENT_LEASE"
MAX_LEASE_BYTES = 16 * 1024


class DeployPreflightError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise DeployPreflightError(code)


@dataclass(frozen=True)
class MountRecord:
    source: str
    target: str
    mount_type: str
    read_only: bool


@dataclass(frozen=True)
class DatabaseMountAssessment:
    canonical_count: int
    legacy_count: int
    duplicate_target_count: int
    conflicting_count: int
    canonical_mount: MountRecord


@dataclass(frozen=True)
class CapacityAssessment:
    phase: str
    target_sha: str
    target_image_id: str | None
    total_bytes: int
    required_bytes: int
    available_bytes: int
    formula_source: str


@dataclass(frozen=True)
class DeploymentLease:
    path: Path
    inode: int
    holder_fingerprint: str


def _remote_identity(
    remote_url: str,
    *,
    repository_slug: str,
    run: Callable[..., object],
) -> None:
    owner, repository = repository_slug.split("/", 1)
    ssh = re.fullmatch(
        r"(?P<user>[^@/:]+)@(?P<host>[^/:]+):"
        r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?",
        remote_url,
    )
    https = re.fullmatch(
        r"https://(?P<host>[^/]+)/(?P<owner>[^/]+)/"
        r"(?P<repo>[^/]+?)(?:\.git)?",
        remote_url,
    )
    if https is not None:
        values = https.groupdict()
        if (
            values["host"] == "github.com"
            and values["owner"] == owner
            and values["repo"] == repository
        ):
            return
        _fail("canonical-remote-identity-mismatch")
    if ssh is None:
        _fail("canonical-remote-identity-mismatch")
    values = ssh.groupdict()
    if (
        values["user"] != "git"
        or values["owner"] != owner
        or values["repo"] != repository
    ):
        _fail("canonical-remote-identity-mismatch")
    if values["host"] == "github.com":
        return
    resolved = run(("ssh", "-G", values["host"]), timeout=10)
    if getattr(resolved, "returncode", 1) != 0:
        _fail("canonical-remote-alias-unproven")
    fields: dict[str, str] = {}
    for line in getattr(resolved, "stdout", "").splitlines():
        name, separator, value = line.partition(" ")
        if separator and name in {"hostname", "user"} and name not in fields:
            fields[name] = value.strip()
    if fields != {"hostname": "github.com", "user": "git"}:
        _fail("canonical-remote-alias-unproven")


def validate_canonical_provenance(
    *,
    root: Path,
    expected_sha: str,
    canonical_repository_slug: str,
    canonical_remote: str,
    allowed_remote_urls: tuple[str, ...],
    canonical_main_ref: str,
    canonical_main_branch: str,
    required_ci_workflows: tuple[str, ...],
    git_output: Callable[..., str],
    run: Callable[..., object],
) -> None:
    if (
        not SHA_RE.fullmatch(expected_sha)
        or canonical_remote != "github"
        or canonical_main_ref != "refs/remotes/github/main"
        or canonical_main_branch != "refs/heads/main"
    ):
        _fail("canonical-provenance-policy")
    remotes = {line for line in git_output("remote").splitlines() if line}
    if canonical_remote not in remotes:
        _fail("canonical-remote-missing")
    remote_url = git_output("remote", "get-url", canonical_remote)
    if remote_url not in allowed_remote_urls:
        _fail("canonical-remote-url-mismatch")
    _remote_identity(
        remote_url,
        repository_slug=canonical_repository_slug,
        run=run,
    )
    if (
        git_output("rev-parse", "--verify", f"{canonical_main_ref}^{{commit}}")
        != expected_sha
    ):
        _fail("canonical-main-sha-mismatch")
    remote_head = run(
        (
            "git",
            "-C",
            str(root),
            "ls-remote",
            "--exit-code",
            canonical_remote,
            canonical_main_branch,
        ),
        timeout=30,
    )
    lines = [
        line.split()
        for line in getattr(remote_head, "stdout", "").splitlines()
        if line.strip()
    ]
    if (
        getattr(remote_head, "returncode", 1) != 0
        or lines != [[expected_sha, canonical_main_branch]]
    ):
        _fail("canonical-remote-head-mismatch")
    ci = run(
        (
            "gh",
            "run",
            "list",
            "--repo",
            canonical_repository_slug,
            "--commit",
            expected_sha,
            "--event",
            "push",
            "--limit",
            "100",
            "--json",
            "name,status,conclusion,headSha",
        ),
        cwd=root,
        timeout=45,
    )
    if getattr(ci, "returncode", 1) != 0:
        _fail("required-ci-unavailable")
    try:
        runs = json.loads(getattr(ci, "stdout", ""))
    except json.JSONDecodeError:
        _fail("required-ci-invalid")
    if not isinstance(runs, list):
        _fail("required-ci-invalid")
    latest: dict[str, dict[str, object]] = {}
    for item in runs:
        if not isinstance(item, dict):
            _fail("required-ci-invalid")
        name = item.get("name")
        if isinstance(name, str) and name not in latest:
            latest[name] = item
    for workflow in required_ci_workflows:
        item = latest.get(workflow)
        if (
            item is None
            or item.get("headSha") != expected_sha
            or item.get("status") != "completed"
            or item.get("conclusion") != "success"
        ):
            _fail("required-ci-not-passing")


def compose_mounts_from_document(
    document: dict[str, object],
    *,
    service_name: str,
) -> tuple[MountRecord, ...]:
    try:
        volumes = document["services"][service_name].get("volumes", [])  # type: ignore[index,union-attr]
    except (KeyError, TypeError):
        _fail("compose-mount-document")
    if not isinstance(volumes, list):
        _fail("compose-mount-document")
    mounts: list[MountRecord] = []
    for item in volumes:
        if not isinstance(item, dict):
            _fail("compose-mount-document")
        values = (
            item.get("source"),
            item.get("target"),
            item.get("type"),
            item.get("read_only", False),
        )
        if (
            not all(isinstance(value, str) for value in values[:3])
            or not isinstance(values[3], bool)
        ):
            _fail("compose-mount-document")
        mounts.append(MountRecord(*values))  # type: ignore[arg-type]
    return tuple(mounts)


def live_mounts_from_document(document: object) -> tuple[MountRecord, ...]:
    if not isinstance(document, list):
        _fail("live-mount-document")
    mounts: list[MountRecord] = []
    for item in document:
        if not isinstance(item, dict):
            _fail("live-mount-document")
        source, target, mount_type, read_write = (
            item.get("Source"),
            item.get("Destination"),
            item.get("Type"),
            item.get("RW"),
        )
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or not isinstance(mount_type, str)
            or not isinstance(read_write, bool)
        ):
            _fail("live-mount-document")
        mounts.append(MountRecord(source, target, mount_type, not read_write))
    return tuple(mounts)


def _absolute(value: str, *, code: str) -> Path:
    pure = PurePosixPath(value)
    if (
        not pure.is_absolute()
        or ".." in pure.parts
        or str(pure) != value
        or value.startswith("//")
    ):
        _fail(code)
    return Path(value)


def validate_database_mounts(
    mounts: Sequence[MountRecord],
    *,
    expected_source: str,
    expected_target: str,
    expected_type: str,
    expected_read_only: bool,
    legacy_sources: tuple[str, ...],
    source_path_validator: Callable[[Path], None] | None = None,
) -> DatabaseMountAssessment:
    source_path = _absolute(expected_source, code="unsafe-db-source-path")
    target_path = PurePosixPath(
        _absolute(expected_target, code="unsafe-db-target-path")
    )
    for legacy in legacy_sources:
        _absolute(legacy, code="unsafe-legacy-db-path")
    if any(mount.source in legacy_sources for mount in mounts):
        _fail("legacy-db-mount-present")
    at_target = [mount for mount in mounts if mount.target == expected_target]
    if len(at_target) > 1:
        _fail("duplicate-db-target")
    if any(
        mount.source == expected_source and mount.target != expected_target
        for mount in mounts
    ):
        _fail("wrong-db-target")
    if not at_target:
        _fail("missing-canonical-db-mount")
    canonical = at_target[0]
    if canonical.source != expected_source:
        _fail("wrong-db-source")
    if canonical.mount_type != expected_type:
        _fail("wrong-db-mount-type")
    if canonical.read_only != expected_read_only:
        _fail("wrong-db-mount-mode")
    for mount in mounts:
        other = PurePosixPath(mount.target)
        if mount is canonical or not other.is_absolute():
            continue
        if (
            mount.source == expected_source
            or target_path.is_relative_to(other)
            or other.is_relative_to(target_path)
        ):
            _fail("conflicting-db-mount")
    if source_path_validator is not None:
        source_path_validator(source_path)
    return DatabaseMountAssessment(1, 0, 0, 0, canonical)


def validate_live_future_database_mounts(
    future_mounts: Sequence[MountRecord],
    live_mounts: Sequence[MountRecord],
    **kwargs,
) -> DatabaseMountAssessment:
    future = validate_database_mounts(future_mounts, **kwargs)
    live = validate_database_mounts(live_mounts, **kwargs)
    if future.canonical_mount != live.canonical_mount:
        _fail("live-future-db-mount-mismatch")
    return future


def validate_capacity(
    *,
    phase: str,
    filesystem: Path,
    minimum_free_basis_points: int,
    estimated_peak_incremental_build_bytes: int,
    build_peak_multiplier: int,
    staging_safety_margin_bytes: int,
    formula_source: str,
    target_sha: str,
    target_image_id: str | None = None,
    total_bytes: int | None = None,
    available_bytes: int | None = None,
) -> CapacityAssessment:
    if phase not in {"build", "deploy"} or not SHA_RE.fullmatch(target_sha):
        _fail("capacity-operation")
    if phase == "deploy":
        if target_image_id is None or not IMAGE_ID_RE.fullmatch(target_image_id):
            _fail("capacity-target-image")
    elif target_image_id is not None:
        _fail("capacity-build-image-binding")
    components = (
        minimum_free_basis_points,
        estimated_peak_incremental_build_bytes,
        build_peak_multiplier,
        staging_safety_margin_bytes,
    )
    if any(value <= 0 for value in components) or minimum_free_basis_points > 10_000:
        _fail("capacity-policy")
    try:
        values = os.statvfs(filesystem)
    except OSError:
        _fail("capacity-filesystem-unavailable")
    total = values.f_blocks * values.f_frsize if total_bytes is None else total_bytes
    available = (
        values.f_bavail * values.f_frsize
        if available_bytes is None
        else available_bytes
    )
    percentage = (total * minimum_free_basis_points + 9_999) // 10_000
    operation_floor = (
        estimated_peak_incremental_build_bytes * build_peak_multiplier
        + staging_safety_margin_bytes
        if phase == "build"
        else staging_safety_margin_bytes
    )
    required = max(percentage, operation_floor)
    if total <= 0 or available < required:
        _fail("insufficient-capacity")
    return CapacityAssessment(
        phase,
        target_sha,
        target_image_id,
        total,
        required,
        available,
        formula_source,
    )


def _utc(value: datetime | None) -> datetime:
    return datetime.now(timezone.utc) if value is None else value


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail("deployment-lease-invalid")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail("deployment-lease-invalid")


def _process_token(pid: int) -> str:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        token = text[text.rindex(")") + 2 :].split()[19]
    except (OSError, UnicodeError, ValueError, IndexError):
        _fail("lease-owner-proof-unavailable")
    if not token.isdigit():
        _fail("lease-owner-proof-unavailable")
    return token


def _owner_active(document: dict[str, object]) -> bool:
    pid, token = document.get("pid"), document.get("process_start_token")
    if not isinstance(pid, int) or not isinstance(token, str):
        _fail("deployment-lease-invalid")
    try:
        return _process_token(pid) == token
    except DeployPreflightError:
        return False


def _load_lease(
    path: Path,
    *,
    allowed_owner_uids: frozenset[int],
) -> tuple[dict[str, object], os.stat_result]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail("deployment-lease-missing")
    except OSError:
        _fail("deployment-lease-invalid")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid not in allowed_owner_uids
    ):
        _fail("deployment-lease-invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            _fail("deployment-lease-race")
        payload = bytearray()
        while len(payload) <= MAX_LEASE_BYTES:
            chunk = os.read(descriptor, min(4096, MAX_LEASE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
    except DeployPreflightError:
        raise
    except OSError:
        _fail("deployment-lease-invalid")
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if len(payload) > MAX_LEASE_BYTES:
        _fail("deployment-lease-invalid")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _fail("deployment-lease-invalid")
    fields = {
        "version", "operation_class", "canonical_repository", "target_sha",
        "target_image_id", "created_at", "expires_at", "pid",
        "process_start_token", "holder_fingerprint",
    }
    if not isinstance(document, dict) or set(document) != fields or document.get("version") != 1:
        _fail("deployment-lease-invalid")
    return document, metadata


def _binding(
    document: dict[str, object],
    *,
    canonical_repository: str,
    target_sha: str,
    target_image_id: str,
) -> None:
    checks = (
        ("canonical_repository", canonical_repository, "repository"),
        ("target_sha", target_sha, "target-sha"),
        ("target_image_id", target_image_id, "target-image"),
    )
    for field, expected, code in checks:
        if document.get(field) != expected:
            _fail(f"deployment-lease-{code}-mismatch")


def assert_lease_available(
    *,
    path: Path,
    allowed_owner_uids: frozenset[int],
    canonical_repository: str,
    target_sha: str,
    target_image_id: str,
    now: datetime | None = None,
) -> None:
    if not path.exists():
        return
    document, _ = _load_lease(path, allowed_owner_uids=allowed_owner_uids)
    _binding(
        document,
        canonical_repository=canonical_repository,
        target_sha=target_sha,
        target_image_id=target_image_id,
    )
    if _utc(now) < _parse_iso(document.get("expires_at")) and _owner_active(document):
        _fail("deployment-lease-active")
    _fail("deployment-lease-recovery-required")


def validate_deployment_lease_owner(
    *,
    allowed_owner_uids: frozenset[int],
) -> int:
    get_euid = getattr(os, "geteuid", None)
    if not callable(get_euid):
        _fail("deployment-lease-owner-unavailable")
    try:
        effective_uid = get_euid()
    except (AttributeError, OSError):
        _fail("deployment-lease-owner-unavailable")
    if effective_uid not in allowed_owner_uids:
        _fail("deployment-lease-owner")
    return effective_uid


def acquire_deployment_lease(
    *,
    path: Path,
    allowed_owner_uids: frozenset[int],
    operation_class: str,
    canonical_repository: str,
    target_sha: str,
    target_image_id: str,
    timeout_seconds: int,
    now: datetime | None = None,
) -> DeploymentLease:
    validate_deployment_lease_owner(
        allowed_owner_uids=allowed_owner_uids,
    )
    assert_lease_available(
        path=path,
        allowed_owner_uids=allowed_owner_uids,
        canonical_repository=canonical_repository,
        target_sha=target_sha,
        target_image_id=target_image_id,
        now=now,
    )
    timestamp, pid = _utc(now), os.getpid()
    token = _process_token(pid)
    fingerprint = hashlib.sha256(
        "\0".join(
            (
                operation_class,
                canonical_repository,
                target_sha,
                target_image_id,
                str(pid),
                token,
                _iso(timestamp),
            )
        ).encode()
    ).hexdigest()
    document = {
        "version": 1,
        "operation_class": operation_class,
        "canonical_repository": canonical_repository,
        "target_sha": target_sha,
        "target_image_id": target_image_id,
        "created_at": _iso(timestamp),
        "expires_at": _iso(timestamp + timedelta(seconds=timeout_seconds)),
        "pid": pid,
        "process_start_token": token,
        "holder_fingerprint": fingerprint,
    }
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    old_umask = os.umask(0o077)
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        assert_lease_available(
            path=path,
            allowed_owner_uids=allowed_owner_uids,
            canonical_repository=canonical_repository,
            target_sha=target_sha,
            target_image_id=target_image_id,
            now=timestamp,
        )
        _fail("deployment-lease-active")
    except DeployPreflightError:
        raise
    except OSError:
        _fail("deployment-lease-write")
    finally:
        os.umask(old_umask)
    return DeploymentLease(path, path.lstat().st_ino, fingerprint)


def validate_held_lease(
    lease: DeploymentLease,
    *,
    allowed_owner_uids: frozenset[int],
    operation_class: str,
    canonical_repository: str,
    target_sha: str,
    target_image_id: str,
    now: datetime | None = None,
) -> None:
    document, metadata = _load_lease(
        lease.path,
        allowed_owner_uids=allowed_owner_uids,
    )
    _binding(
        document,
        canonical_repository=canonical_repository,
        target_sha=target_sha,
        target_image_id=target_image_id,
    )
    if (
        metadata.st_ino != lease.inode
        or document.get("holder_fingerprint") != lease.holder_fingerprint
        or document.get("operation_class") != operation_class
        or not _owner_active(document)
        or _utc(now) >= _parse_iso(document.get("expires_at"))
    ):
        _fail("deployment-lease-ownership-mismatch")


def release_deployment_lease(
    lease: DeploymentLease,
    *,
    allowed_owner_uids: frozenset[int],
) -> None:
    document, metadata = _load_lease(
        lease.path,
        allowed_owner_uids=allowed_owner_uids,
    )
    if (
        metadata.st_ino != lease.inode
        or document.get("holder_fingerprint") != lease.holder_fingerprint
    ):
        _fail("deployment-lease-ownership-mismatch")
    try:
        lease.path.unlink()
        directory = os.open(lease.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        _fail("deployment-lease-release")


def recover_expired_lease(
    *,
    path: Path,
    allowed_owner_uids: frozenset[int],
    expected_fingerprint: str,
    confirmation: str,
    now: datetime | None = None,
) -> None:
    if confirmation != LEASE_RECOVERY_CONFIRMATION:
        _fail("deployment-lease-recovery-confirmation")
    document, metadata = _load_lease(path, allowed_owner_uids=allowed_owner_uids)
    if document.get("holder_fingerprint") != expected_fingerprint:
        _fail("deployment-lease-recovery-fingerprint")
    if _utc(now) < _parse_iso(document.get("expires_at")):
        _fail("deployment-lease-not-expired")
    if _owner_active(document):
        _fail("deployment-lease-owner-active")
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            _fail("deployment-lease-race")
        path.unlink()
    except DeployPreflightError:
        raise
    except OSError:
        _fail("deployment-lease-recovery")
def validate_private_directory(
    path: Path,
    *,
    repository_root: Path,
) -> int:
    if not path.is_absolute():
        _fail("recovery-backup-parent-invalid")

    current = path
    while True:
        try:
            st = current.lstat()
            if stat.S_ISLNK(st.st_mode):
                _fail("recovery-backup-parent-invalid")
            if stat.S_IMODE(st.st_mode) & 0o022:
                _fail("recovery-backup-parent-invalid")
        except OSError:
            _fail("recovery-backup-parent-invalid")
        if current.parent == current:
            break
        current = current.parent

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        st = os.fstat(fd)
        os.close(fd)
    except OSError:
        _fail("recovery-backup-parent-invalid")

    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        _fail("recovery-backup-parent-invalid")
    if st.st_uid != 0 or st.st_gid != 0:
        _fail("recovery-backup-parent-invalid")
    if stat.S_IMODE(st.st_mode) != 0o700:
        _fail("recovery-backup-parent-invalid")

    try:
        resolved_path = path.resolve(strict=True)
        resolved_repo = repository_root.resolve(strict=True)
    except OSError:
        _fail("recovery-backup-parent-invalid")

    try:
        resolved_path.relative_to(resolved_repo)
    except ValueError:
        pass
    else:
        _fail("recovery-backup-parent-invalid")

    return st.st_ino
