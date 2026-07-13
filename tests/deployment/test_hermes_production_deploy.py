from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_production_deploy as deploy  # noqa: E402


FAKE_SECRET = "placeholder-telegram-token"
IMAGE_A = "sha256:" + "a" * 64
IMAGE_B = "sha256:" + "b" * 64
IMAGE_DIGEST = "example.invalid/hermes@sha256:" + "e" * 64
REVISION = "c" * 40
OTHER_REVISION = "d" * 40
REVISION_LABEL = "org.opencontainers.image.revision"


@pytest.fixture
def protected_contract(tmp_path: Path) -> tuple[deploy.DeploymentContract, Path]:
    source = tmp_path / "host-secrets.env"
    source.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\n", encoding="utf-8")
    source.chmod(0o600)
    runtime = tmp_path / "run" / "hermes"
    runtime.parent.mkdir(mode=0o700)
    contract = replace(
        deploy.load_contract(),
        runtime_directory=runtime,
        secret_override=runtime / "hermes-secrets-override.yml",
        approved_secret_source=source,
        approved_source_owner_uids=frozenset({deploy._effective_uid()}),
    )
    return contract, source


def _prepare(contract: deploy.DeploymentContract, source: Path) -> None:
    deploy.prepare_secret_override(contract, source)


def _completed(argv, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _image_record(image_id: str, revision: object = REVISION) -> dict[str, object]:
    labels = {} if revision is None else {REVISION_LABEL: revision}
    return {"Id": image_id, "Config": {"Labels": labels}}


def _safe_docker_runner(
    calls: list[tuple[str, ...]],
    *,
    services: str = "hermes-bot\nqdrant\n",
    revisions: dict[str, object] | None = None,
):
    revisions = revisions or {}

    def runner(argv, **_kwargs):
        command = tuple(str(item) for item in argv)
        calls.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            image = command[-1]
            image_id = IMAGE_A if image == IMAGE_A else IMAGE_B
            revision = revisions.get(image, REVISION)
            return _completed(argv, stdout=json.dumps([_image_record(image_id, revision)]))
        if command[:2] == ("docker", "inspect"):
            return _completed(argv, stdout=f"running 0 {IMAGE_A}\n")
        if command[-2:] == ("config", "--services"):
            return _completed(argv, stdout=services, stderr=FAKE_SECRET)
        return _completed(argv, stderr=FAKE_SECRET)

    return runner


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def _allow_mocked_rollback_revision(monkeypatch) -> None:
    monkeypatch.setattr(deploy, "current_source_head_revision", lambda _contract: OTHER_REVISION)
    monkeypatch.setattr(deploy, "validate_rollback_revision", lambda *_args, **_kwargs: None)


def _runner_with_real_git(calls: list[tuple[str, ...]], **kwargs):
    docker_runner = _safe_docker_runner(calls, **kwargs)

    def runner(argv, **run_kwargs):
        command = tuple(str(item) for item in argv)
        if command[:1] == ("git",):
            return subprocess.run(
                list(command),
                text=True,
                capture_output=True,
                timeout=run_kwargs.get("timeout"),
                check=False,
            )
        return docker_runner(argv, **run_kwargs)

    return runner


@pytest.fixture
def repository_fixture(tmp_path: Path) -> tuple[deploy.DeploymentContract, str]:
    root = tmp_path / "repo"
    (root / "deploy").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "deploy" / "hermes-production.json", root / "deploy" / "hermes-production.json")
    shutil.copy2(
        REPO_ROOT / "deploy" / "docker-compose.production.yml",
        root / "deploy" / "docker-compose.production.yml",
    )
    shutil.copy2(REPO_ROOT / "docker-compose.yml", root / "docker-compose.yml")
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "audit@example.invalid", cwd=root)
    _git("config", "user.name", "Audit Fixture", cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "-qm", "fixture", cwd=root)
    head = _git("rev-parse", "HEAD", cwd=root)
    _git("update-ref", "refs/remotes/healbite-project/main", head, cwd=root)
    return deploy.load_contract(root), head


def test_manifest_is_canonical_and_secret_free() -> None:
    contract = deploy.load_contract()
    text = contract.manifest_path.read_text(encoding="utf-8")
    assert contract.project_name == "hermes-agent"
    assert contract.target_service == "hermes-bot"
    assert contract.runtime_directory == Path("/run/hermes")
    assert contract.secret_override == Path("/run/hermes/hermes-secrets-override.yml")
    assert contract.required_secret_names == ("TELEGRAM_BOT_TOKEN",)
    assert contract.approved_secret_source == Path("/etc/hermes/hermes-production.env")
    assert contract.approved_source_owner_uids == frozenset({0})
    assert contract.image_revision_label == REVISION_LABEL
    assert contract.allowed_revision_ref == "refs/remotes/healbite-project/main"
    assert FAKE_SECRET not in text


