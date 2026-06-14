import base64
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from gateway.platforms.telegram import TelegramAdapter


def _png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg=="
    )


def _make_file_obj(data: bytes | None = None, file_path: str = "photos/meal.png"):
    file_obj = AsyncMock()
    file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(data or _png_bytes()))
    file_obj.file_path = file_path
    return file_obj


def _make_photo(file_obj=None):
    photo = MagicMock()
    photo.get_file = AsyncMock(return_value=file_obj or _make_file_obj())
    return photo


def _make_message(*, text: str = "", caption: str | None = None, photo=None, reply_to_message=None):
    chat = SimpleNamespace(id=100, type="private", title=None, full_name="Test User", is_forum=False)
    user = SimpleNamespace(id=1, full_name="Test User")
    return SimpleNamespace(
        message_id=42,
        text=text,
        caption=caption,
        date=None,
        photo=photo,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        document=None,
        media_group_id=None,
        chat=chat,
        from_user=user,
        message_thread_id=None,
        is_topic_message=False,
        reply_to_message=reply_to_message,
        quote=None,
    )


def _make_update(msg):
    return SimpleNamespace(message=msg, effective_message=msg, update_id=123)


@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    a = TelegramAdapter(config)
    a.handle_message = AsyncMock()
    return a


@pytest.fixture(autouse=True)
def _redirect_image_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.platforms.base.IMAGE_CACHE_DIR", tmp_path / "image_cache")


@pytest.mark.asyncio
async def test_photo_only_routes_to_vision_image_flow(adapter):
    msg = _make_message(photo=[_make_photo()])

    with patch.object(adapter, "_photo_batch_key", return_value="batch-1"), patch.object(
        adapter, "_enqueue_photo_event"
    ) as enqueue_mock:
        await adapter._handle_media_message(_make_update(msg), MagicMock())

    enqueue_mock.assert_called_once()
    event = enqueue_mock.call_args.args[1]
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls and os.path.exists(event.media_urls[0])
    assert event.media_types == ["image/png"]
    assert event.text == ""
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_photo_with_caption_routes_to_vision_image_flow(adapter):
    msg = _make_message(caption="Count calories", photo=[_make_photo()])

    with patch.object(adapter, "_photo_batch_key", return_value="batch-2"), patch.object(
        adapter, "_enqueue_photo_event"
    ) as enqueue_mock:
        await adapter._handle_media_message(_make_update(msg), MagicMock())

    event = enqueue_mock.call_args.args[1]
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls and os.path.exists(event.media_urls[0])
    assert event.media_types == ["image/png"]
    assert event.text == "Count calories"


@pytest.mark.asyncio
async def test_reply_to_photo_with_text_routes_to_vision_image_flow(adapter):
    original_photo = _make_message(caption="my lunch", photo=[_make_photo()])
    msg = _make_message(text="what is in the photo?", reply_to_message=original_photo)

    with patch.object(adapter, "_enqueue_text_event") as enqueue_mock:
        await adapter._handle_text_message(_make_update(msg), MagicMock())

    enqueue_mock.assert_called_once()
    event = enqueue_mock.call_args.args[0]
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls and os.path.exists(event.media_urls[0])
    assert event.media_types == ["image/png"]
    assert event.text == "what is in the photo?"
    assert event.reply_to_text == "my lunch"


@pytest.mark.asyncio
async def test_text_only_reply_does_not_attach_image_without_photo(adapter):
    original_text = _make_message(text="????my lunch?")
    msg = _make_message(text="reply", reply_to_message=original_text)

    with patch.object(adapter, "_enqueue_text_event") as enqueue_mock:
        await adapter._handle_text_message(_make_update(msg), MagicMock())

    event = enqueue_mock.call_args.args[0]
    assert event.message_type == MessageType.TEXT
    assert event.media_urls == []
    assert event.media_types == []
