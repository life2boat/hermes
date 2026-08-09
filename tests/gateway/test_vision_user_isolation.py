"""Telegram image batches must not cross actor/session boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms import base as platform_base
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    cache_image_from_bytes,
    cleanup_cached_image_paths,
    merge_pending_message_event,
)
from gateway.platforms.telegram import TelegramAdapter
from gateway.session import SessionSource


def _png_header() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1).to_bytes(4, "big") * 2


@pytest.fixture()
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TelegramAdapter:
    monkeypatch.setattr(platform_base, "IMAGE_CACHE_DIR", tmp_path / "images")
    instance = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    instance.MEDIA_GROUP_WAIT_SECONDS = 0.02
    instance._media_batch_delay_seconds = 0.02
    instance.handle_message = AsyncMock()
    return instance


def _event(path: str, *, user_id: str) -> MessageEvent:
    return MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="shared-group",
            chat_type="group",
            thread_id="shared-topic",
            user_id=user_id,
        ),
        media_urls=[path],
        media_types=["image/png"],
    )


def _cached() -> str:
    return cache_image_from_bytes(_png_header(), ext=".png")


def test_central_pending_media_merge_rejects_cross_actor_source(
    adapter: TelegramAdapter,
) -> None:
    first_path = _cached()
    rejected_path = _cached()
    first = _event(first_path, user_id="actor-a")
    rejected = _event(rejected_path, user_id="actor-b")
    pending: dict[str, MessageEvent] = {}

    assert merge_pending_message_event(pending, "shared", first) is True
    assert first._cached_media_ownership_transferred is True
    assert merge_pending_message_event(pending, "shared", rejected) is False
    assert pending["shared"] is first
    assert not getattr(rejected, "_cached_media_ownership_transferred", False)
    assert Path(first_path).exists()
    assert Path(rejected_path).exists()

    cleanup_cached_image_paths([first_path, rejected_path])


def test_busy_media_cross_actor_is_queued_separately(
    adapter: TelegramAdapter,
) -> None:
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._queued_events = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    first_path = _cached()
    second_path = _cached()
    session_key = "shared-session"

    assert runner._queue_or_replace_pending_event(
        session_key, _event(first_path, user_id="actor-a")
    )
    assert runner._queue_or_replace_pending_event(
        session_key, _event(second_path, user_id="actor-b")
    )
    assert adapter._pending_messages[session_key].media_urls == [first_path]
    assert runner._queued_events[session_key][0].media_urls == [second_path]
    assert runner._queue_depth(session_key, adapter=adapter) == 2
    assert adapter._pending_messages[session_key]._cached_media_ownership_transferred
    assert runner._queued_events[session_key][0]._cached_media_ownership_transferred

    cleanup_cached_image_paths([first_path, second_path])


def test_photo_burst_key_is_actor_scoped_in_shared_thread(adapter: TelegramAdapter) -> None:
    event_a = _event("/unused/a.png", user_id="actor-a")
    event_b = _event("/unused/b.png", user_id="actor-b")
    message = SimpleNamespace(media_group_id=None)

    key_a = adapter._photo_batch_key(event_a, message)
    key_b = adapter._photo_batch_key(event_b, message)

    assert key_a != key_b
    assert key_a.startswith("agent:main:telegram:group:shared-group:shared-topic:")


@pytest.mark.asyncio
async def test_same_album_id_rejects_cross_user_merge_and_deletes_rejected_file(
    adapter: TelegramAdapter,
) -> None:
    first_path = _cached()
    rejected_path = _cached()
    first = _event(first_path, user_id="actor-a")
    mismatched = _event(rejected_path, user_id="actor-b")

    await adapter._queue_media_group_event("same-album", first)
    await adapter._queue_media_group_event("same-album", mismatched)

    assert not Path(rejected_path).exists()
    assert Path(first_path).exists()
    assert len(adapter._media_group_events) == 1
    await asyncio.sleep(adapter.MEDIA_GROUP_WAIT_SECONDS + 0.03)

    adapter.handle_message.assert_awaited_once()
    delivered = adapter.handle_message.await_args.args[0]
    assert delivered.media_urls == [first_path]
    assert adapter._media_group_events == {}
    assert adapter._media_group_tasks == {}
    cleanup_cached_image_paths([first_path])


@pytest.mark.asyncio
async def test_album_debounce_replacement_keeps_new_task_tracked(
    adapter: TelegramAdapter,
) -> None:
    first_path = _cached()
    second_path = _cached()
    first = _event(first_path, user_id="actor-a")
    second = _event(second_path, user_id="actor-a")

    await adapter._queue_media_group_event("same-album", first)
    await adapter._queue_media_group_event("same-album", second)
    await asyncio.sleep(0)

    assert len(adapter._media_group_tasks) == 1
    assert len(adapter._media_group_events) == 1
    await asyncio.sleep(adapter.MEDIA_GROUP_WAIT_SECONDS + 0.03)

    adapter.handle_message.assert_awaited_once()
    delivered = adapter.handle_message.await_args.args[0]
    assert delivered.media_urls == [first_path, second_path]
    assert adapter._media_group_tasks == {}
    cleanup_cached_image_paths(delivered.media_urls)


@pytest.mark.asyncio
async def test_disconnect_deletes_unflushed_album_images(adapter: TelegramAdapter) -> None:
    adapter.MEDIA_GROUP_WAIT_SECONDS = 60
    cached = _cached()
    await adapter._queue_media_group_event(
        "pending-album",
        _event(cached, user_id="actor-a"),
    )

    await adapter.disconnect()

    assert not Path(cached).exists()
    assert adapter._media_group_events == {}
    assert adapter._media_group_tasks == {}


@pytest.mark.asyncio
async def test_disconnect_cleans_photo_popped_during_inflight_handoff(
    adapter: TelegramAdapter,
) -> None:
    entered = asyncio.Event()

    async def blocking_handle(_event):
        entered.set()
        await asyncio.Event().wait()

    adapter.handle_message = AsyncMock(side_effect=blocking_handle)
    adapter._media_batch_delay_seconds = 0
    cached = _cached()
    event = _event(cached, user_id="actor-a")
    key = adapter._photo_batch_key(
        event,
        SimpleNamespace(media_group_id=None),
    )
    adapter._enqueue_photo_event(key, event)
    await entered.wait()

    await adapter.disconnect()

    assert not Path(cached).exists()
    assert adapter._pending_photo_batches == {}
    assert adapter._pending_photo_batch_tasks == {}


@pytest.mark.asyncio
async def test_disconnect_cleans_album_popped_during_inflight_handoff(
    adapter: TelegramAdapter,
) -> None:
    entered = asyncio.Event()

    async def blocking_handle(_event):
        entered.set()
        await asyncio.Event().wait()

    adapter.handle_message = AsyncMock(side_effect=blocking_handle)
    adapter.MEDIA_GROUP_WAIT_SECONDS = 0
    cached = _cached()
    await adapter._queue_media_group_event(
        "inflight-album",
        _event(cached, user_id="actor-a"),
    )
    await entered.wait()

    await adapter.disconnect()

    assert not Path(cached).exists()
    assert adapter._media_group_events == {}
    assert adapter._media_group_tasks == {}


@pytest.mark.asyncio
async def test_cancelled_photo_flush_preserves_transferred_media(
    adapter: TelegramAdapter,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def transferring_handle(event):
        event._cached_media_ownership_transferred = True
        entered.set()
        await release.wait()

    adapter.handle_message = AsyncMock(side_effect=transferring_handle)
    adapter._media_batch_delay_seconds = 0
    cached = _cached()
    event = _event(cached, user_id='actor-a')
    key = adapter._photo_batch_key(event, SimpleNamespace(media_group_id=None))
    adapter._enqueue_photo_event(key, event)
    task = adapter._pending_photo_batch_tasks[key]

    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert Path(cached).exists()
    cleanup_cached_image_paths([cached])
    assert not Path(cached).exists()


@pytest.mark.asyncio
async def test_cancelled_album_flush_preserves_transferred_media(
    adapter: TelegramAdapter,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def transferring_handle(event):
        event._cached_media_ownership_transferred = True
        entered.set()
        await release.wait()

    adapter.handle_message = AsyncMock(side_effect=transferring_handle)
    adapter.MEDIA_GROUP_WAIT_SECONDS = 0
    cached = _cached()
    await adapter._queue_media_group_event(
        'inflight-album-transferred',
        _event(cached, user_id='actor-a'),
    )
    task = next(iter(adapter._media_group_tasks.values()))

    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert Path(cached).exists()
    cleanup_cached_image_paths([cached])
    assert not Path(cached).exists()


@pytest.mark.asyncio
async def test_unmentioned_observed_image_is_path_free_and_deleted(
    adapter: TelegramAdapter,
) -> None:
    telegram_file = AsyncMock()
    telegram_file.file_path = "photos/observed.png"
    telegram_file.download_as_bytearray = AsyncMock(
        return_value=bytearray(_png_header())
    )
    photo = SimpleNamespace(
        file_size=len(_png_header()),
        get_file=AsyncMock(return_value=telegram_file),
    )
    message = SimpleNamespace(
        photo=[photo],
        document=None,
        video=None,
        voice=None,
        audio=None,
        sticker=None,
        caption=None,
    )
    update = SimpleNamespace(message=message, update_id=7)
    observed_event = _event("/unused/pre-cache.png", user_id="actor-a")
    observed_event.media_urls = []
    observed_event.media_types = []
    captured: dict[str, object] = {}

    adapter._log_healbite_marker = lambda *args, **kwargs: None
    adapter._should_process_message = lambda *_args, **_kwargs: False
    adapter._should_observe_unmentioned_group_message = (
        lambda *_args, **_kwargs: True
    )
    adapter._build_message_event = lambda *_args, **_kwargs: observed_event

    def observe(_message, _message_type, **kwargs):
        event = kwargs["event"]
        captured["text"] = event.text
        captured["path"] = event.media_urls[0]
        assert Path(event.media_urls[0]).exists()

    adapter._observe_unmentioned_group_message = observe
    await adapter._handle_media_message(update, SimpleNamespace())

    cached_path = str(captured["path"])
    assert cached_path not in str(captured["text"])
    assert "saved at" not in str(captured["text"])
    assert not Path(cached_path).exists()
    assert observed_event.media_urls == []
    assert observed_event.media_types == []