def test_non_posix_runtime_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(deploy.os, "geteuid", None)
    with pytest.raises(deploy.DeploymentContractError, match="posix-runtime-required"):
        deploy._effective_uid()


def test_repository_check_passes_on_clean_exact_head(repository_fixture) -> None:
    contract, head = repository_fixture
    deploy.validate_repository(contract, head)


def test_repository_check_rejects_dirty_worktree(repository_fixture) -> None:
    contract, head = repository_fixture
    (contract.root / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(deploy.DeploymentContractError, match="dirty-worktree"):
        deploy.validate_repository(contract, head)


def test_repository_check_rejects_revision_not_reachable_from_allowed_ref(repository_fixture) -> None:
    contract, _head = repository_fixture
    (contract.root / "new.txt").write_text("new", encoding="utf-8")
    _git("add", ".", cwd=contract.root)
    _git("commit", "-qm", "unpublished", cwd=contract.root)
    unpublished = _git("rev-parse", "HEAD", cwd=contract.root)
    with pytest.raises(deploy.DeploymentContractError, match="revision-not-allowed"):
        deploy.validate_repository(contract, unpublished)


def test_repository_check_rejects_legacy_worktree_reference(repository_fixture) -> None:
    contract, _head = repository_fixture
    text = contract.production_override.read_text(encoding="utf-8")
    contract.production_override.write_text(text + "\n# healbite-s71v2-r6-deploy\n", encoding="utf-8")
    _git("add", ".", cwd=contract.root)
    _git("commit", "-qm", "bad reference", cwd=contract.root)
    head = _git("rev-parse", "HEAD", cwd=contract.root)
    _git("update-ref", contract.allowed_revision_ref, head, cwd=contract.root)
    with pytest.raises(deploy.DeploymentContractError, match="legacy-reference"):
        deploy.validate_repository(contract, head)


def test_repository_check_rejects_tmp_override_reference(repository_fixture) -> None:
    contract, _head = repository_fixture
    text = contract.production_override.read_text(encoding="utf-8")
    contract.production_override.write_text(text + "\n# /tmp/hermes-secrets-override.yml\n", encoding="utf-8")
    _git("add", ".", cwd=contract.root)
    _git("commit", "-qm", "bad reference", cwd=contract.root)
    head = _git("rev-parse", "HEAD", cwd=contract.root)
    _git("update-ref", contract.allowed_revision_ref, head, cwd=contract.root)
    with pytest.raises(deploy.DeploymentContractError, match="legacy-reference"):
        deploy.validate_repository(contract, head)


def test_producer_creates_directory_and_override_modes(protected_contract) -> None:
    contract, source = protected_contract
    _prepare(contract, source)
    assert stat_mode(contract.runtime_directory) == 0o700
    assert stat_mode(contract.secret_override) == 0o600
    assert contract.secret_override.is_file()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_repeated_producer_is_deterministic_and_atomic(protected_contract) -> None:
    contract, source = protected_contract
    _prepare(contract, source)
    first = contract.secret_override.read_bytes()
    first_inode = contract.secret_override.stat().st_ino
    _prepare(contract, source)
    assert contract.secret_override.read_bytes() == first
    assert contract.secret_override.stat().st_ino != first_inode


def test_missing_telegram_token_fails_without_ambient_fallback(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    source.write_text("UNRELATED=value\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_SECRET)
    with pytest.raises(deploy.DeploymentContractError, match="required-secret-missing"):
        _prepare(contract, source)
    assert not contract.secret_override.exists()


def test_empty_telegram_token_fails(protected_contract) -> None:
    contract, source = protected_contract
    source.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    with pytest.raises(deploy.DeploymentContractError, match="required-secret-missing"):
        _prepare(contract, source)
    assert not contract.secret_override.exists()


def test_unexpected_secret_variable_fails(protected_contract) -> None:
    contract, source = protected_contract
    source.write_text(
        f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\nUNRELATED=value\n",
        encoding="utf-8",
    )
    with pytest.raises(deploy.DeploymentContractError, match="secret-source-variable-set"):
        _prepare(contract, source)


def test_duplicate_secret_variable_fails(protected_contract) -> None:
    contract, source = protected_contract
    source.write_text(
        f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\nTELEGRAM_BOT_TOKEN=duplicate\n",
        encoding="utf-8",
    )
    with pytest.raises(deploy.DeploymentContractError, match="secret-source-variable"):
        _prepare(contract, source)


@pytest.mark.parametrize(
    "content",
    [
        f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\x00suffix\n",
        f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\r\n",
    ],
)
def test_secret_source_control_characters_fail(protected_contract, content: str) -> None:
    contract, source = protected_contract
    source.write_text(content, encoding="utf-8")
    with pytest.raises(deploy.DeploymentContractError, match="secret-source-control-character"):
        _prepare(contract, source)


def test_multiline_secret_value_fails(protected_contract) -> None:
    contract, source = protected_contract
    source.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\ncontinuation\n", encoding="utf-8")
    with pytest.raises(deploy.DeploymentContractError, match="secret-source-syntax"):
        _prepare(contract, source)


def test_missing_secret_source_fails(protected_contract) -> None:
    contract, source = protected_contract
    source.unlink()
    with pytest.raises(deploy.DeploymentContractError, match="secret-source-missing"):
        _prepare(contract, source)


def test_unapproved_secret_source_fails(protected_contract, tmp_path: Path) -> None:
    contract, _source = protected_contract
    other = tmp_path / "other.env"
    other.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\n", encoding="utf-8")
    other.chmod(0o600)
    with pytest.raises(deploy.DeploymentContractError, match="unapproved-secret-source"):
        _prepare(contract, other)


def test_default_secret_source_and_safe_check_output(protected_contract, monkeypatch, capsys) -> None:
    contract, _source = protected_contract
    monkeypatch.setattr(deploy, "load_contract", lambda: contract)
    assert deploy.main(["check-secret-source"]) == 0
    captured = capsys.readouterr()
    assert "CHECK_SECRET_SOURCE=PASS" in captured.out
    assert "SOURCE_PATH_CLASS=approved-production-secret-source" in captured.out
    assert "SOURCE_REGULAR_FILE=true" in captured.out
    assert "SOURCE_SYMLINK=false" in captured.out
    assert "SOURCE_MODE=0600" in captured.out
    assert "SOURCE_REQUIRED_VARIABLES_PRESENT=true" in captured.out
    assert "SOURCE_DUPLICATE_ASSIGNMENTS=false" in captured.out
    assert "SOURCE_MALFORMED_ASSIGNMENTS=false" in captured.out
    assert "SOURCE_STRUCTURALLY_VALID=true" in captured.out
    assert "SECRET_VALUES_OUTPUT=false" in captured.out
    assert FAKE_SECRET not in captured.out + captured.err


def test_explicit_secret_source_argument_is_supported() -> None:
    args = deploy.build_parser().parse_args(
        ["check-secret-source", "--secret-source", "/etc/hermes/hermes-production.env"]
    )
    assert args.secret_source == Path("/etc/hermes/hermes-production.env")


def test_insecure_source_permissions_fail(protected_contract) -> None:
    contract, source = protected_contract
    source.chmod(0o640)
    with pytest.raises(deploy.DeploymentContractError, match="secret-source-mode"):
        _prepare(contract, source)


def test_unapproved_source_owner_fails(protected_contract) -> None:
    contract, source = protected_contract
    contract = replace(contract, approved_source_owner_uids=frozenset({deploy._effective_uid() + 1}))
    with pytest.raises(deploy.DeploymentContractError, match="secret-source-owner"):
        _prepare(contract, source)


def test_source_file_remains_unchanged(protected_contract) -> None:
    contract, source = protected_contract
    before = source.read_bytes(), source.stat().st_mode, source.stat().st_ino
    _prepare(contract, source)
    after = source.read_bytes(), source.stat().st_mode, source.stat().st_ino
    assert after == before


def test_output_symlink_is_rejected(protected_contract) -> None:
    contract, source = protected_contract
    contract.runtime_directory.mkdir(mode=0o700)
    target = contract.runtime_directory / "target"
    target.write_text("not-secret", encoding="utf-8")
    contract.secret_override.symlink_to(target)
    with pytest.raises(deploy.DeploymentContractError, match="symlink-path"):
        _prepare(contract, source)


def test_runtime_directory_symlink_is_rejected(protected_contract) -> None:
    contract, source = protected_contract
    real = contract.runtime_directory.parent / "real"
    real.mkdir(mode=0o700)
    contract.runtime_directory.symlink_to(real, target_is_directory=True)
    with pytest.raises(deploy.DeploymentContractError, match="runtime-directory-type"):
        _prepare(contract, source)


def test_runtime_directory_wrong_mode_is_rejected(protected_contract) -> None:
    contract, source = protected_contract
    contract.runtime_directory.mkdir(mode=0o755)
    with pytest.raises(deploy.DeploymentContractError, match="runtime-directory-mode"):
        _prepare(contract, source)


def test_temporary_file_collision_failure_leaves_no_partial(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract

    def fail_mkstemp(**_kwargs):
        raise FileExistsError

    monkeypatch.setattr(deploy.tempfile, "mkstemp", fail_mkstemp)
    with pytest.raises(deploy.DeploymentContractError, match="override-atomic-write"):
        _prepare(contract, source)
    assert list(contract.runtime_directory.glob(".hermes-secrets-override.*")) == []


def test_atomic_rename_failure_cleans_partial(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract

    def fail_replace(_source, _target):
        raise OSError

    monkeypatch.setattr(deploy.os, "replace", fail_replace)
    with pytest.raises(deploy.DeploymentContractError, match="override-atomic-write"):
        _prepare(contract, source)
    assert not contract.secret_override.exists()
    assert list(contract.runtime_directory.glob(".hermes-secrets-override.*")) == []


def test_cleanup_removes_exact_override_and_is_idempotent(protected_contract) -> None:
    contract, source = protected_contract
    _prepare(contract, source)
    deploy.cleanup_secret_override(contract)
    deploy.cleanup_secret_override(contract)
    assert not contract.secret_override.exists()
    assert source.exists()


def test_cleanup_rejects_symlink_override(protected_contract) -> None:
    contract, source = protected_contract
    contract.runtime_directory.mkdir(mode=0o700)
    unrelated = contract.runtime_directory / "unrelated"
    unrelated.write_text("preserve", encoding="utf-8")
    contract.secret_override.symlink_to(unrelated)
    with pytest.raises(deploy.DeploymentContractError, match="symlink-path"):
        deploy.cleanup_secret_override(contract)
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert contract.secret_override.is_symlink()
    assert source.exists()


def test_cleanup_refuses_arbitrary_path(protected_contract, tmp_path: Path) -> None:
    contract, _source = protected_contract
    arbitrary = tmp_path / "unrelated"
    arbitrary.write_text("preserve", encoding="utf-8")
    with pytest.raises(deploy.DeploymentContractError, match="cleanup-scope"):
        deploy.cleanup_secret_override(contract, arbitrary)
    assert arbitrary.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("image", ["latest", "healbite-hermes:latest", "healbite-hermes:release"])
def test_mutable_image_references_are_rejected(image: str) -> None:
    with pytest.raises(deploy.DeploymentContractError, match="mutable-image-reference"):
        deploy.validate_immutable_image(image)


def test_compose_render_is_secret_safe(protected_contract, monkeypatch, capsys) -> None:
    contract, source = protected_contract
    _prepare(contract, source)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner(calls))
    deploy.validate_compose_render(contract, IMAGE_A, REVISION)
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out
    assert FAKE_SECRET not in captured.err
    assert any(command[-2:] == ("config", "--quiet") for command in calls)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_real_compose_render_uses_only_temporary_fake_secrets(protected_contract, tmp_path: Path, capsys) -> None:
    contract, source = protected_contract
    compose_root = tmp_path / "compose-root"
    (compose_root / "deploy").mkdir(parents=True)
    base = compose_root / "docker-compose.yml"
    base.write_text(
        """
services:
  hermes-bot:
    image: ${HERMES_IMAGE:?}
    environment:
      HERMES_GIT_SHA: ${HERMES_GIT_SHA:?}
  qdrant:
    image: qdrant/qdrant:v1.15.4
""".lstrip(),
        encoding="utf-8",
    )
    production = compose_root / "deploy" / "docker-compose.production.yml"
    shutil.copy2(REPO_ROOT / "deploy" / "docker-compose.production.yml", production)
    contract = replace(
        contract,
        root=compose_root,
        base_compose=base,
        production_override=production,
    )
    _prepare(contract, source)
    deploy.validate_compose_render(contract, IMAGE_A, REVISION)
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out + captured.err


def test_compose_render_requires_target_service(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    _prepare(contract, source)
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner([], services="qdrant\n"))
    with pytest.raises(deploy.DeploymentContractError, match="target-service-missing"):
        deploy.validate_compose_render(contract, IMAGE_A, REVISION)


def test_invalid_compose_render_fails_without_error_body(protected_contract, monkeypatch, capsys) -> None:
    contract, source = protected_contract
    _prepare(contract, source)

    def failing(argv, **_kwargs):
        return _completed(argv, returncode=1, stderr=FAKE_SECRET)

    monkeypatch.setattr(deploy, "_run", failing)
    with pytest.raises(deploy.DeploymentContractError, match="compose-render"):
        deploy.validate_compose_render(contract, IMAGE_A, REVISION)
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out + captured.err


def test_plan_performs_no_deployment_and_cleans_override(protected_contract, monkeypatch, capsys) -> None:
    contract, source = protected_contract
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner(calls))
    deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=REVISION)
    captured = capsys.readouterr()
    assert "DEPLOYMENT_ACTIONS_PERFORMED=false" in captured.out
    assert FAKE_SECRET not in captured.out + captured.err
    assert not contract.secret_override.exists()
    assert not contract.runtime_directory.exists()
    assert any("hermes-production-plan-" in part for command in calls for part in command)
    assert not any(str(contract.secret_override) in command for command in calls)
    assert not any("up" in command or "build" in command or "pull" in command for command in calls)


def test_rollback_plan_uses_distinct_local_immutable_image(protected_contract, monkeypatch, capsys) -> None:
    contract, source = protected_contract
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    _allow_mocked_rollback_revision(monkeypatch)
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner(calls))
    deploy.plan_operation(
        contract,
        source=source,
        image=IMAGE_A,
        revision=REVISION,
        rollback_from=IMAGE_B,
    )
    captured = capsys.readouterr()
    assert "PLAN=ROLLBACK" in captured.out
    assert contract.project_name in captured.out
    assert contract.target_service in captured.out
    assert FAKE_SECRET not in captured.out + captured.err
    assert not contract.secret_override.exists()
    assert not contract.runtime_directory.exists()


def test_missing_rollback_image_fails(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract

    def missing(argv, **_kwargs):
        return _completed(argv, returncode=1)

    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    _allow_mocked_rollback_revision(monkeypatch)
    monkeypatch.setattr(deploy, "_run", missing)
    with pytest.raises(deploy.DeploymentContractError, match="local-image-missing"):
        deploy.plan_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            rollback_from=IMAGE_B,
        )


def test_plan_render_failure_cleans_ephemeral_override(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)

    def fail_render(argv, **_kwargs):
        command = tuple(str(item) for item in argv)
        calls.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            return _completed(argv, stdout=json.dumps([_image_record(IMAGE_A)]))
        if command[-2:] == ("config", "--quiet"):
            return _completed(argv, returncode=1, stderr=FAKE_SECRET)
        return _completed(argv)

    monkeypatch.setattr(deploy, "_run", fail_render)
    with pytest.raises(deploy.DeploymentContractError, match="compose-render"):
        deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=REVISION)
    assert not contract.runtime_directory.exists()
    temporary_paths = [part for command in calls for part in command if "hermes-production-plan-" in part]
    assert temporary_paths
    assert all(not Path(part).exists() for part in temporary_paths)


