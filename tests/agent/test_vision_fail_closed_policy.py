"""Offline contract tests for explicit vision-provider fail-closed routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agent.auxiliary_client import (
    DEFAULT_LLM_CALL_POLICY,
    LLMCallPolicy,
    LLMServiceUnavailableError,
    VISION_DEFAULT_LLM_CALL_POLICY,
    VISION_SINGLE_REQUEST_LLM_CALL_POLICY,
    _normalize_llm_call_policy,
    async_call_llm,
    call_llm,
)


def _response(content: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


def _sync_client() -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = _response()
    return client


def _async_client() -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_response())
    return client


def _resolved(provider: str) -> tuple[str, str, None, None, str]:
    return provider, "vision-model", None, None, "chat_completions"


def _payment_error() -> Exception:
    error = Exception("synthetic payment failure")
    error.status_code = 402
    return error


@pytest.mark.parametrize("provider", ["gemini", "qwen", "unknown-provider"])
def test_explicit_unavailable_provider_fails_closed_sync(provider):
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved(provider)),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=(provider, None, None),
        ) as resolver,
    ):
        with pytest.raises(LLMServiceUnavailableError, match="vision provider is unavailable"):
            call_llm(task="vision", messages=[])

    assert resolver.call_count == 1
    assert resolver.call_args.kwargs["provider"] == provider


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "qwen", "unknown-provider"])
async def test_explicit_unavailable_provider_fails_closed_async(provider):
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved(provider)),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=(provider, None, None),
        ) as resolver,
    ):
        with pytest.raises(LLMServiceUnavailableError, match="vision provider is unavailable"):
            await async_call_llm(task="vision", messages=[])

    assert resolver.call_count == 1
    assert resolver.call_args.kwargs["provider"] == provider


def test_explicit_available_provider_is_used_sync():
    client = _sync_client()
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=("gemini", client, "vision-model"),
        ) as resolver,
    ):
        result = call_llm(task="vision", messages=[])

    assert result.choices[0].message.content == "ok"
    assert resolver.call_count == 1
    client.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_explicit_available_provider_is_used_async():
    client = _async_client()
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=("gemini", client, "vision-model"),
        ) as resolver,
    ):
        result = await async_call_llm(task="vision", messages=[])

    assert result.choices[0].message.content == "ok"
    assert resolver.call_count == 1
    client.chat.completions.create.assert_awaited_once()


def test_explicit_provider_fallback_requires_explicit_policy():
    client = _sync_client()
    policy = LLMCallPolicy(fallback_provider=True)
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            side_effect=[("gemini", None, None), ("nous", client, "fallback-model")],
        ) as resolver,
    ):
        call_llm(task="vision", messages=[], call_policy=policy)

    assert resolver.call_count == 2
    assert resolver.call_args_list[1] == call(
        provider="auto",
        model="vision-model",
        async_mode=False,
    )


@pytest.mark.asyncio
async def test_explicit_provider_fallback_requires_explicit_policy_async():
    client = _async_client()
    policy = LLMCallPolicy(fallback_provider=True)
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            side_effect=[("gemini", None, None), ("nous", client, "fallback-model")],
        ) as resolver,
    ):
        await async_call_llm(task="vision", messages=[], call_policy=policy)

    assert resolver.call_count == 2
    assert resolver.call_args_list[1] == call(
        provider="auto",
        model="vision-model",
        async_mode=True,
    )


def test_single_request_policy_never_resolves_foreign_provider():
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=("gemini", None, None),
        ) as resolver,
    ):
        with pytest.raises(LLMServiceUnavailableError):
            call_llm(
                task="vision",
                messages=[],
                call_policy=VISION_SINGLE_REQUEST_LLM_CALL_POLICY,
            )

    assert resolver.call_count == 1


def test_auto_provider_keeps_existing_auto_resolution():
    client = _sync_client()
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("auto")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=("openrouter", client, "auto-model"),
        ) as resolver,
    ):
        call_llm(task="vision", messages=[])

    assert resolver.call_count == 1


def test_client_initialization_error_is_safely_wrapped():
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            side_effect=ValueError("synthetic internal detail"),
        ),
    ):
        with pytest.raises(LLMServiceUnavailableError) as caught:
            call_llm(task="vision", messages=[])

    assert str(caught.value) == "Configured vision provider is unavailable."
    assert isinstance(caught.value.cause, ValueError)


def test_text_routing_does_not_use_vision_resolver():
    client = _sync_client()
    with (
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("deepseek", "deepseek-chat", None, None, "chat_completions"),
        ),
        patch("agent.auxiliary_client._get_cached_client", return_value=(client, "deepseek-chat")),
        patch("agent.auxiliary_client.resolve_vision_provider_client") as vision_resolver,
    ):
        call_llm(task="compression", messages=[])

    vision_resolver.assert_not_called()
    client.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_client_initialization_error_is_safely_wrapped_async():
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            side_effect=ValueError("synthetic internal detail"),
        ) as resolver,
    ):
        with pytest.raises(LLMServiceUnavailableError) as caught:
            await async_call_llm(task="vision", messages=[])

    assert str(caught.value) == "Configured vision provider is unavailable."
    assert resolver.call_count == 1


def test_task_aware_default_policy_changes_only_vision():
    assert _normalize_llm_call_policy(None, task="vision") is VISION_DEFAULT_LLM_CALL_POLICY
    assert VISION_DEFAULT_LLM_CALL_POLICY.fallback_provider is False
    assert _normalize_llm_call_policy(None, task="compression") is DEFAULT_LLM_CALL_POLICY
    assert DEFAULT_LLM_CALL_POLICY.fallback_provider is True


@pytest.mark.parametrize(
    "policy",
    [None, LLMCallPolicy(fallback_provider=False)],
    ids=["implicit-vision-default", "explicit-strict"],
)
def test_explicit_provider_request_failure_does_not_fallback_sync(policy):
    primary = _sync_client()
    primary.chat.completions.create.side_effect = _payment_error()
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=("gemini", primary, "vision-model"),
        ) as resolver,
        patch("agent.auxiliary_client._recoverable_pool_provider", return_value=None),
        patch("agent.auxiliary_client._try_configured_fallback_chain") as configured_fallback,
        patch("agent.auxiliary_client._try_main_agent_model_fallback") as main_fallback,
    ):
        with pytest.raises(Exception, match="synthetic payment failure"):
            call_llm(task="vision", messages=[], call_policy=policy)

    assert resolver.call_count == 1
    primary.chat.completions.create.assert_called_once()
    configured_fallback.assert_not_called()
    main_fallback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [None, LLMCallPolicy(fallback_provider=False)],
    ids=["implicit-vision-default", "explicit-strict"],
)
async def test_explicit_provider_request_failure_does_not_fallback_async(policy):
    primary = _async_client()
    primary.chat.completions.create.side_effect = _payment_error()
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=("gemini", primary, "vision-model"),
        ) as resolver,
        patch("agent.auxiliary_client._recoverable_pool_provider", return_value=None),
        patch("agent.auxiliary_client._try_configured_fallback_chain") as configured_fallback,
        patch("agent.auxiliary_client._try_main_agent_model_fallback") as main_fallback,
    ):
        with pytest.raises(Exception, match="synthetic payment failure"):
            await async_call_llm(task="vision", messages=[], call_policy=policy)

    assert resolver.call_count == 1
    primary.chat.completions.create.assert_awaited_once()
    configured_fallback.assert_not_called()
    main_fallback.assert_not_called()


def test_explicit_policy_can_enable_post_request_fallback_sync():
    primary = _sync_client()
    primary.chat.completions.create.side_effect = _payment_error()
    fallback = _sync_client()
    policy = LLMCallPolicy(fallback_provider=True)
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=("gemini", primary, "vision-model"),
        ) as resolver,
        patch("agent.auxiliary_client._recoverable_pool_provider", return_value=None),
        patch("agent.auxiliary_client._mark_provider_unhealthy"),
        patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(fallback, "fallback-model", "configured"),
        ) as configured_fallback,
    ):
        result = call_llm(task="vision", messages=[], call_policy=policy)

    assert result.choices[0].message.content == "ok"
    assert resolver.call_count == 1
    configured_fallback.assert_called_once()
    fallback.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_explicit_policy_can_enable_post_request_fallback_async():
    primary = _async_client()
    primary.chat.completions.create.side_effect = _payment_error()
    sync_fallback = _sync_client()
    async_fallback = _async_client()
    policy = LLMCallPolicy(fallback_provider=True)
    with (
        patch("agent.auxiliary_client._resolve_task_provider_model", return_value=_resolved("gemini")),
        patch(
            "agent.auxiliary_client.resolve_vision_provider_client",
            return_value=("gemini", primary, "vision-model"),
        ) as resolver,
        patch("agent.auxiliary_client._recoverable_pool_provider", return_value=None),
        patch("agent.auxiliary_client._mark_provider_unhealthy"),
        patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(sync_fallback, "fallback-model", "configured"),
        ) as configured_fallback,
        patch(
            "agent.auxiliary_client._to_async_client",
            return_value=(async_fallback, "fallback-model"),
        ),
    ):
        result = await async_call_llm(task="vision", messages=[], call_policy=policy)

    assert result.choices[0].message.content == "ok"
    assert resolver.call_count == 1
    configured_fallback.assert_called_once()
    async_fallback.chat.completions.create.assert_awaited_once()
