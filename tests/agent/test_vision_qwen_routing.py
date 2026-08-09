'''Provider-contract tests for opt-in Qwen/DashScope Vision routing.'''

from __future__ import annotations

from urllib.parse import urlparse
from unittest.mock import patch

import pytest

from agent import auxiliary_client


@pytest.fixture(autouse=True)
def clean_auxiliary_cache():
    auxiliary_client.shutdown_cached_clients()
    yield
    auxiliary_client.shutdown_cached_clients()


def test_qwen_provider_ids_are_unambiguous():
    assert auxiliary_client._normalize_aux_provider('alibaba') == 'alibaba'
    assert auxiliary_client._normalize_aux_provider('qwen-dashscope') == 'alibaba'
    assert auxiliary_client._normalize_aux_provider('qwen-oauth') == 'qwen-oauth'
    assert auxiliary_client._normalize_aux_provider('qwen-portal') == 'qwen-oauth'
    assert auxiliary_client._normalize_aux_provider('qwen') == 'qwen'


@pytest.mark.parametrize('provider', ['alibaba', 'qwen-dashscope'])
def test_dashscope_vision_uses_registry_credentials_and_explicit_model(
    monkeypatch,
    provider,
):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'synthetic-dashscope-test-key')

    resolved, client, model = auxiliary_client.resolve_vision_provider_client(
        provider=provider,
        model='qwen3-vl-8b-instruct',
    )

    assert resolved == 'alibaba'
    assert client is not None
    assert model == 'qwen3-vl-8b-instruct'
    assert urlparse(str(client.base_url)).hostname == 'dashscope-intl.aliyuncs.com'
    assert client.api_key == 'synthetic-dashscope-test-key'


def test_qwen_oauth_does_not_borrow_dashscope_credentials(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'must-not-cross-provider-boundary')

    with patch('hermes_cli.auth.resolve_api_key_provider_credentials') as resolver:
        provider, client, model = auxiliary_client.resolve_vision_provider_client(
            provider='qwen-oauth',
            model='qwen3-vl-8b-instruct',
        )

    assert (provider, client, model) == ('qwen-oauth', None, None)
    resolver.assert_not_called()


def test_bare_qwen_alias_fails_closed_instead_of_guessing(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'must-not-select-dashscope')

    with patch('agent.auxiliary_client.OpenAI') as openai_client:
        provider, client, model = auxiliary_client.resolve_vision_provider_client(
            provider='qwen',
            model='qwen3-vl-8b-instruct',
        )

    assert (provider, client, model) == ('qwen', None, None)
    openai_client.assert_not_called()


@pytest.mark.parametrize('provider', ['alibaba', 'qwen-dashscope', 'qwen-oauth'])
def test_qwen_vision_without_explicit_model_fails_closed(monkeypatch, provider):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'synthetic-dashscope-test-key')
    monkeypatch.setenv('QWEN_API_KEY', 'synthetic-qwen-test-key')
    with patch('agent.auxiliary_client.OpenAI') as openai_client:
        resolved, client, model = auxiliary_client.resolve_vision_provider_client(
            provider=provider,
            model=None,
        )

    expected = 'alibaba' if provider == 'qwen-dashscope' else provider
    assert (resolved, client, model) == (expected, None, None)
    openai_client.assert_not_called()