def test_plan_leaves_legacy_tmp_path_untouched(protected_contract, monkeypatch, tmp_path: Path) -> None:
    contract, source = protected_contract
    legacy = tmp_path / "hermes-secrets-override.yml"
    legacy.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner([]))
    deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=REVISION)
    assert legacy.read_text(encoding="utf-8") == "preserve"


def test_matching_image_revision_label_passes(protected_contract, monkeypatch) -> None:
    contract, _source = protected_contract
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner([]))
    inspected = deploy.inspect_local_image(contract, IMAGE_A, expected_revision=REVISION)
    assert inspected == deploy.InspectedImage(image_id=IMAGE_A, revision=REVISION)


@pytest.mark.parametrize(
    ("label", "error"),
    [
        (None, "image-revision-label-missing"),
        ("", "image-revision-label-missing"),
        ("not-a-sha", "image-revision-label-invalid"),
        ("a" * 12, "image-revision-label-invalid"),
        (OTHER_REVISION, "image-revision-mismatch"),
    ],
)
def test_invalid_image_revision_labels_fail_closed(protected_contract, monkeypatch, label, error) -> None:
    contract, _source = protected_contract
    monkeypatch.setattr(
        deploy,
        "_run",
        _safe_docker_runner([], revisions={IMAGE_A: label}),
    )
    with pytest.raises(deploy.DeploymentContractError, match=error):
        deploy.inspect_local_image(contract, IMAGE_A, expected_revision=REVISION)


