"""Temporary inbound vision media must be private, bounded, and ephemeral."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms import base as platform_base
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cleanup_cached_image_paths,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _png_header(width: int = 1, height: int = 1) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 8
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


@pytest.fixture()
def private_image_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "private-images"
    monkeypatch.setattr(platform_base, "IMAGE_CACHE_DIR", root)
    return root


def test_image_cache_is_private_and_cleanup_is_root_bounded(
    private_image_cache: Path,
    tmp_path: Path,
) -> None:
    cached = Path(cache_image_from_bytes(_png_header(), ext=".png"))
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_header())

    assert cached.exists()
    if os.name != "nt":
        assert stat.S_IMODE(private_image_cache.stat().st_mode) == 0o700
        assert stat.S_IMODE(cached.stat().st_mode) == 0o600

    assert cleanup_cached_image_paths([str(cached), str(outside)]) == 1
    assert not cached.exists()
    assert outside.exists()


def test_image_cache_rejects_payload_without_echoing_excerpt(
    private_image_cache: Path,
) -> None:
    marker = "RAW_PAYLOAD_MUST_NOT_BE_LOGGED"
    with pytest.raises(ValueError) as exc_info:
        cache_image_from_bytes(marker.encode("utf-8"), ext=".jpg")

    assert marker not in str(exc_info.value)
    assert not list(private_image_cache.glob("*")) if private_image_cache.exists() else True


def test_image_cache_rejects_oversized_dimensions(private_image_cache: Path) -> None:
    with pytest.raises(ValueError, match="dimension"):
        cache_image_from_bytes(
            _png_header(platform_base.MAX_CACHED_IMAGE_DIMENSION + 1, 1),
            ext=".png",
        )


@pytest.mark.parametrize(
    "payload, extension",
    [
        (b"BM" + b"\x00" * 30, ".bmp"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 32, ".jpg"),
    ],
)
def test_image_cache_rejects_magic_without_parseable_positive_dimensions(
    private_image_cache: Path,
    payload: bytes,
    extension: str,
) -> None:
    with pytest.raises(ValueError, match="dimensions"):
        cache_image_from_bytes(payload, ext=extension)


class _CleanupAdapter(BasePlatformAdapter):
    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _event(path: str) -> MessageEvent:
    return MessageEvent(
        text="photo",
        message_type=MessageType.PHOTO,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="private-chat",
            chat_type="dm",
            user_id="owner",
        ),
        media_urls=[path],
        media_types=["image/png"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_fails", [False, True])
async def test_final_turn_consumer_deletes_cached_image_on_success_and_error(
    private_image_cache: Path,
    handler_fails: bool,
) -> None:
    cached = cache_image_from_bytes(_png_header(), ext=".png")
    adapter = _CleanupAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )

    async def handler(_event):
        if handler_fails:
            raise RuntimeError("synthetic failure")
        return ""

    adapter.set_message_handler(handler)
    event = _event(cached)
    await adapter._process_message_background(event, build_session_key(event.source))

    assert not Path(cached).exists()


@pytest.mark.asyncio
async def test_cancelled_turn_deletes_cached_image(private_image_cache: Path) -> None:
    cached = cache_image_from_bytes(_png_header(), ext=".png")
    adapter = _CleanupAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    entered = asyncio.Event()

    async def handler(_event):
        entered.set()
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    event = _event(cached)
    session_key = build_session_key(event.source)
    task = asyncio.create_task(
        adapter._process_message_background(event, session_key)
    )
    await entered.wait()
    adapter._expected_cancelled_tasks.add(task)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not Path(cached).exists()


@pytest.mark.asyncio
async def test_busy_handler_preserves_media_after_pending_ownership_transfer(
    private_image_cache: Path,
) -> None:
    cached = cache_image_from_bytes(_png_header(), ext=".png")
    adapter = _CleanupAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result=""))
    event = _event(cached)
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()

    async def transfer_to_pending(incoming, key):
        adapter._pending_messages[key] = incoming
        incoming._cached_media_ownership_transferred = True
        return True

    adapter.set_busy_session_handler(transfer_to_pending)
    await adapter.handle_message(event)

    assert Path(cached).exists()
    pending = adapter._pending_messages.pop(session_key)
    assert pending is event

    # The eventual pending-turn consumer owns and removes the same file.
    adapter._active_sessions.clear()
    await adapter._process_message_background(pending, session_key)
    assert not Path(cached).exists()


@pytest.mark.asyncio
async def test_busy_handler_deletes_media_when_consumed_without_transfer(
    private_image_cache: Path,
) -> None:
    cached = cache_image_from_bytes(_png_header(), ext=".png")
    adapter = _CleanupAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result=""))
    event = _event(cached)
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()

    async def consume_without_transfer(_incoming, _key):
        return True

    adapter.set_busy_session_handler(consume_without_transfer)
    await adapter.handle_message(event)

    assert not Path(cached).exists()


@pytest.mark.asyncio
async def test_runner_priority_media_survives_until_late_drain(
    private_image_cache: Path,
) -> None:
    from gateway.run import GatewayRunner

    cached = cache_image_from_bytes(_png_header(), ext=".png")
    adapter = _CleanupAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._queued_events = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    session_key = build_session_key(_event(cached).source)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(incoming):
        nonlocal calls
        if calls == 0:
            calls += 1
            assert runner._queue_or_replace_pending_event(session_key, incoming)
            return ""
        entered.set()
        await release.wait()
        return ""

    async def yield_to_queued_drain(*_args, **_kwargs):
        # Deterministically exercise the Linux scheduling order that exposed
        # the race: the child claims the shared event while the parent's
        # finalizer is suspended before deciding whether to delete its media.
        await asyncio.sleep(0)

    adapter.set_message_handler(handler)
    adapter.stop_typing = yield_to_queued_drain
    initial = _event(cached)
    task = asyncio.create_task(
        adapter._process_message_background(initial, session_key)
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    # The first task's finalizer must defer unlinking until the queued drain
    # claims the transferred event.
    assert Path(cached).exists()
    release.set()
    await task
    for drain_task in list(adapter._background_tasks):
        await drain_task
    assert not Path(cached).exists()


@pytest.mark.asyncio
async def test_prestart_cancelled_drain_cleans_media_and_session_guard(
    private_image_cache: Path,
) -> None:
    from gateway.run import GatewayRunner

    cached = cache_image_from_bytes(_png_header(), ext=".png")
    adapter = _CleanupAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._queued_events = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    initial = _event(cached)
    session_key = build_session_key(initial.source)
    created_tasks = []

    async def handler(incoming):
        assert runner._queue_or_replace_pending_event(session_key, incoming)
        return ""

    original_create = adapter._create_message_processing_task

    def create_cancelled_drain(incoming, key):
        task = original_create(incoming, key)
        task.cancel()
        created_tasks.append(task)
        return task

    adapter.set_message_handler(handler)
    adapter._create_message_processing_task = create_cancelled_drain
    await adapter._process_message_background(initial, session_key)

    assert len(created_tasks) == 1
    with pytest.raises(asyncio.CancelledError):
        await created_tasks[0]
    await asyncio.sleep(0)

    assert not Path(cached).exists()
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_prestart_cleanup_cannot_release_replacement_command_guard(
    private_image_cache: Path,
) -> None:
    cached = cache_image_from_bytes(_png_header(), ext=".png")
    adapter = _CleanupAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    event = _event(cached)
    session_key = build_session_key(event.source)
    original_guard = asyncio.Event()
    replacement_guard = asyncio.Event()
    adapter._active_sessions[session_key] = original_guard

    task = adapter._create_message_processing_task(event, session_key)
    adapter._session_tasks[session_key] = task
    task.cancel()
    adapter._active_sessions[session_key] = replacement_guard

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert not Path(cached).exists()
    assert adapter._active_sessions.get(session_key) is replacement_guard
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_busy_queue_cap_drops_media_with_single_cleanup(
    private_image_cache: Path,
) -> None:
    cached = cache_image_from_bytes(_png_header(), ext=".png")
    adapter = _CleanupAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._queued_events = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    event = _event(cached)
    session_key = build_session_key(event.source)
    for index in range(runner._BUSY_QUEUE_MAX_PENDING):
        text_event = MessageEvent(
            text=f"queued-{index}",
            source=event.source,
            message_type=MessageType.TEXT,
        )
        assert runner._queue_or_replace_pending_event(session_key, text_event)

    adapter._active_sessions[session_key] = asyncio.Event()

    async def queue_and_ack(incoming, key):
        assert runner._queue_or_replace_pending_event(key, incoming) is False
        return True

    adapter.set_busy_session_handler(queue_and_ack)
    await adapter.handle_message(event)
    assert not Path(cached).exists()
    assert runner._queue_depth(session_key, adapter=adapter) == runner._BUSY_QUEUE_MAX_PENDING


@pytest.mark.asyncio
async def test_vision_enrichment_never_persists_or_logs_cache_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_path = "C:/private/cache/owner-secret-image.png"

    async def analyze(**_kwargs):
        return json.dumps({"success": True, "analysis": "a refrigerator"})

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", analyze)
    caplog.set_level(logging.DEBUG)
    runner = object.__new__(GatewayRunner)

    enriched, success = await runner._try_enrich_message_with_vision(
        "make a menu",
        [raw_path],
    )

    assert success is True
    assert "a refrigerator" in enriched
    assert raw_path not in enriched
    assert raw_path not in caplog.text
    assert "image_url:" not in enriched


@pytest.mark.asyncio
async def test_vision_exception_log_does_not_emit_cache_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_path = "C:/private/cache/exception-secret-image.png"

    async def fail(**_kwargs):
        raise FileNotFoundError(raw_path)

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", fail)
    caplog.set_level(logging.WARNING)
    runner = object.__new__(GatewayRunner)

    _, success = await runner._try_enrich_message_with_vision("caption", [raw_path])

    assert success is False
    assert raw_path not in caplog.text
    assert "error_type=FileNotFoundError" in caplog.text
