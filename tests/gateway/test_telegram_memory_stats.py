from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig


@pytest.mark.asyncio
async def test_memory_stats_command_is_admin_only(monkeypatch):
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={"allow_admin_from": ["999"]})
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    summary_called = {"called": False}

    def _fake_summary(*args, **kwargs):
        summary_called["called"] = True
        return {}

    monkeypatch.setattr("gateway.platforms.telegram.compute_memory_analytics_summary", _fake_summary)

    msg = SimpleNamespace(
        text="/memory_stats",
        chat=SimpleNamespace(id=111, type="private"),
        from_user=SimpleNamespace(id=111),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    assert summary_called["called"] is False
    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs["text"] == "This command is admin-only."
    adapter._ensure_forum_commands.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_stats_command_loads_admin_policy_from_runtime_config(monkeypatch):
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={})
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    monkeypatch.setattr(
        'gateway.config.load_gateway_config',
        lambda: GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    extra={"allow_admin_from": ["968323641"]},
                )
            }
        ),
    )
    monkeypatch.setattr(
        'gateway.platforms.telegram.compute_memory_analytics_summary',
        lambda *args, **kwargs: {
            "total_searches": 7,
            "qdrant_hit_rate": 57.1,
            "sqlite_fallback_rate": 42.9,
            "avg_search_latency_ms": 12.3,
            "avg_facts_injected": 1.7,
            "qdrant_hits": 4,
            "sqlite_fallbacks": 3,
        },
    )
    monkeypatch.setattr(
        'gateway.platforms.telegram.format_memory_analytics_report',
        lambda summary: (
            'Memory Analytics Report\n'
            '=======================\n'
            f"Total Memory Searches: {summary['total_searches']}"
        ),
    )

    msg = SimpleNamespace(
        text='/memory_stats',
        chat=SimpleNamespace(id=968323641, type='private'),
        from_user=SimpleNamespace(id=968323641),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs['text'] != 'This command is admin-only.'
    assert 'Total Memory Searches: 7' in kwargs['text']
    adapter._ensure_forum_commands.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_stats_command_returns_filtered_report_for_dm_admin(monkeypatch):
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={"allow_admin_from": ["111"]})
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    observed = {}

    def _fake_summary(*args, **kwargs):
        observed.update(kwargs)
        return {
            "total_searches": 5,
            "qdrant_hit_rate": 60.0,
            "sqlite_fallback_rate": 40.0,
            "avg_search_latency_ms": 18.5,
            "avg_facts_injected": 2.0,
            "qdrant_hits": 3,
            "sqlite_fallbacks": 2,
        }

    monkeypatch.setattr("gateway.platforms.telegram.compute_memory_analytics_summary", _fake_summary)
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_memory_analytics_report",
        lambda summary: (
            "Memory Analytics Report\n"
            "=======================\n"
            f"Total Memory Searches: {summary['total_searches']}"
        ),
    )

    msg = SimpleNamespace(
        text="/memory_stats --hours 24 --user-id 111",
        chat=SimpleNamespace(id=111, type="private"),
        from_user=SimpleNamespace(id=111),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    assert observed == {"user_id": 111, "hours": 24.0}
    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Total Memory Searches: 5" in kwargs["text"]
    assert "Scope: last 24h, user_id=111" in kwargs["text"]
    adapter._ensure_forum_commands.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_stats_command_uses_group_admin_scope(monkeypatch):
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={"group_allow_admin_from": ["222"]})
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    monkeypatch.setattr(
        "gateway.platforms.telegram.compute_memory_analytics_summary",
        lambda *args, **kwargs: {
            "total_searches": 1,
            "qdrant_hit_rate": 100.0,
            "sqlite_fallback_rate": 0.0,
            "avg_search_latency_ms": 9.0,
            "avg_facts_injected": 1.0,
            "qdrant_hits": 1,
            "sqlite_fallbacks": 0,
        },
    )
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_memory_analytics_report",
        lambda _summary: "Memory Analytics Report\n=======================\nTotal Memory Searches: 1",
    )

    msg = SimpleNamespace(
        text="/memory_stats",
        chat=SimpleNamespace(id=-100, type="group"),
        from_user=SimpleNamespace(id=222),
        message_thread_id=77,
    )
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs["chat_id"] == "-100"
    assert kwargs["message_thread_id"] == 77
    assert "Total Memory Searches: 1" in kwargs["text"]