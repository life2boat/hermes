from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_deploy_preflight as preflight  # noqa: E402


SHA = "a" * 40
OTHER_SHA = "b" * 40
IMAGE = "sha256:" + "c" * 64
REPOSITORY = "life2boat/hermes"
URLS = (
    "git@github-healbite:life2boat/hermes.git",
    "git@github.com:life2boat/hermes.git",
    "https://github.com/life2boat/hermes.git",
)


def _result(argv, *, returncode: int = 0, stdout: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def _provenance(
    *,
    remote_url: str = URLS[0],
    ref_sha: str = SHA,
    remote_sha: str = SHA,
    ci_ok: bool = True,
    alias_host: str = "github.com",
):
    def git_output(*args: str) -> str:
        if args == ("remote",):
            return "github\norigin"
        if args == ("remote", "get-url", "github"):
            return remote_url
        if args == ("rev-parse", "--verify", "refs/remotes/github/main^{commit}"):
            return ref_sha
        raise AssertionError(args)

    def run(argv, **_kwargs):
        if argv[:2] == ("ssh", "-G"):
            return _result(argv, stdout=f"hostname {alias_host}\nuser git\n")
        if "ls-remote" in argv:
            return _result(argv, stdout=f"{remote_sha}\trefs/heads/main\n")
        if argv[:2] == ("gh", "run"):
            runs = [
                {
                    "name": name,
                    "status": "completed",
                    "conclusion": "success" if ci_ok else "failure",
                    "headSha": SHA,
                }
                for name in ("Tests", "Lint", "Typecheck", "Nix")
            ]
            return _result(argv, stdout=json.dumps(runs))
        raise AssertionError(argv)

    return git_output, run


@pytest.mark.parametrize("remote_url", URLS)
def test_canonical_provenance_accepts_authorized_transports(remote_url: str, tmp_path: Path) -> None:
    git_output, run = _provenance(remote_url=remote_url)
    preflight.validate_canonical_provenance(
        root=tmp_path,
        expected_sha=SHA,
        canonical_repository_slug=REPOSITORY,
        canonical_remote="github",
        allowed_remote_urls=URLS,
        canonical_main_ref="refs/remotes/github/main",
        canonical_main_branch="refs/heads/main",
        required_ci_workflows=("Tests", "Lint", "Typecheck", "Nix"),
        git_output=git_output,
        run=run,
    )


@pytest.mark.parametrize(
    ("remote_url", "code"),
    (
        ("git@github.com:wrong/hermes.git", "canonical-remote-url-mismatch"),
        ("git@github.com:life2boat/wrong.git", "canonical-remote-url-mismatch"),
        ("git@unapproved:life2boat/hermes.git", "canonical-remote-url-mismatch"),
    ),
)
def test_canonical_provenance_rejects_unapproved_repository_identity(
    remote_url: str, code: str, tmp_path: Path
) -> None:
    git_output, run = _provenance(remote_url=remote_url)
    with pytest.raises(preflight.DeployPreflightError, match=code):
        preflight.validate_canonical_provenance(
            root=tmp_path,
            expected_sha=SHA,
            canonical_repository_slug=REPOSITORY,
            canonical_remote="github",
            allowed_remote_urls=URLS,
            canonical_main_ref="refs/remotes/github/main",
            canonical_main_branch="refs/heads/main",
            required_ci_workflows=("Tests",),
            git_output=git_output,
            run=run,
        )


@pytest.mark.parametrize(
    ("kwargs", "code"),
    (
        ({"canonical_remote": "origin"}, "canonical-provenance-policy"),
        ({"canonical_main_ref": "refs/remotes/origin/main"}, "canonical-provenance-policy"),
        ({"ref_sha": OTHER_SHA}, "canonical-main-sha-mismatch"),
        ({"remote_sha": OTHER_SHA}, "canonical-remote-head-mismatch"),
        ({"ci_ok": False}, "required-ci-not-passing"),
    ),
)
def test_canonical_provenance_rejects_substitution_and_stale_state(
    kwargs: dict[str, object], code: str, tmp_path: Path
) -> None:
    run_kwargs = {key: value for key, value in kwargs.items() if key in {"ref_sha", "remote_sha", "ci_ok"}}
    git_output, run = _provenance(**run_kwargs)
    with pytest.raises(preflight.DeployPreflightError, match=code):
        preflight.validate_canonical_provenance(
            root=tmp_path,
            expected_sha=SHA,
            canonical_repository_slug=REPOSITORY,
            canonical_remote=kwargs.get("canonical_remote", "github"),
            allowed_remote_urls=URLS,
            canonical_main_ref=kwargs.get("canonical_main_ref", "refs/remotes/github/main"),
            canonical_main_branch="refs/heads/main",
            required_ci_workflows=("Tests", "Lint", "Typecheck", "Nix"),
            git_output=git_output,
            run=run,
        )


def _canonical_mounts() -> tuple[preflight.MountRecord, ...]:
    return (
        preflight.MountRecord(
            "/var/lib/hermes/production-db/healbite.db",
            "/home/hermes/healbite.db",
            "bind",
            False,
        ),
    )


def _mount_kwargs() -> dict[str, object]:
    return {
        "expected_source": "/var/lib/hermes/production-db/healbite.db",
        "expected_target": "/home/hermes/healbite.db",
        "expected_type": "bind",
        "expected_read_only": False,
        "legacy_sources": ("/home/hermes/healbite.db",),
    }


def test_database_mount_accepts_exact_canonical_record() -> None:
    assessment = preflight.validate_database_mounts(_canonical_mounts(), **_mount_kwargs())
    assert assessment.canonical_count == 1
    assert assessment.legacy_count == 0


@pytest.mark.parametrize(
    ("mounts", "code"),
    (
        ((), "missing-canonical-db-mount"),
        ((preflight.MountRecord("/home/hermes/healbite.db", "/home/hermes/healbite.db", "bind", False),), "legacy-db-mount-present"),
        (_canonical_mounts() * 2, "duplicate-db-target"),
        ((preflight.MountRecord("/var/lib/hermes/production-db/healbite.db", "/wrong", "bind", False),), "wrong-db-target"),
        ((preflight.MountRecord("/wrong", "/home/hermes/healbite.db", "bind", False),), "wrong-db-source"),
        (_canonical_mounts() + (preflight.MountRecord("/other", "/home/hermes", "bind", False),), "conflicting-db-mount"),
    ),
)
def test_database_mount_rejects_missing_legacy_duplicate_and_shadowing(
    mounts: tuple[preflight.MountRecord, ...], code: str
) -> None:
    with pytest.raises(preflight.DeployPreflightError, match=code):
        preflight.validate_database_mounts(mounts, **_mount_kwargs())


def test_database_mount_rejects_unsafe_source_and_live_future_mismatch() -> None:
    with pytest.raises(preflight.DeployPreflightError, match="unsafe-db-source-path"):
        preflight.validate_database_mounts(
            _canonical_mounts(),
            **_mount_kwargs(),
            source_path_validator=lambda _path: (_ for _ in ()).throw(
                preflight.DeployPreflightError("unsafe-db-source-path")
            ),
        )
    mismatched_live = (
        preflight.MountRecord("/var/lib/hermes/production-db/healbite.db", "/home/hermes/healbite.db", "bind", True),
    )
    with pytest.raises(preflight.DeployPreflightError, match="wrong-db-mount-mode"):
        preflight.validate_live_future_database_mounts(
            _canonical_mounts(), mismatched_live, **_mount_kwargs()
        )


@pytest.mark.parametrize("phase", ("build", "deploy"))
def test_capacity_rejects_one_byte_below_and_accepts_boundary(phase: str, tmp_path: Path) -> None:
    kwargs = dict(
        phase=phase,
        filesystem=tmp_path,
        minimum_free_basis_points=1000,
        estimated_peak_incremental_build_bytes=100,
        build_peak_multiplier=2,
        staging_safety_margin_bytes=50,
        formula_source="reviewed-test-policy",
        target_sha=SHA,
        target_image_id=IMAGE if phase == "deploy" else None,
        total_bytes=1_000,
    )
    required = 250 if phase == "build" else 100
    exact = preflight.validate_capacity(**kwargs, available_bytes=required)
    assert exact.required_bytes == required
    with pytest.raises(preflight.DeployPreflightError, match="insufficient-capacity"):
        preflight.validate_capacity(**kwargs, available_bytes=required - 1)
    assert preflight.validate_capacity(**kwargs, available_bytes=required + 1).available_bytes == required + 1


def test_lease_acquisition_binding_and_explicit_expired_recovery(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "operation.json"
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 0)
    owner = frozenset({0})
    now = datetime.now(timezone.utc)
    lease = preflight.acquire_deployment_lease(
        path=path,
        allowed_owner_uids=owner,
        operation_class="deploy",
        canonical_repository="https://github.com/life2boat/hermes.git",
        target_sha=SHA,
        target_image_id=IMAGE,
        timeout_seconds=60,
        now=now,
    )
    preflight.validate_held_lease(
        lease,
        allowed_owner_uids=owner,
        operation_class="deploy",
        canonical_repository="https://github.com/life2boat/hermes.git",
        target_sha=SHA,
        target_image_id=IMAGE,
        now=now,
    )
    with pytest.raises(preflight.DeployPreflightError, match="deployment-lease-active"):
        preflight.acquire_deployment_lease(
            path=path,
            allowed_owner_uids=owner,
            operation_class="deploy",
            canonical_repository="https://github.com/life2boat/hermes.git",
            target_sha=SHA,
            target_image_id=IMAGE,
            timeout_seconds=60,
            now=now,
        )
    with pytest.raises(preflight.DeployPreflightError, match="deployment-lease-target-sha-mismatch"):
        preflight.validate_held_lease(
            lease,
            allowed_owner_uids=owner,
            operation_class="deploy",
            canonical_repository="https://github.com/life2boat/hermes.git",
            target_sha=OTHER_SHA,
            target_image_id=IMAGE,
            now=now,
        )
    monkeypatch.setattr(preflight, "_owner_active", lambda _document: False)
    with pytest.raises(preflight.DeployPreflightError, match="deployment-lease-recovery-confirmation"):
        preflight.recover_expired_lease(
            path=path,
            allowed_owner_uids=owner,
            expected_fingerprint=lease.holder_fingerprint,
            confirmation="wrong",
            now=now + timedelta(seconds=61),
        )
    preflight.recover_expired_lease(
        path=path,
        allowed_owner_uids=owner,
        expected_fingerprint=lease.holder_fingerprint,
        confirmation=preflight.LEASE_RECOVERY_CONFIRMATION,
        now=now + timedelta(seconds=61),
    )
    assert not path.exists()


def test_lease_denies_unix_non_root_before_file_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "operation.json"
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 1000)
    with pytest.raises(
        preflight.DeployPreflightError,
        match="deployment-lease-owner",
    ):
        preflight.acquire_deployment_lease(
            path=path,
            allowed_owner_uids=frozenset({0}),
            operation_class="deploy",
            canonical_repository="https://github.com/life2boat/hermes.git",
            target_sha=SHA,
            target_image_id=IMAGE,
            timeout_seconds=60,
        )
    assert not path.exists()


