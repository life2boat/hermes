from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.telegram import TelegramAdapter
from gateway.run import _normalize_empty_agent_response, _sanitize_gateway_final_response
from gateway.session import SessionSource
from gateway.slash_access import policy_for_source


SAFE_PROVIDER_REPLY = "Сервис временно перегружен, попробуйте через минуту."


def _memory_stats_message(*, user_id: int = 123456789, chat_id: int = 100, chat_type: str = "private"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id),
    )


def test_gateway_provider_auth_error_is_masked_before_telegram_send(caplog):
    agent_result = {
        "failed": True,
        "error": "Provider authentication failed. Check configured credentials.",
        "api_calls": 1,
    }

    with caplog.at_level("WARNING", logger="gateway.run"):
        response = _normalize_empty_agent_response(agent_result, "", history_len=0)
        safe = _sanitize_gateway_final_response(Platform.TELEGRAM, response)

    assert safe == SAFE_PROVIDER_REPLY
    assert "Provider authentication failed" not in safe
    assert "configured credentials" not in safe
    assert "Provider authentication failed" in caplog.text


def test_policy_for_source_marks_telegram_admin_from_allow_admin_from():
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="fake-token",
                extra={"allow_admin_from": ["123456789"]},
            )
        }
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="100",
        chat_type="dm",
        user_id="123456789",
    )

    policy = policy_for_source(config, source)

    assert policy.is_admin("123456789") is True


def test_memory_stats_admin_reads_allow_admin_from_from_physical_config_file(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        """
platforms:
  telegram:
    extra:
      allow_admin_from:
        - 123456789
      group_allow_admin_from:
        - 987654321
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(tmp_path))

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={}))
    empty_runtime_config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token", extra={})}
    )

    with patch("gateway.config.load_gateway_config", return_value=empty_runtime_config):
        assert adapter._memory_stats_is_admin(_memory_stats_message()) is True
