"""Tests for preserving pending Telegram updates across connect, reconnect, and conflict handling.

Regression tests for HER-8:
When the Telegram adapter starts up, reconnects after network recovery, or retries after
a polling conflict, it must NOT drop pending updates by default. Dropping pending updates
erases user messages (such as /start onboarding) that arrived while the bot was offline or
reconnecting, leaving users stranded with two checkmarks and no response.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


@pytest.fixture(autouse=True)
def _mock_telegram_environment(monkeypatch):
    """Disable auto-discovery and platform locking so connect() runs deterministically."""
    async def _noop():
        return []

    monkeypatch.setattr("gateway.platforms.telegram.discover_fallback_ips", _noop)
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    monkeypatch.setattr(
        "gateway.status.release_scoped_lock",
        lambda scope, identity: None,
    )


def _setup_mock_app(adapter: TelegramAdapter, monkeypatch):
    updater = SimpleNamespace(
        start_polling=AsyncMock(),
        start_webhook=AsyncMock(),
        stop=AsyncMock(),
        running=True,
    )
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(),
        delete_webhook=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=1, username="testbot")),
    )
    app = SimpleNamespace(
        bot=bot,
        updater=updater,
        add_handler=MagicMock(),
        initialize=AsyncMock(),
        start=AsyncMock(),
    )
    builder = MagicMock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.get_updates_request.return_value = builder
    builder.build.return_value = app
    monkeypatch.setattr(
        "gateway.platforms.telegram.Application",
        SimpleNamespace(builder=MagicMock(return_value=builder)),
    )
    return updater, bot


@pytest.mark.asyncio
async def test_connect_polling_preserves_pending_updates_by_default(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    updater, bot = _setup_mock_app(adapter, monkeypatch)

    ok = await adapter.connect()
    assert ok is True

    # By default, drop_pending_updates must be False everywhere
    assert adapter._drop_pending_updates is False
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    assert updater.start_polling.await_count == 1
    call_kwargs = updater.start_polling.call_args.kwargs
    assert call_kwargs["drop_pending_updates"] is False


@pytest.mark.asyncio
async def test_connect_polling_honors_env_var_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_DROP_PENDING_UPDATES", "true")
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    updater, bot = _setup_mock_app(adapter, monkeypatch)

    ok = await adapter.connect()
    assert ok is True

    assert adapter._drop_pending_updates is True
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
    assert updater.start_polling.await_count == 1
    call_kwargs = updater.start_polling.call_args.kwargs
    assert call_kwargs["drop_pending_updates"] is True


@pytest.mark.asyncio
async def test_connect_polling_honors_config_extra_override(monkeypatch):
    config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={"drop_pending_updates": True},
    )
    adapter = TelegramAdapter(config)
    updater, bot = _setup_mock_app(adapter, monkeypatch)

    ok = await adapter.connect()
    assert ok is True

    assert adapter._drop_pending_updates is True
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
    assert updater.start_polling.await_count == 1
    call_kwargs = updater.start_polling.call_args.kwargs
    assert call_kwargs["drop_pending_updates"] is True


@pytest.mark.asyncio
async def test_connect_webhook_preserves_pending_updates_by_default(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.com/telegram")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "supersecret")
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    updater, _ = _setup_mock_app(adapter, monkeypatch)

    ok = await adapter.connect()
    assert ok is True

    assert adapter._drop_pending_updates is False
    assert updater.start_webhook.await_count == 1
    call_kwargs = updater.start_webhook.call_args.kwargs
    assert call_kwargs["drop_pending_updates"] is False


@pytest.mark.asyncio
async def test_connect_webhook_honors_env_var_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.com/telegram")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("TELEGRAM_DROP_PENDING_UPDATES", "true")
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    updater, _ = _setup_mock_app(adapter, monkeypatch)

    ok = await adapter.connect()
    assert ok is True

    assert adapter._drop_pending_updates is True
    assert updater.start_webhook.await_count == 1
    call_kwargs = updater.start_webhook.call_args.kwargs
    assert call_kwargs["drop_pending_updates"] is True


@pytest.mark.asyncio
async def test_network_reconnect_preserves_pending_updates():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    assert adapter._drop_pending_updates is False
    adapter._polling_network_error_count = 0

    mock_updater = MagicMock()
    mock_updater.running = True
    mock_updater.stop = AsyncMock()
    mock_updater.start_polling = AsyncMock()

    mock_app = MagicMock()
    mock_app.updater = mock_updater
    adapter._app = mock_app

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await adapter._handle_polling_network_error(Exception("Connection reset"))

    assert mock_updater.start_polling.await_count == 1
    call_kwargs = mock_updater.start_polling.call_args.kwargs
    assert call_kwargs["drop_pending_updates"] is False


@pytest.mark.asyncio
async def test_conflict_retry_preserves_pending_updates():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    assert adapter._drop_pending_updates is False
    adapter._polling_conflict_count = 0

    mock_updater = MagicMock()
    mock_updater.running = True
    mock_updater.stop = AsyncMock()
    mock_updater.start_polling = AsyncMock()

    mock_app = MagicMock()
    mock_app.updater = mock_updater
    adapter._app = mock_app

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await adapter._handle_polling_conflict(Exception("Conflict"))

    assert mock_updater.start_polling.await_count == 1
    call_kwargs = mock_updater.start_polling.call_args.kwargs
    assert call_kwargs["drop_pending_updates"] is False
