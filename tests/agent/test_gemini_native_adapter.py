"""Tests for the native Google AI Studio Gemini adapter."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest


class DummyResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


def test_build_native_request_preserves_thought_signature_on_tool_replay():
    from agent.gemini_native_adapter import build_gemini_request

    request = build_gemini_request(
        messages=[
            {"role": "system", "content": "Be helpful."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Paris"}',
                        },
                        "extra_content": {
                            "google": {"thought_signature": "sig-123"}
                        },
                    }
                ],
            },
        ],
        tools=[],
        tool_choice=None,
    )

    parts = request["contents"][0]["parts"]
    assert parts[0]["functionCall"]["name"] == "get_weather"
    assert parts[0]["thoughtSignature"] == "sig-123"


def test_build_native_request_uses_original_function_name_for_tool_result():
    from agent.gemini_native_adapter import build_gemini_request

    request = build_gemini_request(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Paris"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"forecast": "sunny"}',
            },
        ],
        tools=[],
        tool_choice=None,
    )

    tool_response = request["contents"][1]["parts"][0]["functionResponse"]
    assert tool_response["name"] == "get_weather"


def test_build_native_request_strips_json_schema_only_fields_from_tool_parameters():
    from agent.gemini_native_adapter import build_gemini_request

    request = build_gemini_request(
        messages=[{"role": "user", "content": "Hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup_weather",
                    "description": "Weather lookup",
                    "parameters": {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "city": {
                                "type": "string",
                                "$schema": "ignored",
                                "description": "City name",
                            }
                        },
                        "required": ["city"],
                    },
                },
            }
        ],
        tool_choice=None,
    )

    params = request["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "$schema" not in params
    assert "additionalProperties" not in params
    assert params["type"] == "object"
    assert params["properties"]["city"] == {
        "type": "string",
        "description": "City name",
    }


def test_translate_native_response_surfaces_reasoning_and_tool_calls():
    from agent.gemini_native_adapter import translate_gemini_response

    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "text": "thinking..."},
                        {"functionCall": {"name": "search", "args": {"q": "hermes"}}},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        },
    }

    response = translate_gemini_response(payload, model="gemini-2.5-flash")
    choice = response.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.reasoning == "thinking..."
    assert choice.message.tool_calls[0].function.name == "search"
    assert json.loads(choice.message.tool_calls[0].function.arguments) == {"q": "hermes"}


def test_native_client_uses_x_goog_api_key_and_native_models_endpoint(monkeypatch):
    from agent.gemini_native_adapter import GeminiNativeClient

    recorded = {}

    class DummyHTTP:
        def post(self, url, json=None, headers=None, timeout=None):
            recorded["url"] = url
            recorded["json"] = json
            recorded["headers"] = headers
            return DummyResponse(
                payload={
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "hello"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 1,
                        "candidatesTokenCount": 1,
                        "totalTokenCount": 2,
                    },
                }
            )

        def close(self):
            return None

    monkeypatch.setattr("agent.gemini_native_adapter.httpx.Client", lambda *a, **k: DummyHTTP())

    client = GeminiNativeClient(api_key="AIza-test", base_url="https://generativelanguage.googleapis.com/v1beta")
    response = client.chat.completions.create(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "Hello"}],
    )

    assert recorded["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert recorded["headers"]["x-goog-api-key"] == "AIza-test"
    assert "Authorization" not in recorded["headers"]
    assert response.choices[0].message.content == "hello"


def test_native_client_fixed_string_key_reproduces_stale_capture(monkeypatch):
    from agent.gemini_native_adapter import GeminiNativeClient

    seen_keys = []
    key_source = {"value": "TEST_GEMINI_KEY_DO_NOT_LOG_7f93A"}

    class DummyHTTP:
        def post(self, url, json=None, headers=None, timeout=None):
            seen_keys.append(headers["x-goog-api-key"])
            return DummyResponse(
                payload={
                    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
                }
            )

        def close(self):
            return None

    monkeypatch.setattr("agent.gemini_native_adapter.httpx.Client", lambda *a, **k: DummyHTTP())
    client = GeminiNativeClient(api_key=key_source["value"])
    client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": "one"}])
    key_source["value"] = "TEST_GEMINI_KEY_DO_NOT_LOG_7f93B"
    client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": "two"}])

    assert seen_keys == ["TEST_GEMINI_KEY_DO_NOT_LOG_7f93A", "TEST_GEMINI_KEY_DO_NOT_LOG_7f93A"]


def test_native_client_runtime_key_provider_refreshes_per_request_without_rebuild(monkeypatch):
    from agent.gemini_native_adapter import GeminiNativeClient

    seen_keys = []
    call_count = 0
    key_source = {"value": "TEST_GEMINI_KEY_DO_NOT_LOG_7f93A"}

    def provider():
        nonlocal call_count
        call_count += 1
        return key_source["value"]

    class DummyHTTP:
        def post(self, url, json=None, headers=None, timeout=None):
            seen_keys.append(headers["x-goog-api-key"])
            return DummyResponse(
                payload={
                    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
                }
            )

        def close(self):
            return None

    monkeypatch.setattr("agent.gemini_native_adapter.httpx.Client", lambda *a, **k: DummyHTTP())
    client = GeminiNativeClient(api_key=provider)
    client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": "one"}])
    key_source["value"] = "TEST_GEMINI_KEY_DO_NOT_LOG_7f93B"
    client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": "two"}])

    assert seen_keys == ["TEST_GEMINI_KEY_DO_NOT_LOG_7f93A", "TEST_GEMINI_KEY_DO_NOT_LOG_7f93B"]
    assert call_count == 2


@pytest.mark.asyncio
async def test_async_native_client_runtime_key_provider_refreshes_per_request(monkeypatch):
    from agent.gemini_native_adapter import AsyncGeminiNativeClient, GeminiNativeClient

    seen_keys = []
    call_count = 0
    key_source = {"value": "TEST_GEMINI_KEY_DO_NOT_LOG_7f93A"}

    def provider():
        nonlocal call_count
        call_count += 1
        return key_source["value"]

    class DummyHTTP:
        def post(self, url, json=None, headers=None, timeout=None):
            seen_keys.append(headers["x-goog-api-key"])
            return DummyResponse(
                payload={
                    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
                }
            )

        def close(self):
            return None

    monkeypatch.setattr("agent.gemini_native_adapter.httpx.Client", lambda *a, **k: DummyHTTP())
    async_client = AsyncGeminiNativeClient(GeminiNativeClient(api_key=provider))
    await async_client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": "one"}])
    key_source["value"] = "TEST_GEMINI_KEY_DO_NOT_LOG_7f93B"
    await async_client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": "two"}])

    assert seen_keys == ["TEST_GEMINI_KEY_DO_NOT_LOG_7f93A", "TEST_GEMINI_KEY_DO_NOT_LOG_7f93B"]
    assert call_count == 2


def test_native_stream_uses_request_time_header_auth(monkeypatch):
    from agent.gemini_native_adapter import GeminiNativeClient

    seen_keys = []
    call_count = 0
    key_source = {"value": "TEST_GEMINI_KEY_DO_NOT_LOG_7f93A"}

    def provider():
        nonlocal call_count
        call_count += 1
        return key_source["value"]

    class DummyStreamResponse:
        status_code = 200
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def iter_text(self):
            return iter(['data: {"candidates":[{"content":{"parts":[{"text":"ok"}]},"finishReason":"STOP"}]}\n\n'])

    class DummyHTTP:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            seen_keys.append(headers["x-goog-api-key"])
            return DummyStreamResponse()
        def close(self):
            return None

    monkeypatch.setattr("agent.gemini_native_adapter.httpx.Client", lambda *a, **k: DummyHTTP())
    client = GeminiNativeClient(api_key=provider)
    chunks = list(client.chat.completions.create(stream=True, model="gemini-2.5-flash", messages=[{"role": "user", "content": "one"}]))
    assert chunks
    assert seen_keys == ["TEST_GEMINI_KEY_DO_NOT_LOG_7f93A"]
    assert call_count == 1


def test_native_client_empty_runtime_key_fails_before_network(monkeypatch):
    from agent.gemini_native_adapter import GeminiCredentialError, GeminiNativeClient

    called = False

    class DummyHTTP:
        def post(self, *args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("network should not be called")
        def close(self):
            return None

    monkeypatch.setattr("agent.gemini_native_adapter.httpx.Client", lambda *a, **k: DummyHTTP())
    client = GeminiNativeClient(api_key=lambda: "   ")
    with pytest.raises(GeminiCredentialError) as excinfo:
        client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": "one"}])
    assert called is False
    assert "empty API key" in str(excinfo.value)


def test_native_client_key_provider_exception_is_redacted_and_local(monkeypatch):
    from agent.gemini_native_adapter import GeminiCredentialError, GeminiNativeClient

    sentinel = "TEST_GEMINI_KEY_DO_NOT_LOG_7f93SECRET"
    called = False

    class DummyHTTP:
        def post(self, *args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("network should not be called")
        def close(self):
            return None

    def provider():
        raise ValueError(sentinel)

    monkeypatch.setattr("agent.gemini_native_adapter.httpx.Client", lambda *a, **k: DummyHTTP())
    client = GeminiNativeClient(api_key=provider)
    with pytest.raises(GeminiCredentialError) as excinfo:
        client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": "one"}])
    assert called is False
    assert sentinel not in str(excinfo.value)
    assert sentinel not in repr(client)


def test_native_client_concurrent_requests_isolate_runtime_keys(monkeypatch):
    from agent.gemini_native_adapter import GeminiNativeClient

    seen = []
    lock = threading.Lock()
    key_by_thread = {}
    call_count = 0

    def provider():
        nonlocal call_count
        with lock:
            call_count += 1
        return key_by_thread[threading.get_ident()]

    class DummyHTTP:
        def post(self, url, json=None, headers=None, timeout=None):
            with lock:
                seen.append(headers["x-goog-api-key"])
            return DummyResponse(
                payload={
                    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
                }
            )
        def close(self):
            return None

    monkeypatch.setattr("agent.gemini_native_adapter.httpx.Client", lambda *a, **k: DummyHTTP())
    client = GeminiNativeClient(api_key=provider)

    def run(key):
        key_by_thread[threading.get_ident()] = key
        return client.chat.completions.create(model="gemini-2.5-flash", messages=[{"role": "user", "content": key}])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ["TEST_GEMINI_KEY_DO_NOT_LOG_7f93A", "TEST_GEMINI_KEY_DO_NOT_LOG_7f93B"]))

    assert sorted(seen) == ["TEST_GEMINI_KEY_DO_NOT_LOG_7f93A", "TEST_GEMINI_KEY_DO_NOT_LOG_7f93B"]
    assert call_count == 2


def test_native_http_error_keeps_status_and_retry_after():
    from agent.gemini_native_adapter import gemini_http_error

    response = DummyResponse(
        status_code=429,
        headers={"Retry-After": "17"},
        payload={
            "error": {
                "code": 429,
                "message": "quota exhausted",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "RESOURCE_EXHAUSTED",
                        "metadata": {"service": "generativelanguage.googleapis.com"},
                    }
                ],
            }
        },
    )

    err = gemini_http_error(response)
    assert getattr(err, "status_code", None) == 429
    assert getattr(err, "retry_after", None) == 17.0
    assert "quota exhausted" in str(err)


def test_native_client_accepts_injected_http_client():
    from agent.gemini_native_adapter import GeminiNativeClient

    injected = SimpleNamespace(close=lambda: None)
    client = GeminiNativeClient(api_key="AIza-test", http_client=injected)
    assert client._http is injected


def test_native_client_rejects_empty_api_key_with_actionable_message():
    """Empty/whitespace api_key must raise at construction, not produce a cryptic
    Google GFE 'Error 400 (Bad Request)!!1' HTML page on the first request."""
    from agent.gemini_native_adapter import GeminiNativeClient

    for bad in ("", "   ", None):
        with pytest.raises(RuntimeError) as excinfo:
            GeminiNativeClient(api_key=bad)  # type: ignore[arg-type]
        msg = str(excinfo.value)
        assert "GOOGLE_API_KEY" in msg and "GEMINI_API_KEY" in msg
        assert "aistudio.google.com" in msg


@pytest.mark.asyncio
async def test_async_native_client_streams_without_requiring_async_iterator_from_sync_client():
    from agent.gemini_native_adapter import AsyncGeminiNativeClient

    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"), finish_reason=None)])
    sync_stream = iter([chunk])

    def _advance(iterator):
        try:
            return False, next(iterator)
        except StopIteration:
            return True, None

    sync_client = SimpleNamespace(
        api_key="AIza-test",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: sync_stream)),
        _advance_stream_iterator=_advance,
        close=lambda: None,
    )

    async_client = AsyncGeminiNativeClient(sync_client)
    stream = await async_client.chat.completions.create(stream=True)
    collected = []
    async for item in stream:
        collected.append(item)
    assert collected == [chunk]


def test_stream_event_translation_emits_tool_call_delta_with_stable_index():
    from agent.gemini_native_adapter import translate_stream_event

    tool_call_indices = {}
    event = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "search", "args": {"q": "abc"}}}
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }

    first = translate_stream_event(event, model="gemini-2.5-flash", tool_call_indices=tool_call_indices)
    second = translate_stream_event(event, model="gemini-2.5-flash", tool_call_indices=tool_call_indices)

    assert first[0].choices[0].delta.tool_calls[0].index == 0
    assert second[0].choices[0].delta.tool_calls[0].index == 0
    assert first[0].choices[0].delta.tool_calls[0].id == second[0].choices[0].delta.tool_calls[0].id
    assert first[0].choices[0].delta.tool_calls[0].function.arguments == '{"q": "abc"}'
    assert second[0].choices[0].delta.tool_calls[0].function.arguments == ""
    assert first[-1].choices[0].finish_reason == "tool_calls"


def test_stream_event_translation_keeps_identical_calls_in_distinct_parts():
    from agent.gemini_native_adapter import translate_stream_event

    event = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "search", "args": {"q": "abc"}}},
                        {"functionCall": {"name": "search", "args": {"q": "abc"}}},
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }

    chunks = translate_stream_event(event, model="gemini-2.5-flash", tool_call_indices={})
    tool_chunks = [chunk for chunk in chunks if chunk.choices[0].delta.tool_calls]
    assert tool_chunks[0].choices[0].delta.tool_calls[0].index == 0
    assert tool_chunks[1].choices[0].delta.tool_calls[0].index == 1
    assert tool_chunks[0].choices[0].delta.tool_calls[0].id != tool_chunks[1].choices[0].delta.tool_calls[0].id


def test_max_tokens_none_defaults_to_gemini_output_ceiling():
    """max_tokens=None must send the model's full output ceiling, not omit it.

    Gemini's native generateContent applies a low internal default when
    maxOutputTokens is absent, truncating tool calls mid-stream. Hermes passes
    None to mean "unlimited", so the adapter must translate that to the
    published 65,535 ceiling rather than leaving the field unset.
    """
    from agent.gemini_native_adapter import (
        build_gemini_request,
        GEMINI_DEFAULT_MAX_OUTPUT_TOKENS,
    )

    req = build_gemini_request(messages=[{"role": "user", "content": "hi"}], max_tokens=None)
    assert req["generationConfig"]["maxOutputTokens"] == GEMINI_DEFAULT_MAX_OUTPUT_TOKENS == 65535


def test_explicit_max_tokens_is_respected():
    from agent.gemini_native_adapter import build_gemini_request

    req = build_gemini_request(messages=[{"role": "user", "content": "hi"}], max_tokens=4096)
    assert req["generationConfig"]["maxOutputTokens"] == 4096
