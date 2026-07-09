from __future__ import annotations

import json

import httpx


def test_build_native_request_converts_data_url_to_inline_data_without_openai_fields():
    from agent.gemini_native_adapter import build_gemini_request

    request = build_gemini_request(
        messages=[
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'Describe the meal'},
                    {
                        'type': 'image_url',
                        'image_url': {'url': 'data:image/png;base64,QUJDRA=='},
                    },
                ],
            }
        ],
        tools=[],
        tool_choice=None,
        max_tokens=321,
    )

    parts = request['contents'][0]['parts']
    assert parts[0] == {'text': 'Describe the meal'}
    assert parts[1]['inlineData']['mimeType'] == 'image/png'
    assert parts[1]['inlineData']['data'] == 'QUJDRA=='
    assert request['generationConfig']['maxOutputTokens'] == 321

    encoded = json.dumps(request, ensure_ascii=False)
    assert 'response_format' not in encoded
    assert 'json_schema' not in encoded
    assert 'max_completion_tokens' not in encoded
    assert 'dashscope-intl.aliyuncs.com' not in encoded


def test_native_client_uses_header_auth_not_query_param(monkeypatch):
    from agent.gemini_native_adapter import GeminiNativeClient

    recorded = {}

    class DummyHTTP:
        def post(self, url, json=None, headers=None, timeout=None):
            recorded['url'] = url
            recorded['headers'] = headers
            return httpx.Response(
                200,
                json={
                    'candidates': [{'content': {'parts': [{'text': 'ok'}]}, 'finishReason': 'STOP'}],
                    'usageMetadata': {'promptTokenCount': 1, 'candidatesTokenCount': 1, 'totalTokenCount': 2},
                },
            )

        def close(self):
            return None

    monkeypatch.setattr('agent.gemini_native_adapter.httpx.Client', lambda *a, **k: DummyHTTP())
    client = GeminiNativeClient(api_key='AIza-test')
    response = client.chat.completions.create(
        model='gemini-2.5-flash',
        messages=[{'role': 'user', 'content': 'Hello'}],
    )

    assert recorded['headers']['x-goog-api-key'] == 'AIza-test'
    assert 'Authorization' not in recorded['headers']
    assert '?key=' not in recorded['url']
    assert response.choices[0].message.content == 'ok'


def test_transport_timeout_error_is_sanitized():
    from agent.gemini_native_adapter import GeminiAPIError

    err = GeminiAPIError(
        'Gemini request timed out',
        code='gemini_timeout',
        stage='TRANSPORT',
        request_was_attempted=True,
        provider_response_received=False,
        response_parse_attempted=False,
        validator_reached=False,
        retryable=True,
        safe_reason='timeout',
    )

    assert 'AIza' not in str(err)
    assert 'base64' not in str(err)
    assert err.safe_reason == 'timeout'
