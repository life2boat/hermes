from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.gemini_native_adapter import GeminiAPIError


def _png_bytes() -> bytes:
    return b'\x89PNG\r\n\x1a\n' + (b'\x00' * 8)


@pytest.mark.asyncio
async def test_gemini_provider_failure_returns_sanitized_diagnostic(tmp_path, monkeypatch, caplog):
    from tools.vision_tools import vision_analyze_tool

    image_path = tmp_path / 'meal.png'
    image_path.write_bytes(_png_bytes())
    raw_marker = 'RAW_PROVIDER_MESSAGE_REDACTED_MARKER'
    error = GeminiAPIError(
        raw_marker,
        code='gemini_http_403',
        status_code=403,
        stage='PROVIDER_HTTP',
        request_was_attempted=True,
        provider_response_received=True,
        response_parse_attempted=False,
        validator_reached=False,
        retryable=False,
        sanitized_provider_code='PERMISSION_DENIED',
        safe_reason='http_403',
    )

    monkeypatch.setitem(vision_analyze_tool.__globals__, '_configured_vision_provider_and_model', lambda _model=None: ('gemini', 'gemini-2.5-flash'))
    monkeypatch.setitem(vision_analyze_tool.__globals__, 'async_call_llm', AsyncMock(side_effect=error))

    with caplog.at_level('INFO', logger='tools.vision_tools'):
        payload = json.loads(await vision_analyze_tool(str(image_path), 'Посчитай КБЖУ', 'test/model'))

    joined = '\n'.join(record.getMessage() for record in caplog.records)
    assert payload['success'] is False
    assert payload['error'] == 'Error analyzing image: request_rejected'
    assert payload['diagnostic']['category'] == 'GEMINI_ACCESS_DENIED'
    assert payload['diagnostic']['stage'] == 'PROVIDER_HTTP'
    assert payload['diagnostic']['request_was_attempted'] is True
    assert 'RAW_PROVIDER_MESSAGE_REDACTED_MARKER' not in json.dumps(payload, ensure_ascii=False)
    assert raw_marker not in joined
    assert 'vision_provider_failed' in joined
    assert 'PERMISSION_DENIED' not in joined
    assert 'Посчитай КБЖУ' not in joined


@pytest.mark.asyncio
async def test_gemini_empty_content_logs_sanitized_failure_marker(tmp_path, monkeypatch, caplog):
    from tools.vision_tools import vision_analyze_tool

    image_path = tmp_path / 'meal.png'
    image_path.write_bytes(_png_bytes())
    monkeypatch.setitem(vision_analyze_tool.__globals__, '_configured_vision_provider_and_model', lambda _model=None: ('gemini', 'gemini-2.5-flash'))
    monkeypatch.setitem(vision_analyze_tool.__globals__, 'async_call_llm', AsyncMock(return_value=object()))
    monkeypatch.setitem(vision_analyze_tool.__globals__, 'extract_content_or_reasoning', lambda _response: '')

    with (
        patch('tools.vision_tools._image_to_base64_data_url', return_value='data:image/png;base64,abc'),
        caplog.at_level('INFO', logger='tools.vision_tools'),
    ):
        payload = json.loads(await vision_analyze_tool(str(image_path), 'describe', 'test/model'))

    joined = '\n'.join(record.getMessage() for record in caplog.records)
    assert payload['success'] is False
    assert payload['diagnostic']['category'] == 'GEMINI_CONTENT_EXTRACTION_FAILURE'
    assert 'missing_text_content' in json.dumps(payload['diagnostic'])
    assert 'base64' not in joined.lower()
    assert 'stage=PROVIDER_CONTENT_EXTRACTION' in joined