def test_multiple_image_inspect_records_are_denied(protected_contract, monkeypatch) -> None:
    contract, _source = protected_contract

    def ambiguous(argv, **_kwargs):
        return _completed(argv, stdout=json.dumps([_image_record(IMAGE_A), _image_record(IMAGE_B)]))

    monkeypatch.setattr(deploy, "_run", ambiguous)
    with pytest.raises(deploy.DeploymentContractError, match="image-inspect-ambiguous"):
        deploy.inspect_local_image(contract, IMAGE_A, expected_revision=REVISION)


def test_nonexistent_image_is_denied(protected_contract, monkeypatch) -> None:
    contract, _source = protected_contract
    monkeypatch.setattr(deploy, "_run", lambda argv, **_kwargs: _completed(argv, returncode=1))
    with pytest.raises(deploy.DeploymentContractError, match="local-image-missing"):
        deploy.inspect_local_image(contract, IMAGE_A, expected_revision=REVISION)


def test_plan_image_mismatch_fails_without_canonical_runtime_write(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    monkeypatch.setattr(
        deploy,
        "_run",
        _safe_docker_runner([], revisions={IMAGE_A: OTHER_REVISION}),
    )
    with pytest.raises(deploy.DeploymentContractError, match="image-revision-mismatch"):
        deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=REVISION)
    assert not contract.runtime_directory.exists()


