from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest


def _png_bytes() -> bytes:
    return b'\x89PNG\r\n\x1a\n' + (b'\x00' * 8)


@pytest.mark.asyncio
async def test_unexpected_qwen_error_is_coarse_and_not_logged(tmp_path, monkeypatch, caplog):
    from tools.vision_tools import vision_analyze_tool

    image_path = tmp_path / 'meal.png'
    image_path.write_bytes(_png_bytes())
    raw_provider_detail = 'qwen-provider-detail-leak-marker'
    endpoint_detail = 'https://dashscope.example.invalid/v1/request'

    monkeypatch.setitem(
        vision_analyze_tool.__globals__,
        '_configured_vision_provider_and_model',
        lambda _model=None: ('alibaba', 'qwen3-vl-8b-instruct'),
    )
    monkeypatch.setitem(
        vision_analyze_tool.__globals__,
        'async_call_llm',
        AsyncMock(side_effect=RuntimeError(f'{raw_provider_detail} {endpoint_detail}')),
    )

    with caplog.at_level('DEBUG', logger='tools.vision_tools'):
        payload = json.loads(
            await vision_analyze_tool(str(image_path), 'describe image', 'qwen3-vl-8b-instruct')
        )

    joined = '\n'.join(record.getMessage() for record in caplog.records)
    assert payload == {
        'success': False,
        'error': 'Error analyzing image: unexpected_error',
        'analysis': 'There was a problem with the request and the image could not be analyzed.',
    }
    assert raw_provider_detail not in json.dumps(payload)
    assert endpoint_detail not in json.dumps(payload)
    assert raw_provider_detail not in joined
    assert endpoint_detail not in joined
    exception_text = "\n".join(
        str(record.exc_info[1])
        for record in caplog.records
        if record.exc_info and record.exc_info[1] is not None
    )
    assert raw_provider_detail not in exception_text
    assert endpoint_detail not in exception_text
    assert "vision_error_details_redacted" in exception_text
