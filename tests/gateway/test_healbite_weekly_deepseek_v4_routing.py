from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from agent.auxiliary_client import (
    ExternalRequestTelemetry,
    WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
    _resolve_task_provider_model,
    call_llm,
)
from hermes_cli.config import DEFAULT_CONFIG
from providers import get_provider_profile


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"entries":[]}'))]
        )


def _client(recorder: _Recorder):
    return SimpleNamespace(
        base_url="https://api.deepseek.com/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=recorder.create)),
    )


def test_weekly_model_override_is_task_scoped():
    config = deepcopy(DEFAULT_CONFIG)

    with patch("hermes_cli.config.load_config", return_value=config):
        weekly = _resolve_task_provider_model(task="weekly_menu_generation")
        vision = _resolve_task_provider_model(task="vision")
        compression = _resolve_task_provider_model(task="compression")

    assert weekly[:2] == ("deepseek", "deepseek-v4-flash")
    assert weekly[1] != "deepseek-chat"
    assert vision[:2] == ("auto", None)
    assert compression[:2] == ("auto", None)
    assert get_provider_profile("deepseek").default_aux_model == "deepseek-chat"


def test_weekly_call_uses_v4_flash_json_object_without_retry_or_fallback():
    recorder = _Recorder()
    telemetry = ExternalRequestTelemetry()
    config = deepcopy(DEFAULT_CONFIG)
    client = _client(recorder)

    with (
        patch("hermes_cli.config.load_config", return_value=config),
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(client, "deepseek-v4-flash"),
        ) as get_client,
    ):
        response = call_llm(
            task="weekly_menu_generation",
            messages=[
                {
                    "role": "system",
                    "content": 'Return only a JSON object like {"entries": []}.',
                },
                {"role": "user", "content": '{"week_start":"2030-01-07"}'},
            ],
            temperature=0.2,
            timeout=45.0,
            call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
            request_telemetry=telemetry,
        )

    assert response.choices[0].message.content == '{"entries": []}'
    assert len(recorder.calls) == 1
    assert get_client.call_args.args[:2] == ("deepseek", "deepseek-v4-flash")
    kwargs = recorder.calls[0]
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["extra_body"] == {"response_format": {"type": "json_object"}}
    assert kwargs["temperature"] == 0.2
    assert kwargs["timeout"] == 45.0
    assert "tools" not in kwargs
    assert "max_tokens" not in kwargs
    assert telemetry.external_request_attempts == 1
    assert telemetry.external_request_budget == 1
    assert telemetry.retry_performed is False
    assert telemetry.fallback_performed is False


def test_unrelated_auxiliary_defaults_are_unchanged():
    assert DEFAULT_CONFIG["auxiliary"]["vision"] == {
        "provider": "auto",
        "model": "",
        "base_url": "",
        "api_key": "",
        "api_key_env": "",
        "timeout": 120,
        "extra_body": {},
        "download_timeout": 30,
    }
    compression = DEFAULT_CONFIG["auxiliary"]["compression"]
    assert compression["provider"] == "auto"
    assert compression["model"] == ""
    assert compression["extra_body"] == {}