@pytest.mark.parametrize(("rollback", "error"), [(False, "dirty-worktree"), (True, "head-mismatch")])
def test_execute_repository_failure_precedes_secret_image_and_runtime(
    protected_contract,
    monkeypatch,
    rollback: bool,
    error: str,
) -> None:
    contract, source = protected_contract
    events: list[str] = []

    def reject_repository(*_args):
        events.append("repository")
        raise deploy.DeploymentContractError(error)

    monkeypatch.setattr(deploy, "validate_repository", reject_repository)
    monkeypatch.setattr(deploy, "inspect_local_image", lambda *_args, **_kwargs: events.append("image"))
    monkeypatch.setattr(deploy, "read_required_secrets", lambda *_args: events.append("secret"))
    with pytest.raises(deploy.DeploymentContractError, match=error):
        deploy.execute_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            confirmation=deploy.ROLLBACK_CONFIRMATION if rollback else deploy.DEPLOY_CONFIRMATION,
            rollback=rollback,
            current_image=IMAGE_B if rollback else None,
        )
    assert events == ["repository"]
    assert not contract.runtime_directory.exists()


@pytest.mark.parametrize("revision", ["c" * 12, "main", "v1.0.0", "C" * 40])
def test_execute_denies_non_exact_revision_before_other_gates(
    protected_contract,
    monkeypatch,
    revision: str,
) -> None:
    contract, source = protected_contract
    monkeypatch.setattr(deploy, "_run", lambda *_args, **_kwargs: pytest.fail("command must not run"))
    with pytest.raises(deploy.DeploymentContractError, match="expected-sha"):
        deploy.execute_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=revision,
            confirmation=deploy.DEPLOY_CONFIRMATION,
            rollback=False,
        )
    assert not contract.runtime_directory.exists()


