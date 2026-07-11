import os
import subprocess
import sys

from hermes_cli.env_loader import load_hermes_dotenv


def test_process_env_takes_precedence_over_user_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_BASE_URL=https://new.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_BASE_URL") == "https://old.example/v1"


def test_process_env_takes_precedence_over_project_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://old.example/v1"


def test_project_env_is_sanitized_before_loading(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=0123456789:test"
        "ANTHROPIC_API_KEY=sk-ant-test123\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("TELEGRAM_BOT_TOKEN") == "0123456789:test"
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-ant-test123"


def test_user_env_takes_precedence_over_project_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    user_env = home / ".env"
    project_env = tmp_path / ".env"
    user_env.write_text("OPENAI_BASE_URL=https://user.example/v1\n", encoding="utf-8")
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\nOPENAI_API_KEY=project-key\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [user_env, project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://user.example/v1"
    assert os.getenv("OPENAI_API_KEY") == "project-key"


def test_null_bytes_in_user_env_are_stripped(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # Null bytes can be introduced when copy-pasting API keys.
    env_file.write_text("GLM_API_KEY=abc\x00\x00\nOPENAI_API_KEY=sk-123\n", encoding="utf-8")

    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("GLM_API_KEY") == "abc"
    assert os.getenv("OPENAI_API_KEY") == "sk-123"


def test_main_import_preserves_process_env_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "OPENAI_BASE_URL=https://new.example/v1\nHERMES_INFERENCE_PROVIDER=custom\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; import hermes_cli.main; "
                "assert os.getenv('OPENAI_BASE_URL') == 'https://old.example/v1'; "
                "assert os.getenv('HERMES_INFERENCE_PROVIDER') == 'openrouter'"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr


def test_user_env_fills_missing_process_value(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_BASE_URL=https://user.example/v1\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("OPENAI_BASE_URL") == "https://user.example/v1"


def test_empty_dotenv_value_does_not_replace_process_secret(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "container-secret")

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("GEMINI_API_KEY") == "container-secret"


def test_repeated_load_is_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_BASE_URL=https://first.example/v1\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    load_hermes_dotenv(hermes_home=home)
    env_file.write_text("OPENAI_BASE_URL=https://second.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("OPENAI_BASE_URL") == "https://first.example/v1"


def test_secret_and_non_secret_process_values_are_preserved(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "GEMINI_API_KEY=dotenv-secret\nHEALBITE_MODE=dotenv-mode\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "container-secret")
    monkeypatch.setenv("HEALBITE_MODE", "container-mode")

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("GEMINI_API_KEY") == "container-secret"
    assert os.getenv("HEALBITE_MODE") == "container-mode"


def test_missing_dotenv_files_are_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://container.example/v1")

    loaded = load_hermes_dotenv(
        hermes_home=tmp_path / "missing-home",
        project_env=tmp_path / "missing-project.env",
    )

    assert loaded == []
    assert os.getenv("OPENAI_BASE_URL") == "https://container.example/v1"


def test_malformed_dotenv_line_does_not_override_process_value(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "this is not an assignment\nOPENAI_BASE_URL=https://dotenv.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://container.example/v1")

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("OPENAI_BASE_URL") == "https://container.example/v1"
