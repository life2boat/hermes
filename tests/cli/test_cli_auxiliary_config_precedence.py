"""Real-path precedence tests for the CLI auxiliary config bridge."""

from __future__ import annotations

import logging

import pytest
import yaml

import cli


TASK_CASES = (
    ("vision", "gemini", "AUXILIARY_VISION_API_KEY"),
    ("web_extract", "qwen", "AUXILIARY_WEB_EXTRACT_API_KEY"),
    ("approval", "deepseek", "AUXILIARY_APPROVAL_API_KEY"),
)


def _write_config(tmp_path, auxiliary: dict) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"auxiliary": auxiliary}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(("task", "provider", "env_key"), TASK_CASES)
def test_real_cli_preserves_existing_process_credential(
    tmp_path,
    monkeypatch,
    task,
    provider,
    env_key,
):
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.setenv(env_key, "synthetic-process-value")
    _write_config(
        tmp_path,
        {task: {"provider": provider, "api_key": "synthetic-config-value"}},
    )

    cli.load_cli_config()

    assert cli.os.environ[env_key] == "synthetic-process-value"


@pytest.mark.parametrize(("task", "provider", "env_key"), TASK_CASES)
def test_real_cli_fills_missing_process_credential(
    tmp_path,
    monkeypatch,
    task,
    provider,
    env_key,
):
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.delenv(env_key, raising=False)
    _write_config(
        tmp_path,
        {task: {"provider": provider, "api_key": "synthetic-config-value"}},
    )

    cli.load_cli_config()

    assert cli.os.environ[env_key] == "synthetic-config-value"


@pytest.mark.parametrize("config_value", ("", "   ", None))
def test_real_cli_ignores_empty_or_missing_config_credential(
    tmp_path,
    monkeypatch,
    config_value,
):
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.delenv("AUXILIARY_VISION_API_KEY", raising=False)
    _write_config(tmp_path, {"vision": {"api_key": config_value}})

    cli.load_cli_config()

    assert "AUXILIARY_VISION_API_KEY" not in cli.os.environ


def test_real_cli_preserves_existing_empty_process_credential(tmp_path, monkeypatch):
    """Key presence is authoritative, matching canonical dotenv semantics."""
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.setenv("AUXILIARY_VISION_API_KEY", "")
    _write_config(tmp_path, {"vision": {"api_key": "synthetic-config-value"}})

    cli.load_cli_config()

    assert cli.os.environ["AUXILIARY_VISION_API_KEY"] == ""


def test_real_cli_bridge_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.delenv("AUXILIARY_VISION_API_KEY", raising=False)
    _write_config(tmp_path, {"vision": {"api_key": "synthetic-first-value"}})
    cli.load_cli_config()

    _write_config(tmp_path, {"vision": {"api_key": "synthetic-second-value"}})
    cli.load_cli_config()

    assert cli.os.environ["AUXILIARY_VISION_API_KEY"] == "synthetic-first-value"


def test_real_cli_handles_multiple_auxiliary_tasks_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    for _task, _provider, env_key in TASK_CASES:
        monkeypatch.delenv(env_key, raising=False)
    _write_config(
        tmp_path,
        {
            task: {
                "provider": provider,
                "api_key": f"synthetic-{task}-value",
            }
            for task, provider, _env_key in TASK_CASES
        },
    )

    cli.load_cli_config()

    for task, _provider, env_key in TASK_CASES:
        assert cli.os.environ[env_key] == f"synthetic-{task}-value"


def test_real_cli_preserves_non_secret_config_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.setenv("AUXILIARY_VISION_PROVIDER", "process-provider")
    monkeypatch.setenv("AUXILIARY_VISION_MODEL", "process-model")
    monkeypatch.setenv("AUXILIARY_VISION_BASE_URL", "https://process.invalid")
    _write_config(
        tmp_path,
        {
            "vision": {
                "provider": "config-provider",
                "model": "config-model",
                "base_url": "https://config.invalid",
            }
        },
    )

    cli.load_cli_config()

    assert cli.os.environ["AUXILIARY_VISION_PROVIDER"] == "config-provider"
    assert cli.os.environ["AUXILIARY_VISION_MODEL"] == "config-model"
    assert cli.os.environ["AUXILIARY_VISION_BASE_URL"] == "https://config.invalid"


def test_real_cli_does_not_log_credential_material(
    tmp_path, monkeypatch, caplog, capsys
):
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.setenv("AUXILIARY_VISION_API_KEY", "synthetic-process-value")
    _write_config(tmp_path, {"vision": {"api_key": "synthetic-config-value"}})

    with caplog.at_level(logging.DEBUG):
        cli.load_cli_config()
    captured = capsys.readouterr()

    combined = captured.out + captured.err + caplog.text
    assert "synthetic-process-value" not in combined
    assert "synthetic-config-value" not in combined