@pytest.mark.parametrize("rollback", [False, True])
def test_execute_image_mismatch_precedes_secret_read_and_runtime(
    protected_contract,
    monkeypatch,
    rollback: bool,
) -> None:
    contract, source = protected_contract
    secret_reads = 0
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    if rollback:
        _allow_mocked_rollback_revision(monkeypatch)
    monkeypatch.setattr(
        deploy,
        "_run",
        _safe_docker_runner([], revisions={IMAGE_A: OTHER_REVISION}),
    )

    def count_secret_reads(*_args):
        nonlocal secret_reads
        secret_reads += 1
        return {}

    monkeypatch.setattr(deploy, "read_required_secrets", count_secret_reads)
    with pytest.raises(deploy.DeploymentContractError, match="image-revision-mismatch"):
        deploy.execute_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            confirmation=deploy.ROLLBACK_CONFIRMATION if rollback else deploy.DEPLOY_CONFIRMATION,
            rollback=rollback,
            current_image=IMAGE_B if rollback else None,
        )
    assert secret_reads == 0
    assert not contract.runtime_directory.exists()


def test_execute_requires_explicit_confirmation_before_docker(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner(calls))
    with pytest.raises(deploy.DeploymentContractError, match="explicit-confirmation-required"):
        deploy.execute_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            confirmation="NO",
            rollback=False,
        )
    assert calls == []


def test_execute_orders_all_gates_and_deploys_inspected_image_id(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    events: list[str] = []
    original_read = deploy.read_required_secrets
    original_write = deploy._write_secret_override

    def repository_gate(*_args):
        events.append("repository")

    def secret_gate(*args):
        events.append("secret")
        return original_read(*args)

    def tracked_write(target_contract, secrets):
        events.append("canonical_override" if target_contract is contract else "temporary_override")
        return original_write(target_contract, secrets)

    def runner(argv, **kwargs):
        command = tuple(str(item) for item in argv)
        if command[:3] == ("docker", "image", "inspect"):
            events.append("image")
            return _completed(argv, stdout=json.dumps([_image_record(IMAGE_A)]))
        if command[-2:] == ("config", "--quiet"):
            events.append("compose_render")
            return _completed(argv)
        if command[-2:] == ("config", "--services"):
            return _completed(argv, stdout="hermes-bot\nqdrant\n")
        if "up" in command:
            events.append("docker_mutation")
            assert kwargs["env"]["HERMES_IMAGE"] == IMAGE_A
            assert IMAGE_DIGEST not in kwargs["env"].values()
            return _completed(argv)
        if command[:2] == ("docker", "inspect"):
            return _completed(argv, stdout=f"running 0 {IMAGE_A}\n")
        return _completed(argv)

    monkeypatch.setattr(deploy, "validate_repository", repository_gate)
    monkeypatch.setattr(deploy, "read_required_secrets", secret_gate)
    monkeypatch.setattr(deploy, "_write_secret_override", tracked_write)
    monkeypatch.setattr(deploy, "_run", runner)
    deploy.execute_operation(
        contract,
        source=source,
        image=IMAGE_DIGEST,
        revision=REVISION,
        confirmation=deploy.DEPLOY_CONFIRMATION,
        rollback=False,
    )
    assert events == [
        "repository",
        "image",
        "secret",
        "temporary_override",
        "compose_render",
        "canonical_override",
        "docker_mutation",
    ]
    assert not contract.secret_override.exists()


def test_cleanup_failure_does_not_mask_primary_execute_failure(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    original_cleanup = deploy.cleanup_secret_override
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)

    def cleanup(target_contract, requested_path=None):
        if target_contract is contract:
            raise deploy.DeploymentContractError("cleanup-failed")
        return original_cleanup(target_contract, requested_path)

    def runner(argv, **_kwargs):
        command = tuple(str(item) for item in argv)
        if command[:3] == ("docker", "image", "inspect"):
            return _completed(argv, stdout=json.dumps([_image_record(IMAGE_A)]))
        if command[-2:] == ("config", "--services"):
            return _completed(argv, stdout="hermes-bot\n")
        if "up" in command:
            return _completed(argv, returncode=1)
        return _completed(argv)

    monkeypatch.setattr(deploy, "cleanup_secret_override", cleanup)
    monkeypatch.setattr(deploy, "_run", runner)
    with pytest.raises(deploy.DeploymentContractError, match="compose-up"):
        deploy.execute_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            confirmation=deploy.DEPLOY_CONFIRMATION,
            rollback=False,
        )


