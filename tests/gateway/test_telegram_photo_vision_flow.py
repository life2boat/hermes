import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.auxiliary_client import LLMServiceUnavailableError
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.healbite_nutrition_diary import HealBiteNutritionDiary
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.telegram import TelegramAdapter
from gateway.run import (
    GatewayRunner,
    _exec_approval_policy_for_turn,
    _filter_user_facing_toolsets_for_turn,
)
from gateway.session import SessionSource


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


def _make_document(
    *,
    file_name: str = "meal.jpg",
    mime_type: str = "image/jpeg",
    file_size: int = 64,
    file_obj=None,
):
    document = MagicMock()
    document.file_name = file_name
    document.mime_type = mime_type
    document.file_size = file_size
    document.get_file = AsyncMock(return_value=file_obj or _make_file_obj(file_path=f"documents/{file_name}"))
    return document


def _make_message(
    *,
    text: str = "",
    caption: str | None = None,
    photo=None,
    document=None,
    reply_to_message=None,
):
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
        document=document,
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
    monkeypatch.setenv("HEALBITE_DB_PATH", str(tmp_path / "healbite.db"))
    monkeypatch.setattr("gateway.healbite_nutrition_diary._GLOBAL_DIARY", None)
    yield
    monkeypatch.setattr("gateway.healbite_nutrition_diary._GLOBAL_DIARY", None)


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
async def test_reply_to_image_document_with_text_routes_to_vision_image_flow(adapter):
    original_document = _make_message(
        caption="count this meal",
        document=_make_document(file_name="meal.jpg", mime_type="image/jpeg"),
    )
    msg = _make_message(text="what is in the file?", reply_to_message=original_document)

    with patch.object(adapter, "_enqueue_text_event") as enqueue_mock:
        await adapter._handle_text_message(_make_update(msg), MagicMock())

    enqueue_mock.assert_called_once()
    event = enqueue_mock.call_args.args[0]
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls and os.path.exists(event.media_urls[0])
    assert event.media_types == ["image/jpeg"]
    assert event.text == "what is in the file?"
    assert event.reply_to_text == "count this meal"


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

SAFE_VISION_FALLBACK = (
    "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u0442\u044c \u0444\u043e\u0442\u043e \u0441\u0435\u0439\u0447\u0430\u0441. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0451 \u0440\u0430\u0437 \u043f\u043e\u0437\u0436\u0435 \u0438\u043b\u0438 \u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u0431\u043b\u044e\u0434\u043e \u0442\u0435\u043a\u0441\u0442\u043e\u043c."
)


def _make_text_only_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")},
    )
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._model = "deepseek-chat"
    runner._base_url = None
    runner._has_setup_skill = lambda: False
    runner._decide_image_input_mode = lambda: "text"
    return runner, adapter


def _runner_source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="100",
        chat_type="dm",
        user_id="1",
        user_name="Test User",
    )


def _runner_photo_event(source, *, text: str = ""):
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["/tmp/meal.png"],
        media_types=["image/png"],
    )


def _runner_document_image_event(source, *, text: str = "", media_type: str = "image/jpeg"):
    return MessageEvent(
        text=text,
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=["/tmp/meal.jpg"],
        media_types=[media_type],
    )


def _vision_success_payload(*, meal_name: str = "Борщ") -> str:
    return json.dumps(
        {
            "success": True,
            "analysis": json.dumps(
                {
                    "is_food": True,
                    "meal_name": meal_name,
                    "display_name": meal_name,
                    "raw_summary": f"{meal_name} на тарелке.",
                    "confidence": 0.84,
                    "totals": {
                        "calories_kcal": 320,
                        "protein_g": 14,
                        "fat_g": 11,
                        "carbs_g": 29,
                    },
                    "items": [{"name": meal_name}],
                },
                ensure_ascii=False,
            ),
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_runner_photo_only_skips_text_only_path_when_vision_unavailable():
    runner, adapter = _make_text_only_runner()
    source = _runner_source()
    event = _runner_photo_event(source)
    failure = json.dumps({
        "success": False,
        "error": "Provider authentication failed. Check configured credentials.",
        "analysis": "No LLM provider configured for task=vision provider=auto.",
    })

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=failure)):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert sent_text == SAFE_VISION_FALLBACK
    assert "Provider authentication failed" not in sent_text
    assert "No LLM provider configured" not in sent_text
    assert "vision_analyze" not in sent_text


@pytest.mark.asyncio
async def test_runner_photo_caption_skips_text_only_path_when_vision_unavailable():
    runner, adapter = _make_text_only_runner()
    source = _runner_source()
    event = _runner_photo_event(source, text="Count calories")

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(side_effect=RuntimeError("auth failed"))):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert sent_text == SAFE_VISION_FALLBACK
    assert "auth failed" not in sent_text
    assert "Count calories" not in sent_text


@pytest.mark.asyncio
async def test_runner_photo_skips_text_only_path_when_vision_request_is_rejected():
    runner, adapter = _make_text_only_runner()
    source = _runner_source()
    event = _runner_photo_event(source)
    failure = json.dumps(
        {
            "success": False,
            "error": "Error analyzing image: request_rejected",
            "analysis": "The vision request could not be processed right now. Please try again later or describe the meal in text.",
        }
    )

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=failure)):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert sent_text == SAFE_VISION_FALLBACK
    assert "image_url" not in sent_text
    assert "request_rejected" not in sent_text


@pytest.mark.asyncio
async def test_runner_photo_turn_ignores_old_water_history_when_vision_unavailable():
    runner, adapter = _make_text_only_runner()
    source = _runner_source()
    event = _runner_photo_event(source)
    history = [{"role": "user", "content": "????? ?????? ????"}]

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=json.dumps({
        "success": False,
        "error": "Provider authentication failed. Check configured credentials.",
        "analysis": "No LLM provider configured for task=vision provider=auto.",
    }))):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=history,
        )

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert sent_text == SAFE_VISION_FALLBACK
    assert "???" not in sent_text.lower()


