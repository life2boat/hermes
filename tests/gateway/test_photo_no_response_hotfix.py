"""Regression test suite for HEALBITE_PHOTO_NO_RESPONSE_P0_HOTFIX_V1.

Covers all 12 mandatory regression scenarios:
1. Фото без подписи в публичном DM.
2. Фото орехов в скорлупе.
3. Ошибку Vision provider.
4. Таймаут Vision.
5. Невалидный JSON.
6. Ошибку загрузки фото.
7. Исключение в async photo batch.
8. Возврат result=None.
9. Ошибку отправки Telegram.
10. Повторный update и отсутствие дублей.
11. Корректную изоляцию пользователей.
12. Успешную запись КБЖУ без обязательной ручной граммовки.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.healbite_nutrition_diary import (
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    NutritionDiaryOutcome,
    NutritionRecord,
)
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.telegram import TelegramAdapter
from gateway.run import GatewayRunner, _vision_safe_fallback_text
from gateway.session import SessionSource


def _png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg=="
    )


def _make_source(user_id: int = 100, chat_id: int = 100) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=str(chat_id),
        user_id=str(user_id),
        chat_type="dm",
    )


def _make_event(source: SessionSource, image_path: str, text: str = "") -> MessageEvent:
    event = MessageEvent(
        source=source,
        text=text,
        message_id="msg-1",
        message_type=MessageType.PHOTO,
    )
    event.media_urls = [image_path]
    event.media_types = ["image/png"]
    return event


def _make_runner(tmp_path) -> tuple[GatewayRunner, AsyncMock]:
    cfg = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)},
    )
    runner = GatewayRunner(config=cfg)
    adapter = AsyncMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite_test.db", background_write=False)
    runner._get_healbite_nutrition_diary = lambda: diary
    return runner, adapter


def _seed_profile(tmp_path, user_id: int = 100) -> HealBiteUserProfileStore:
    store = HealBiteUserProfileStore(db_path=tmp_path / "profiles.db")
    store.upsert_user_profile(user_id=user_id, username="tester", daily_kcal_target=2000)
    return store



# -----------------------------------------------------------------------------
# Scenario 1: Фото без подписи в публичном DM
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_01_photo_without_caption_in_public_dm(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")
    profile_store = _seed_profile(tmp_path, user_id=201)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: profile_store)

    runner, adapter = _make_runner(tmp_path)
    runner._is_user_authorized = lambda s: False
    source = _make_source(user_id=201, chat_id=201)
    img_file = tmp_path / "meal.png"
    img_file.write_bytes(_png_bytes())
    event = _make_event(source, str(img_file), text="")

    food_payload = json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": "food_vision_inventory_v1",
            "is_food": True,
            "overall_confidence": 0.9,
            "needs_user_confirmation": False,
            "warnings": [],
            "items": [
                {
                    "visible_name": "яблоко",
                    "normalized_name": "яблоко",
                    "confidence": 0.95,
                    "estimated_grams_min": 150.0,
                    "estimated_grams_max": 200.0,
                    "preparation": "свежее",
                    "is_sauce": False,
                    "uncertainty": "",
                }
            ],
        }),
    })

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=food_payload)), \
         caplog.at_level("INFO", logger="gateway.run"):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert "Я вижу:" in sent_text
    assert "яблоко" in sent_text
    assert any("status=ANSWER_DELIVERED" in r.getMessage() and "route=healbite_nutrition" in r.getMessage() for r in caplog.records)


# -----------------------------------------------------------------------------
# Scenario 2: Фото орехов в скорлупе (оценка съедобной массы + КБЖУ с интервалом)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_02_nuts_in_shell_edible_mass_recognition(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")
    profile_store = _seed_profile(tmp_path, user_id=202)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: profile_store)

    runner, adapter = _make_runner(tmp_path)
    runner._is_user_authorized = lambda s: False
    source = _make_source(user_id=202, chat_id=202)
    img_file = tmp_path / "nuts.png"
    img_file.write_bytes(_png_bytes())
    event = _make_event(source, str(img_file), text="")

    nuts_payload = json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": "food_vision_inventory_v1",
            "is_food": True,
            "overall_confidence": 0.88,
            "needs_user_confirmation": True,
            "warnings": [],
            "items": [
                {
                    "visible_name": "грецкие орехи в скорлупе",
                    "normalized_name": "грецкие орехи",
                    "confidence": 0.88,
                    "estimated_grams_min": 30.0,
                    "estimated_grams_max": 50.0,
                    "preparation": "в скорлупе",
                    "is_sauce": False,
                    "uncertainty": "оценка ядрышек без скорлупы",
                }
            ],
        }),
    })

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=nuts_payload)), \
         caplog.at_level("INFO", logger="gateway.run"):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert "Я вижу:" in sent_text
    assert "грецкие орехи" in sent_text
    assert "30-50 г" in sent_text
    assert "оценка ядрышек без скорлупы" in sent_text



# -----------------------------------------------------------------------------
# Scenario 3: Ошибка Vision provider -> fail-closed + safe fallback
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_03_vision_provider_error_fail_closed(tmp_path, caplog):
    runner, adapter = _make_runner(tmp_path)
    runner._is_user_authorized = lambda s: False
    source = _make_source(user_id=203, chat_id=203)
    img_file = tmp_path / "err.png"
    img_file.write_bytes(_png_bytes())
    event = _make_event(source, str(img_file), text="")

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value='{"success": false, "error": "upstream model error"}')), \
         caplog.at_level("INFO", logger="gateway.run"):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == _vision_safe_fallback_text()
    assert any("status=ANSWER_DELIVERED" in r.getMessage() and "route=vision_unavailable_fallback" in r.getMessage() for r in caplog.records)


# -----------------------------------------------------------------------------
# Scenario 4: Таймаут Vision -> fallback delivery
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_04_vision_timeout_triggers_fallback(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("HEALBITE_VISION_ANALYZE_TIMEOUT", "0.05")
    runner, adapter = _make_runner(tmp_path)
    runner._is_user_authorized = lambda s: False
    source = _make_source(user_id=204, chat_id=204)
    img_file = tmp_path / "timeout.png"
    img_file.write_bytes(_png_bytes())
    event = _make_event(source, str(img_file), text="")

    async def _hanging_vision(*args, **kwargs):
        await asyncio.sleep(2.0)
        return '{"success": true}'

    with patch("tools.vision_tools.vision_analyze_tool", new=_hanging_vision), \
         caplog.at_level("INFO", logger="gateway.run"):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == _vision_safe_fallback_text()
    assert any("status=ANSWER_DELIVERED" in r.getMessage() for r in caplog.records)


# -----------------------------------------------------------------------------
# Scenario 5: Невалидный JSON -> fail-closed
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_05_invalid_vision_json_fail_closed(tmp_path, caplog):
    runner, adapter = _make_runner(tmp_path)
    runner._is_user_authorized = lambda s: False
    source = _make_source(user_id=205, chat_id=205)
    img_file = tmp_path / "invalid.png"
    img_file.write_bytes(_png_bytes())
    event = _make_event(source, str(img_file), text="")

    invalid_payload = json.dumps({
        "success": True,
        "analysis": "This is plain text instead of food_vision_inventory_v1 json",
    })

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=invalid_payload)), \
         caplog.at_level("INFO", logger="gateway.run"):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == _vision_safe_fallback_text()


# -----------------------------------------------------------------------------
# Scenario 6: Ошибка загрузки фото Telegram -> safe message
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_06_telegram_photo_download_failure(tmp_path):
    cfg = PlatformConfig(enabled=True)
    adapter = TelegramAdapter(cfg)
    adapter.send = AsyncMock()

    photo_mock = MagicMock()
    photo_mock.file_size = 100
    file_obj = AsyncMock()
    file_obj.download_as_bytearray.side_effect = RuntimeError("Telegram CDN unreachable")
    photo_mock.get_file = AsyncMock(return_value=file_obj)

    msg = MagicMock()
    msg.photo = [photo_mock]
    msg.caption = ""
    msg.chat = MagicMock(id=100)
    msg.chat.id = 100
    msg.message_thread_id = None
    msg.from_user = MagicMock(id=100)
    msg.document = None
    msg.sticker = None
    msg.voice = None
    msg.audio = None
    msg.video = None

    update = MagicMock()
    update.update_id = 901
    update.message = msg

    adapter.handle_message = AsyncMock()
    await adapter._handle_media_message(update, SimpleNamespace())

    adapter.handle_message.assert_awaited_once()
    event_arg = adapter.handle_message.await_args.args[0]
    assert "image could not be processed safely" in event_arg.text


# -----------------------------------------------------------------------------
# Scenario 7: Исключение в async photo batch -> recovery + fallback
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_07_photo_batch_exception_recovery(tmp_path, caplog):
    cfg = PlatformConfig(enabled=True)
    adapter = TelegramAdapter(cfg)
    adapter.send = AsyncMock()
    adapter._media_batch_delay_seconds = 0.01

    source = _make_source(user_id=207, chat_id=207)
    event = _make_event(source, str(tmp_path / "batch.png"))

    adapter.handle_message = AsyncMock(side_effect=RuntimeError("unexpected pipeline error"))
    adapter._pending_photo_batches["batch_key_207"] = event

    with caplog.at_level("INFO"):
        await adapter._flush_photo_batch("batch_key_207")

    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == _vision_safe_fallback_text()
    assert any("status=ANSWER_DELIVERED" in r.getMessage() and "route=photo_batch_exception" in r.getMessage() for r in caplog.records)


# -----------------------------------------------------------------------------
# Scenario 8: Возврат result=None -> fails closed
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_08_result_none_fails_closed(tmp_path, caplog):
    runner, adapter = _make_runner(tmp_path)
    runner._is_user_authorized = lambda s: False
    source = _make_source(user_id=208, chat_id=208)
    img_file = tmp_path / "none.png"
    img_file.write_bytes(_png_bytes())
    event = _make_event(source, str(img_file), text="")

    with patch.object(runner, "_try_enrich_message_with_healbite_nutrition", new=AsyncMock(return_value=("", False, None, None))), \
         caplog.at_level("INFO", logger="gateway.run"):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == _vision_safe_fallback_text()


# -----------------------------------------------------------------------------
# Scenario 9: Ошибка отправки Telegram -> логируется SEND_FAILED
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_09_telegram_send_failure_logged(tmp_path, caplog):
    runner, adapter = _make_runner(tmp_path)
    runner._is_user_authorized = lambda s: False
    source = _make_source(user_id=209, chat_id=209)
    img_file = tmp_path / "send_err.png"
    img_file.write_bytes(_png_bytes())
    event = _make_event(source, str(img_file), text="")

    # Simulate Telegram API error during fallback send
    adapter.send = AsyncMock(side_effect=ConnectionError("Telegram network failed"))

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value='{"success": false}')), \
         caplog.at_level("INFO", logger="gateway.run"):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    assert any("status=SEND_FAILED" in r.getMessage() for r in caplog.records)
    assert not any("status=ANSWER_DELIVERED" in r.getMessage() for r in caplog.records)


# -----------------------------------------------------------------------------
# Scenario 10: Повторный update и отсутствие дублей
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_10_duplicate_update_dropped_no_double_process(tmp_path, caplog):
    cfg = PlatformConfig(enabled=True)
    adapter = TelegramAdapter(cfg)
    adapter._enqueue_photo_event = MagicMock()
    adapter._cache_photo_message_to_event = AsyncMock(return_value=True)

    photo_mock = MagicMock(file_size=100)
    msg = MagicMock(
        photo=[photo_mock],
        caption="",
        chat=MagicMock(id=100),
        message_thread_id=None,
        from_user=MagicMock(id=100),
        document=None,
        sticker=None,
        voice=None,
        audio=None,
        video=None,
        media_group_id=None,
    )
    update = MagicMock(update_id=555123, message=msg)

    with caplog.at_level("INFO"):
        # First delivery
        await adapter._handle_media_message(update, SimpleNamespace())
        assert adapter._enqueue_photo_event.call_count == 1

        # Duplicate delivery
        await adapter._handle_media_message(update, SimpleNamespace())
        assert adapter._enqueue_photo_event.call_count == 1

    assert any("duplicate_update_dropped" in r.getMessage() and "555123" in r.getMessage() for r in caplog.records)


# -----------------------------------------------------------------------------
# Scenario 11: Корректная изоляция пользователей
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_11_user_isolation_between_sessions(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    inv_a = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=[FoodVisionItem(visible_name="вафли", normalized_name="вафли", confidence=0.9, estimated_grams_min=50, estimated_grams_max=70, preparation="", is_sauce=False, uncertainty="")],
        overall_confidence=0.9,
        needs_user_confirmation=False,
        warnings=[],
    )
    inv_b = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=[FoodVisionItem(visible_name="яблоко", normalized_name="яблоко", confidence=0.9, estimated_grams_min=100, estimated_grams_max=150, preparation="свежее", is_sauce=False, uncertainty="")],
        overall_confidence=0.9,
        needs_user_confirmation=False,
        warnings=[],
    )

    diary.stage_pending_inventory(user_id=101, source="vision", inventory=inv_a, image_ref="ref1", occurred_at=None)
    diary.stage_pending_inventory(user_id=102, source="vision", inventory=inv_b, image_ref="ref2", occurred_at=None)

    pending_a = diary.get_pending_inventory(101)
    pending_b = diary.get_pending_inventory(102)

    assert pending_a is not None
    assert pending_b is not None
    assert pending_a.user_id == 101
    assert pending_b.user_id == 102
    assert pending_a.items[0].visible_name == "вафли"
    assert pending_b.items[0].visible_name == "яблоко"

    # Confirming A does not affect B
    res_a = diary.save_pending_inventory_directly(101)
    assert res_a.status == "saved"
    assert diary.get_pending_inventory(101) is None
    assert diary.get_pending_inventory(102) is not None
    assert diary.get_daily_summary(user_id=101)["entry_count"] == 1
    assert diary.get_daily_summary(user_id=102)["entry_count"] == 0


# -----------------------------------------------------------------------------
# Scenario 12: Успешная запись КБЖУ без обязательной ручной граммовки
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_12_save_record_without_manual_grams(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    inv = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=[
            FoodVisionItem(
                visible_name="вафли",
                normalized_name="вафли",
                confidence=0.95,
                estimated_grams_min=50.0,
                estimated_grams_max=70.0,
                preparation="",
                is_sauce=False,
                uncertainty="",
            )
        ],
        overall_confidence=0.95,
        needs_user_confirmation=False,
        warnings=[],
    )

    pending = diary.stage_pending_inventory(
        user_id=300,
        source="vision",
        inventory=inv,
        image_ref="ref_300",
        occurred_at=None,
    )

    # Automatic proposed weight should be set
    assert pending.items[0].selected_grams == 60.0

    # User confirms directly without manual gram editing
    res = diary.save_pending_inventory_directly(300)
    assert res.status == "saved"
    assert res.record is not None
    assert res.record.calories_kcal is not None and res.record.calories_kcal > 0
    assert diary.get_pending_inventory(300) is None
    assert diary.get_daily_summary(user_id=300)["entry_count"] == 1