def test_execute_rollback_deploys_inspected_previous_image_id(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    deployed_images: list[str] = []
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    _allow_mocked_rollback_revision(monkeypatch)

    def runner(argv, **kwargs):
        command = tuple(str(item) for item in argv)
        if command[:3] == ("docker", "image", "inspect"):
            image_id = IMAGE_A if command[-1] == IMAGE_DIGEST else IMAGE_B
            return _completed(argv, stdout=json.dumps([_image_record(image_id)]))
        if command[-2:] == ("config", "--services"):
            return _completed(argv, stdout="hermes-bot\n")
        if "up" in command:
            deployed_images.append(kwargs["env"]["HERMES_IMAGE"])
            return _completed(argv)
        if command[:2] == ("docker", "inspect"):
            return _completed(argv, stdout=f"running 0 {IMAGE_A}\n")
        return _completed(argv)

    monkeypatch.setattr(deploy, "_run", runner)
    deploy.execute_operation(
        contract,
        source=source,
        image=IMAGE_DIGEST,
        revision=REVISION,
        confirmation=deploy.ROLLBACK_CONFIRMATION,
        rollback=True,
        current_image=IMAGE_B,
    )
    assert deployed_images == [IMAGE_A]
    assert not contract.secret_override.exists()


def test_execute_rollback_requires_distinct_current_image(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    _allow_mocked_rollback_revision(monkeypatch)
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner(calls))
    with pytest.raises(deploy.DeploymentContractError, match="rollback-image-not-distinct"):
        deploy.execute_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            confirmation=deploy.ROLLBACK_CONFIRMATION,
            rollback=True,
            current_image=IMAGE_A,
        )
    assert not contract.secret_override.exists()



def _make_child_commit(contract: deploy.DeploymentContract) -> str:
    marker = contract.root / "child.txt"
    marker.write_text("child", encoding="utf-8")
    _git("add", ".", cwd=contract.root)
    _git("commit", "-qm", "child", cwd=contract.root)
    head = _git("rev-parse", "HEAD", cwd=contract.root)
    _git("update-ref", contract.allowed_revision_ref, head, cwd=contract.root)
    return head


def _production_like_contract(repository_fixture, protected_contract) -> tuple[deploy.DeploymentContract, Path, str, str]:
    repo_contract, previous = repository_fixture
    protected, source = protected_contract
    source_head = _make_child_commit(repo_contract)
    contract = replace(
        repo_contract,
        runtime_directory=protected.runtime_directory,
        secret_override=protected.secret_override,
        approved_secret_source=source,
        approved_source_owner_uids=protected.approved_source_owner_uids,
    )
    return contract, source, previous, source_head


def test_rollback_plan_accepts_previous_ancestor_revision_distinct_from_source_head(
    repository_fixture,
    protected_contract,
    monkeypatch,
    capsys,
) -> None:
    contract, source, rollback_revision, source_head = _production_like_contract(repository_fixture, protected_contract)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        deploy,
        "_run",
        _runner_with_real_git(calls, revisions={IMAGE_A: rollback_revision, IMAGE_B: source_head}),
    )
    deploy.plan_operation(
        contract,
        source=source,
        image=IMAGE_A,
        revision=rollback_revision,
        rollback_from=IMAGE_B,
    )
    captured = capsys.readouterr()
    assert "PLAN=ROLLBACK" in captured.out
    assert f"SOURCE_HEAD_REVISION={source_head}" in captured.out
    assert f"ROLLBACK_TARGET_REVISION={rollback_revision}" in captured.out
    assert f"ROLLBACK_IMAGE_REVISION_LABEL={rollback_revision}" in captured.out
    assert "ROLLBACK_REVISION_ANCESTOR_OF_SOURCE=true" in captured.out
    assert "DEPLOYMENT_ACTIONS_PERFORMED=false" in captured.out
    assert FAKE_SECRET not in captured.out + captured.err
    assert not contract.secret_override.exists()
    assert not contract.runtime_directory.exists()
    assert not any("up" in command or "build" in command or "pull" in command for command in calls)


