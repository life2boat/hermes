'''Fallback boundaries for opt-in Qwen/DashScope Vision requests.'''

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import (
    LLMServiceUnavailableError,
    VISION_SINGLE_REQUEST_LLM_CALL_POLICY,
    call_llm,
)


def _resolved_alibaba():
    return 'alibaba', 'qwen3-vl-8b-instruct', None, None, 'chat_completions'


def test_qwen_request_failure_is_single_request_and_never_forwards_pixels():
    primary = MagicMock()
    failure = RuntimeError('synthetic provider detail')
    failure.status_code = 503
    primary.chat.completions.create.side_effect = failure
    messages = [
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'describe'},
                {
                    'type': 'image_url',
                    'image_url': {'url': 'data:image/png;base64,c2FmZQ=='},
                },
            ],
        }
    ]

    with (
        patch(
            'agent.auxiliary_client._resolve_task_provider_model',
            return_value=_resolved_alibaba(),
        ),
        patch(
            'agent.auxiliary_client.resolve_vision_provider_client',
            return_value=('alibaba', primary, 'qwen3-vl-8b-instruct'),
        ) as resolver,
        patch('agent.auxiliary_client._try_configured_fallback_chain') as chain,
        patch(
            'agent.auxiliary_client._try_main_agent_model_fallback'
        ) as main_fallback,
        patch('agent.auxiliary_client._try_payment_fallback') as payment_fallback,
    ):
        with pytest.raises(RuntimeError, match='synthetic provider detail'):
            call_llm(
                task='vision',
                messages=messages,
                call_policy=VISION_SINGLE_REQUEST_LLM_CALL_POLICY,
            )

    resolver.assert_called_once()
    primary.chat.completions.create.assert_called_once()
    assert primary.chat.completions.create.call_args.kwargs['messages'] == messages
    chain.assert_not_called()
    main_fallback.assert_not_called()
    payment_fallback.assert_not_called()


def test_unavailable_qwen_provider_raises_sanitized_service_error():
    with (
        patch(
            'agent.auxiliary_client._resolve_task_provider_model',
            return_value=_resolved_alibaba(),
        ),
        patch(
            'agent.auxiliary_client.resolve_vision_provider_client',
            return_value=('alibaba', None, None),
        ) as resolver,
    ):
        with pytest.raises(LLMServiceUnavailableError) as caught:
            call_llm(task='vision', messages=[])

    assert str(caught.value) == 'Configured vision provider is unavailable.'
    resolver.assert_called_once()


def test_standard_text_routing_does_not_enter_qwen_vision_resolver():
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='ok'))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )

    with (
        patch(
            'agent.auxiliary_client._resolve_task_provider_model',
            return_value=(
                'openai-api',
                'gpt-5-mini',
                None,
                None,
                'chat_completions',
            ),
        ),
        patch(
            'agent.auxiliary_client._get_cached_client',
            return_value=(client, 'gpt-5-mini'),
        ),
        patch(
            'agent.auxiliary_client.resolve_vision_provider_client'
        ) as vision_resolver,
    ):
        response = call_llm(task='compression', messages=[])

    assert response.choices[0].message.content == 'ok'
    vision_resolver.assert_not_called()
    client.chat.completions.create.assert_called_once()