def test_lease_denies_unavailable_geteuid_before_file_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "operation.json"
    monkeypatch.delattr(preflight.os, "geteuid", raising=False)
    with pytest.raises(
        preflight.DeployPreflightError,
        match="deployment-lease-owner-unavailable",
    ) as error:
        preflight.acquire_deployment_lease(
            path=path,
            allowed_owner_uids=frozenset({0}),
            operation_class="deploy",
            canonical_repository="https://github.com/life2boat/hermes.git",
            target_sha=SHA,
            target_image_id=IMAGE,
            timeout_seconds=60,
        )
    assert not path.exists()
    assert "secret" not in str(error.value).lower()
    assert "identity" not in str(error.value).lower()


def test_lease_denies_geteuid_error_before_file_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "operation.json"

    def unavailable() -> int:
        raise OSError("synthetic platform error")

    monkeypatch.setattr(preflight.os, "geteuid", unavailable)
    with pytest.raises(
        preflight.DeployPreflightError,
        match="deployment-lease-owner-unavailable",
    ):
        preflight.acquire_deployment_lease(
            path=path,
            allowed_owner_uids=frozenset({0}),
            operation_class="deploy",
            canonical_repository="https://github.com/life2boat/hermes.git",
            target_sha=SHA,
            target_image_id=IMAGE,
            timeout_seconds=60,
        )
    assert not path.exists()