def test_rollback_plan_denies_label_substitution_with_source_head(
    repository_fixture,
    protected_contract,
    monkeypatch,
) -> None:
    contract, source, rollback_revision, source_head = _production_like_contract(repository_fixture, protected_contract)
    monkeypatch.setattr(
        deploy,
        "_run",
        _runner_with_real_git([], revisions={IMAGE_A: source_head, IMAGE_B: source_head}),
    )
    with pytest.raises(deploy.DeploymentContractError, match="image-revision-mismatch"):
        deploy.plan_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=rollback_revision,
            rollback_from=IMAGE_B,
        )
    assert not contract.runtime_directory.exists()


@pytest.mark.parametrize("revision", ["c" * 12, "main", "C" * 40, "not-a-sha"])
def test_rollback_plan_denies_non_full_lowercase_sha(
    repository_fixture,
    protected_contract,
    monkeypatch,
    revision: str,
) -> None:
    contract, source, _rollback_revision, source_head = _production_like_contract(repository_fixture, protected_contract)
    monkeypatch.setattr(
        deploy,
        "_run",
        _runner_with_real_git([], revisions={IMAGE_A: revision, IMAGE_B: source_head}),
    )
    with pytest.raises(deploy.DeploymentContractError, match="revision"):
        deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=revision, rollback_from=IMAGE_B)
    assert not contract.runtime_directory.exists()


def test_rollback_plan_denies_unknown_commit(repository_fixture, protected_contract, monkeypatch) -> None:
    contract, source, _rollback_revision, source_head = _production_like_contract(repository_fixture, protected_contract)
    unknown = "e" * 40
    monkeypatch.setattr(
        deploy,
        "_run",
        _runner_with_real_git([], revisions={IMAGE_A: unknown, IMAGE_B: source_head}),
    )
    with pytest.raises(deploy.DeploymentContractError, match="rollback-revision-not-commit"):
        deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=unknown, rollback_from=IMAGE_B)
    assert not contract.runtime_directory.exists()


def test_rollback_plan_denies_non_ancestor_commit(repository_fixture, protected_contract, monkeypatch) -> None:
    contract, source, _rollback_revision, source_head = _production_like_contract(repository_fixture, protected_contract)
    _git("checkout", "--orphan", "side", cwd=contract.root)
    (contract.root / "side.txt").write_text("side", encoding="utf-8")
    _git("add", ".", cwd=contract.root)
    _git("commit", "-qm", "side", cwd=contract.root)
    side = _git("rev-parse", "HEAD", cwd=contract.root)
    _git("checkout", "-q", source_head, cwd=contract.root)
    monkeypatch.setattr(
        deploy,
        "_run",
        _runner_with_real_git([], revisions={IMAGE_A: side, IMAGE_B: source_head}),
    )
    with pytest.raises(deploy.DeploymentContractError, match="rollback-revision-not-ancestor"):
        deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=side, rollback_from=IMAGE_B)
    assert not contract.runtime_directory.exists()


def test_rollback_plan_denies_source_head_mismatch(repository_fixture, protected_contract, monkeypatch) -> None:
    contract, source, rollback_revision, _source_head = _production_like_contract(repository_fixture, protected_contract)
    monkeypatch.setattr(deploy, "current_source_head_revision", lambda _contract: rollback_revision)
    monkeypatch.setattr(
        deploy,
        "_run",
        _runner_with_real_git([], revisions={IMAGE_A: rollback_revision, IMAGE_B: OTHER_REVISION}),
    )
    with pytest.raises(deploy.DeploymentContractError, match="head-mismatch"):
        deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=rollback_revision, rollback_from=IMAGE_B)
    assert not contract.runtime_directory.exists()

def test_feature_flags_remain_disabled() -> None:
    contract = deploy.load_contract()
    override = json.loads(contract.production_override.read_text(encoding="utf-8"))
    assert override["services"]["hermes-bot"]["environment"] == {
        "HEALBITE_SHOPPING_LIST_ENABLED": "false",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": "",
    }


def test_canonical_tooling_has_no_active_legacy_paths() -> None:
    paths = (
        REPO_ROOT / "deploy" / "hermes-production.json",
        REPO_ROOT / "deploy" / "docker-compose.production.yml",
        REPO_ROOT / "scripts" / "hermes_production_deploy.py",
        REPO_ROOT / "scripts" / "hermes_production_deploy.sh",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "/tmp/hermes-secrets-override.yml" not in combined
    assert "healbite-s71v2-r6-deploy" not in combined


def test_wrapper_has_no_implicit_deployment() -> None:
    wrapper = (REPO_ROOT / "scripts" / "hermes_production_deploy.sh").read_text(encoding="utf-8")
    assert "docker compose" not in wrapper
    assert " up " not in wrapper
    assert "set -x" not in wrapper


def test_runbook_documents_read_only_plans_and_revision_binding() -> None:
    runbook = (REPO_ROOT / "docs" / "runbooks" / "hermes-production-deployment.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(runbook.split())
    assert "org.opencontainers.image.revision" in runbook
    assert "never creates `/run/hermes`" in runbook
    assert "successful plan is not authorization" in runbook
    assert "exact inspected immutable image ID" in normalized