@pytest.mark.asyncio
async def test_runner_photo_success_stages_pending_meal(tmp_path):
    runner, adapter = _make_text_only_runner()
    source = _runner_source()
    event = _runner_photo_event(source)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    runner._get_healbite_nutrition_diary = lambda: diary

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=_vision_success_payload())):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert "Сохранить в дневник?" in sent_text
    pending = diary.get_pending_meal(1)
    assert pending is not None
    assert pending.record.display_name == "Борщ"
    assert diary.get_daily_summary(user_id=1)["entry_count"] == 0


@pytest.mark.asyncio
async def test_runner_image_document_success_stages_pending_meal(tmp_path):
    runner, adapter = _make_text_only_runner()
    source = _runner_source()
    event = _runner_document_image_event(source, text="Посчитай КБЖУ")
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    runner._get_healbite_nutrition_diary = lambda: diary

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=_vision_success_payload(meal_name="Салат"))):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert "Сохранить в дневник?" in sent_text
    pending = diary.get_pending_meal(1)
    assert pending is not None
    assert pending.record.display_name == "Салат"
    assert diary.get_daily_summary(user_id=1)["entry_count"] == 0


@pytest.mark.asyncio
async def test_runner_photo_parse_failed_returns_safe_fallback(tmp_path):
    runner, adapter = _make_text_only_runner()
    source = _runner_source()
    event = _runner_photo_event(source)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    runner._get_healbite_nutrition_diary = lambda: diary

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new=AsyncMock(return_value=json.dumps({"success": True, "analysis": "looks tasty but no json"})),
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert sent_text == SAFE_VISION_FALLBACK
    assert diary.get_pending_meal(1) is None
    assert diary.get_daily_summary(user_id=1)["entry_count"] == 0


@pytest.mark.asyncio
async def test_vision_diagnostic_markers_are_sanitized(tmp_path, caplog, monkeypatch):
    from tools.vision_tools import vision_analyze_tool

    image_path = tmp_path / "meal.png"
    image_path.write_bytes(_png_bytes())
    monkeypatch.setattr("tools.vision_tools.safe_async_call_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        "tools.vision_tools.extract_content_or_reasoning",
        lambda _response: json.dumps({"ok": True}, ensure_ascii=False),
    )

    with caplog.at_level("INFO", logger="tools.vision_tools"):
        result = await vision_analyze_tool(str(image_path), "Посчитай КБЖУ")

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert json.loads(result)["success"] is True
    assert "vision_input_received" in joined
    assert "vision_download_ok" in joined
    assert "vision_payload_built" in joined
    assert "vision_provider_called" in joined
    assert "data:image" not in joined
    assert "base64" not in joined
    assert "Посчитай КБЖУ" not in joined
    assert str(image_path) not in joined


@pytest.mark.asyncio
async def test_vision_failure_marker_is_sanitized(tmp_path, caplog, monkeypatch):
    from tools.vision_tools import vision_analyze_tool

    image_path = tmp_path / "meal.png"
    image_path.write_bytes(_png_bytes())
    monkeypatch.setattr(
        "tools.vision_tools.safe_async_call_llm",
        AsyncMock(side_effect=LLMServiceUnavailableError("Gemini unavailable")),
    )

    with caplog.at_level("INFO", logger="tools.vision_tools"):
        result = await vision_analyze_tool(str(image_path), "Посчитай КБЖУ")

    joined = "\n".join(record.getMessage() for record in caplog.records)
    payload = json.loads(result)
    assert payload["success"] is False
    assert "vision_provider_failed" in joined
    assert "provider_unavailable" in joined
    assert "Посчитай КБЖУ" not in joined
    assert str(image_path) not in joined


def test_photo_turn_removes_terminal_and_execute_code_toolsets():
    source = _runner_source()
    event = _runner_photo_event(source, text="Count calories")

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=source,
        event=event,
        enabled_toolsets=["terminal", "code_execution", "file", "vision", "memory"],
        disabled_toolsets=[],
    )

    assert "terminal" not in enabled
    assert "code_execution" not in enabled
    assert "file" not in enabled
    assert "vision" in enabled
    assert "memory" in enabled
    assert {"terminal", "code_execution", "file"}.issubset(set(disabled))


def test_image_document_turn_removes_terminal_and_execute_code_toolsets():
    source = _runner_source()
    event = _runner_document_image_event(source, text="Count calories", media_type="image/png")

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=source,
        event=event,
        enabled_toolsets=["terminal", "code_execution", "file", "vision", "memory"],
        disabled_toolsets=[],
    )

    assert "terminal" not in enabled
    assert "code_execution" not in enabled
    assert "file" not in enabled
    assert "vision" in enabled
    assert "memory" in enabled
    assert {"terminal", "code_execution", "file"}.issubset(set(disabled))


def test_photo_turn_auto_denies_exec_approval_ui():
    source = _runner_source()
    event = _runner_photo_event(source)

    policy = _exec_approval_policy_for_turn(source=source, event=event)

    assert policy == "auto_deny"


def test_image_document_turn_auto_denies_exec_approval_ui():
    source = _runner_source()
    event = _runner_document_image_event(source, media_type="image/webp")

    policy = _exec_approval_policy_for_turn(source=source, event=event)

    assert policy == "auto_deny"


def test_text_turn_keeps_interactive_approval_policy():
    source = _runner_source()
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, source=source)

    policy = _exec_approval_policy_for_turn(source=source, event=event)

    assert policy == "interactive"
