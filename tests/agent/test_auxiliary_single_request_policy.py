from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.auxiliary_client import (
    ExternalRequestTelemetry,
    WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
    call_llm,
)


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _CreateRecorder:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(content: str = '{"ok": true}'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _client(recorder: _CreateRecorder):
    return SimpleNamespace(
        base_url="https://provider.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=recorder.create)),
    )


def _patch_resolution(recorder: _CreateRecorder):
    return patch.multiple(
        "agent.auxiliary_client",
        _resolve_task_provider_model=lambda *args, **kwargs: ("custom", "model-a", None, "key", None),
        _get_cached_client=lambda *args, **kwargs: (_client(recorder), "model-a"),
        _get_task_extra_body=lambda task: {},
        _get_task_timeout=lambda task: 1.0,
    )


def test_strict_policy_disables_transient_retry_for_weekly_path():
    recorder = _CreateRecorder([_StatusError("server unavailable", 503), _response()])
    telemetry = ExternalRequestTelemetry()

    with _patch_resolution(recorder), patch("agent.auxiliary_client.time.sleep"):
        with pytest.raises(_StatusError):
            call_llm(
                task="weekly_menu_generation",
                messages=[{"role": "user", "content": "synthetic"}],
                call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
                request_telemetry=telemetry,
            )

    assert recorder.calls == 1
    assert telemetry.external_request_attempts == 1
    assert telemetry.external_request_budget == 1
    assert telemetry.retry_performed is False
    assert telemetry.fallback_performed is False


def test_generic_call_keeps_transient_retry_default_behavior():
    recorder = _CreateRecorder([_StatusError("server unavailable", 503), _response()])

    with _patch_resolution(recorder), patch("agent.auxiliary_client.time.sleep"):
        result = call_llm(task="generic", messages=[{"role": "user", "content": "synthetic"}])

    assert recorder.calls == 2
    assert result.choices[0].message.content


def test_strict_policy_disables_temperature_recovery_retry():
    recorder = _CreateRecorder([_StatusError("unsupported parameter: temperature", 400), _response()])
    telemetry = ExternalRequestTelemetry()

    with _patch_resolution(recorder):
        with pytest.raises(_StatusError):
            call_llm(
                task="weekly_menu_generation",
                messages=[{"role": "user", "content": "synthetic"}],
                temperature=0.2,
                call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
                request_telemetry=telemetry,
            )

    assert recorder.calls == 1
    assert telemetry.external_request_attempts == 1
    assert telemetry.retry_performed is False


def test_generic_call_keeps_temperature_recovery_default_behavior():
    recorder = _CreateRecorder([_StatusError("unsupported parameter: temperature", 400), _response()])

    with _patch_resolution(recorder):
        result = call_llm(
            task="generic",
            messages=[{"role": "user", "content": "synthetic"}],
            temperature=0.2,
        )

    assert recorder.calls == 2
    assert result.choices[0].message.content


def test_strict_policy_disables_payment_fallback():
    recorder = _CreateRecorder([_StatusError("payment required", 402), _response()])
    telemetry = ExternalRequestTelemetry()

    with _patch_resolution(recorder), patch("agent.auxiliary_client._try_configured_fallback_chain") as fallback_chain, patch("agent.auxiliary_client._try_main_agent_model_fallback") as main_fallback:
        with pytest.raises(_StatusError):
            call_llm(
                task="weekly_menu_generation",
                messages=[{"role": "user", "content": "synthetic"}],
                call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
                request_telemetry=telemetry,
            )

    assert recorder.calls == 1
    assert telemetry.external_request_attempts == 1
    assert telemetry.fallback_performed is False
    fallback_chain.assert_not_called()
    main_fallback.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("synthetic timeout"),
        _StatusError("rate limit", 429),
        _StatusError("server error", 500),
        _StatusError("bad gateway", 502),
        _StatusError("unavailable", 503),
        _StatusError("bad-credentials", 401),
        _StatusError("payment required", 402),
        _StatusError("model does not exist", 404),
    ],
)
def test_strict_policy_limits_common_provider_failures_to_one_external_request(error):
    recorder = _CreateRecorder([error, _response()])
    telemetry = ExternalRequestTelemetry()

    with _patch_resolution(recorder), patch("agent.auxiliary_client.time.sleep"), patch("agent.auxiliary_client._refresh_provider_credentials", return_value=True) as refresh, patch("agent.auxiliary_client._try_configured_fallback_chain") as fallback_chain, patch("agent.auxiliary_client._try_main_agent_model_fallback") as main_fallback, patch("agent.auxiliary_client._refresh_nous_recommended_model", return_value="other-model") as refresh_model:
        with pytest.raises(Exception):
            call_llm(
                task="weekly_menu_generation",
                messages=[{"role": "user", "content": "synthetic"}],
                call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
                request_telemetry=telemetry,
            )

    assert recorder.calls == 1
    assert telemetry.external_request_attempts == 1
    assert telemetry.retry_performed is False
    assert telemetry.fallback_performed is False
    refresh.assert_not_called()
    fallback_chain.assert_not_called()
    main_fallback.assert_not_called()
    refresh_model.assert_not_called()


def test_strict_policy_disables_max_tokens_recovery_retry():
    recorder = _CreateRecorder([_StatusError("unsupported parameter: max_tokens", 400), _response()])
    telemetry = ExternalRequestTelemetry()

    with _patch_resolution(recorder):
        with pytest.raises(_StatusError):
            call_llm(
                task="weekly_menu_generation",
                messages=[{"role": "user", "content": "synthetic"}],
                max_tokens=100,
                call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
                request_telemetry=telemetry,
            )

    assert recorder.calls == 1
    assert telemetry.external_request_attempts == 1
    assert telemetry.retry_performed is False


def test_budget_guard_prevents_second_direct_recovery_request_when_policy_budget_is_one():
    recorder = _CreateRecorder([_StatusError("unsupported parameter: temperature", 400), _response()])
    telemetry = ExternalRequestTelemetry()
    policy = WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY.__class__(
        max_external_requests=1,
        retry_transient=False,
        retry_without_temperature=True,
        retry_without_max_tokens=True,
        refresh_model=True,
        recover_credentials=True,
        fallback_provider=True,
        fallback_model=True,
    )

    with _patch_resolution(recorder):
        with pytest.raises(Exception):
            call_llm(
                task="budgeted_generic",
                messages=[{"role": "user", "content": "synthetic"}],
                temperature=0.2,
                call_policy=policy,
                request_telemetry=telemetry,
            )

    assert recorder.calls == 1
    assert telemetry.external_request_attempts == 1
