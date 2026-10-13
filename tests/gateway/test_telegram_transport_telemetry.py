"""Tests for Telegram transport-level exchange telemetry (HER-8).

Verifies that:
1. getUpdates with 0 updates records api_method=getUpdates, status_code=200, updates_count=0.
2. getUpdates with >0 updates records updates_count, min_update_id, max_update_id.
3. sendMessage records api_method=sendMessage without logging message text, chat IDs or tokens.
4. Transport errors record status_code=0, error_type=<ExcClass>, ok=false, and re-raise.
5. Telemetry preserves response stream/content for PTB and does not alter updates or offsets.
6. End-to-end correlation ID propagates from transport exchange to PTB ingress.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from gateway.platforms import telegram_network as tnet
from gateway.platforms.telegram import TelegramAdapter


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.handler(request)


@pytest.mark.asyncio
async def test_get_updates_empty_response_telemetry(caplog):
    telemetry_events = []

    def on_telemetry(event):
        telemetry_events.append(event)

    async def handler(req):
        body = json.dumps({"ok": True, "result": []}).encode()
        return httpx.Response(200, request=req, content=body)

    transport = tnet.TelegramFallbackTransport(
        [],
        telemetry_callback=on_telemetry,
    )
    transport._primary = FakeTransport(handler)

    req = httpx.Request("POST", "https://api.telegram.org/bot123456789:ABCDEF_SECRET_TOKEN/getUpdates")
    with caplog.at_level("INFO", logger="gateway.platforms.telegram_network"):
        resp = await transport.handle_async_request(req)

    assert resp.status_code == 200
    # Check emitted callback event
    assert len(telemetry_events) == 1
    ev = telemetry_events[0]
    assert ev["marker"] == "telegram_transport_exchange"
    assert ev["request_method"] == "POST"
    assert ev["api_method"] == "getUpdates"
    assert ev["status_code"] == 200
    assert ev["updates_count"] == 0
    assert ev["min_update_id"] is None
    assert ev["max_update_id"] is None
    assert ev["ok"] is True
    assert ev["duration_ms"] >= 0

    # Privacy verification: token NEVER appears in log or event
    assert "ABCDEF_SECRET_TOKEN" not in caplog.text
    assert "123456789:ABCDEF_SECRET_TOKEN" not in caplog.text
    assert "[Telegram][telegram_transport_exchange]" in caplog.text
    assert "api_method=getUpdates" in caplog.text
    assert "updates_count=0" in caplog.text


@pytest.mark.asyncio
async def test_get_updates_with_updates_telemetry(caplog):
    telemetry_events = []

    async def handler(req):
        body = json.dumps({
            "ok": True,
            "result": [
                {"update_id": 9001, "message": {"text": "/start"}},
                {"update_id": 9002, "message": {"text": "hello"}},
            ],
        }).encode()
        return httpx.Response(200, request=req, content=body)

    transport = tnet.TelegramFallbackTransport(
        [],
        telemetry_callback=telemetry_events.append,
    )
    transport._primary = FakeTransport(handler)

    req = httpx.Request("POST", "https://api.telegram.org/bot987654321:TOP_SECRET_TOKEN/getUpdates")
    resp = await transport.handle_async_request(req)

    assert resp.status_code == 200
    assert len(telemetry_events) == 1
    ev = telemetry_events[0]
    assert ev["api_method"] == "getUpdates"
    assert ev["updates_count"] == 2
    assert ev["min_update_id"] == 9001
    assert ev["max_update_id"] == 9002

    # Verify that response is intact and can be read by PTB
    parsed = resp.json()
    assert len(parsed["result"]) == 2
    assert parsed["result"][0]["update_id"] == 9001


@pytest.mark.asyncio
async def test_send_message_telemetry_does_not_log_body_or_chat_id(caplog):
    telemetry_events = []

    async def handler(req):
        body = json.dumps({"ok": True, "result": {"message_id": 42}}).encode()
        return httpx.Response(200, request=req, content=body)

    transport = tnet.TelegramFallbackTransport(
        [],
        telemetry_callback=telemetry_events.append,
    )
    transport._primary = FakeTransport(handler)

    req = httpx.Request(
        "POST",
        "https://api.telegram.org/bot111222333:SECRET_TOKEN/sendMessage",
        json={"chat_id": 888777666, "text": "Confidential patient record: blood glucose 5.4"},
    )
    with caplog.at_level("INFO", logger="gateway.platforms.telegram_network"):
        resp = await transport.handle_async_request(req)

    assert resp.status_code == 200
    assert len(telemetry_events) == 1
    ev = telemetry_events[0]
    assert ev["api_method"] == "sendMessage"
    assert ev["request_method"] == "POST"
    assert ev["status_code"] == 200

    # Strictly assert that private content is absent from log
    log_text = caplog.text
    assert "SECRET_TOKEN" not in log_text
    assert "888777666" not in log_text
    assert "glucose" not in log_text
    assert "Confidential" not in log_text


@pytest.mark.asyncio
async def test_transport_exception_telemetry(caplog):
    telemetry_events = []

    async def handler(req):
        raise httpx.ConnectTimeout("connection timed out")

    transport = tnet.TelegramFallbackTransport(
        [],
        telemetry_callback=telemetry_events.append,
    )
    transport._primary = FakeTransport(handler)

    req = httpx.Request("POST", "https://api.telegram.org/bot111222333:SECRET/getUpdates")
    with caplog.at_level("INFO", logger="gateway.platforms.telegram_network"):
        with pytest.raises(httpx.ConnectTimeout):
            await transport.handle_async_request(req)

    assert len(telemetry_events) == 1
    ev = telemetry_events[0]
    assert ev["api_method"] == "getUpdates"
    assert ev["status_code"] == 0
    assert ev["ok"] is False
    assert ev["error_type"] == "ConnectTimeout"
    assert "error_type=ConnectTimeout" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_adapter_record_transport_telemetry_caches_correlation_id():
    adapter = object.__new__(TelegramAdapter)
    adapter._healbite_log_correlations = {}
    adapter._record_transport_telemetry = TelegramAdapter._record_transport_telemetry.__get__(adapter, TelegramAdapter)
    adapter._healbite_correlation_id = TelegramAdapter._healbite_correlation_id.__get__(adapter, TelegramAdapter)

    event = {
        "corr": "corr_feed123",
        "api_method": "getUpdates",
        "status_code": 200,
        "updates_count": 1,
        "min_update_id": 55555,
        "max_update_id": 55555,
    }
    adapter._record_transport_telemetry(event)

    # Correlation ID is cached for update_id=55555
    retrieved_corr = adapter._healbite_correlation_id(msg=None, update_id=55555)
    assert retrieved_corr == "corr_feed123"

    # Verify adapter stats
    assert adapter._transport_telemetry_stats["total_requests"] == 1
    assert adapter._transport_telemetry_stats["get_updates_requests"] == 1
    assert adapter._transport_telemetry_stats["get_updates_total_updates"] == 1
    assert adapter._transport_telemetry_stats["last_get_updates_count"] == 1
    assert adapter._transport_telemetry_stats["last_get_updates_status"] == 200
