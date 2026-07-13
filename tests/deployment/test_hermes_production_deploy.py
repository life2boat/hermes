from __future__ import annotations

import json
import os
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
REVISION = "c" * 40


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
        approved_source_owner_uids=frozenset({os.geteuid()}),
    )
    return contract, source


def _prepare(contract: deploy.DeploymentContract, source: Path) -> None:
    deploy.prepare_secret_override(contract, source)


def _completed(argv, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _safe_docker_runner(calls: list[tuple[str, ...]], *, services: str = "hermes-bot\nqdrant\n"):
    def runner(argv, **_kwargs):
        command = tuple(str(item) for item in argv)
        calls.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            image = command[-1]
            return _completed(argv, stdout=(IMAGE_A if image == IMAGE_A else IMAGE_B) + "\n")
        if command[-2:] == ("config", "--services"):
            return _completed(argv, stdout=services, stderr=FAKE_SECRET)
        return _completed(argv, stderr=FAKE_SECRET)

    return runner


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout.strip()


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
    return deploy.load_contract(root), _git("rev-parse", "HEAD", cwd=root)


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
    assert FAKE_SECRET not in text


def test_repository_check_passes_on_clean_exact_head(repository_fixture) -> None:
    contract, head = repository_fixture
    deploy.validate_repository(contract, head)


def test_repository_check_rejects_dirty_worktree(repository_fixture) -> None:
    contract, head = repository_fixture
    (contract.root / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(deploy.DeploymentContractError, match="dirty-worktree"):
        deploy.validate_repository(contract, head)


def test_repository_check_rejects_legacy_worktree_reference(repository_fixture) -> None:
    contract, _head = repository_fixture
    text = contract.production_override.read_text(encoding="utf-8")
    contract.production_override.write_text(text + "\n# healbite-s71v2-r6-deploy\n", encoding="utf-8")
    _git("add", ".", cwd=contract.root)
    _git("commit", "-qm", "bad reference", cwd=contract.root)
    head = _git("rev-parse", "HEAD", cwd=contract.root)
    with pytest.raises(deploy.DeploymentContractError, match="legacy-reference"):
        deploy.validate_repository(contract, head)


def test_repository_check_rejects_tmp_override_reference(repository_fixture) -> None:
    contract, _head = repository_fixture
    text = contract.production_override.read_text(encoding="utf-8")
    contract.production_override.write_text(text + "\n# /tmp/hermes-secrets-override.yml\n", encoding="utf-8")
    _git("add", ".", cwd=contract.root)
    _git("commit", "-qm", "bad reference", cwd=contract.root)
    head = _git("rev-parse", "HEAD", cwd=contract.root)
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
    contract = replace(contract, approved_source_owner_uids=frozenset({os.geteuid() + 1}))
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
    monkeypatch.setattr(deploy, "_run", _safe_docker_runner(calls))
    deploy.plan_operation(contract, source=source, image=IMAGE_A, revision=REVISION)
    captured = capsys.readouterr()
    assert "DEPLOYMENT_ACTIONS_PERFORMED=false" in captured.out
    assert FAKE_SECRET not in captured.out + captured.err
    assert not contract.secret_override.exists()
    assert not any("up" in command or "build" in command or "pull" in command for command in calls)


def test_rollback_plan_uses_distinct_local_immutable_image(protected_contract, monkeypatch, capsys) -> None:
    contract, source = protected_contract
    calls: list[tuple[str, ...]] = []
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


def test_missing_rollback_image_fails(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract

    def missing(argv, **_kwargs):
        return _completed(argv, returncode=1)

    monkeypatch.setattr(deploy, "_run", missing)
    with pytest.raises(deploy.DeploymentContractError, match="local-image-missing"):
        deploy.plan_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            rollback_from=IMAGE_B,
        )


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


def test_execute_rollback_requires_distinct_current_image(protected_contract, monkeypatch) -> None:
    contract, source = protected_contract
    calls: list[tuple[str, ...]] = []
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
