from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_nutrition_diary import (
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    PendingFoodVisionItem,
    build_food_vision_inventory_keyboard,
    format_food_vision_inventory_reply,
)
from gateway.platforms.telegram import TelegramAdapter


def _make_dummy_inventory(
    user_id: int = 123,
    inv_id: str = "inv-abc-123",
    items: list[PendingFoodVisionItem] | None = None,
) -> PendingFoodVisionInventory:
    if items is None:
        items = [
            PendingFoodVisionItem(
                index=1,
                visible_name="Куриная грудка",
                normalized_name="куриная грудка",
                confidence=0.9,
                estimated_grams_min=100.0,
                estimated_grams_max=140.0,
                selected_grams=120.0,
                preparation="",
                is_sauce=False,
                uncertainty="",
                user_modified=False,
            )
        ]
    now = datetime.now(timezone.utc)
    return PendingFoodVisionInventory(
        kind="pending_inventory",
        state="pending",
        inventory_id=inv_id,
        schema_version="food_vision_inventory_v1",
        user_id=user_id,
        source="vision",
        items=items,
        overall_confidence=0.9,
        needs_user_confirmation=True,
        warnings=[],
        image_ref=None,
        occurred_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )


def _stage_inventory(
    diary: HealBiteNutritionDiary,
    user_id: int,
    inv_id: str,
    items: list[FoodVisionItem],
) -> PendingFoodVisionInventory:
    now = datetime.now(timezone.utc)
    return diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=FoodVisionInventory(
            schema_version="food_vision_inventory_v1",
            items=items,
            overall_confidence=0.9,
            needs_user_confirmation=True,
            warnings=[],
        ),
        image_ref="telegram:1:1",
        occurred_at=now,
        inventory_id=inv_id,
    )


def test_build_food_vision_inventory_keyboard_all_mapped():
    inventory = _make_dummy_inventory(user_id=456, inv_id="inv-xyz-789")
    kb = build_food_vision_inventory_keyboard(inventory)

    assert len(kb) == 2
    # Row 1: Save
    assert len(kb[0]) == 1
    assert kb[0][0]["text"] == "✅ Сохранить"
    assert kb[0][0]["callback_data"] == "food_inv:save:456:inv-xyz-789"

    # Row 2: Edit and Cancel
    assert len(kb[1]) == 2
    assert kb[1][0]["text"] == "✏️ Исправить"
    assert kb[1][0]["callback_data"] == "food_inv:edit:456:inv-xyz-789"
    assert kb[1][1]["text"] == "❌ Отмена"
    assert kb[1][1]["callback_data"] == "food_inv:cancel:456:inv-xyz-789"


def test_build_food_vision_inventory_keyboard_unmapped_component():
    unmapped_item = PendingFoodVisionItem(
        index=1,
        visible_name="Драконий фрукт питайя экзотическая",
        normalized_name="драконий фрукт питайя экзотическая",
        confidence=0.8,
        estimated_grams_min=100.0,
        estimated_grams_max=150.0,
        selected_grams=120.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        user_modified=False,
    )
    inventory = _make_dummy_inventory(user_id=789, inv_id="inv-unmapped-001", items=[unmapped_item])
    kb = build_food_vision_inventory_keyboard(inventory)

    assert len(kb) == 2
    assert kb[0][0]["text"] == "✅ Сохранить без КБЖУ"
    assert kb[0][0]["callback_data"] == "food_inv:save_no_kbzhu:789:inv-unmapped-001"


def test_weight_labels_distinguish_midpoint_from_user_modified():
    # Item 1 has auto midpoint (user_modified=False)
    item_auto = PendingFoodVisionItem(
        index=1,
        visible_name="Драники",
        normalized_name="драники",
        confidence=0.88,
        estimated_grams_min=180.0,
        estimated_grams_max=220.0,
        selected_grams=200.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        user_modified=False,
    )
    # Item 2 has user confirmed weight (user_modified=True)
    item_user = PendingFoodVisionItem(
        index=2,
        visible_name="Лосось",
        normalized_name="лосось",
        confidence=0.92,
        estimated_grams_min=80.0,
        estimated_grams_max=120.0,
        selected_grams=100.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        user_modified=True,
    )
    # Item 3 has no weight
    item_noweight = PendingFoodVisionItem(
        index=3,
        visible_name="Сыр",
        normalized_name="сыр",
        confidence=0.85,
        estimated_grams_min=None,
        estimated_grams_max=None,
        selected_grams=None,
        preparation="",
        is_sauce=False,
        uncertainty="",
        user_modified=False,
    )

    inventory = _make_dummy_inventory(items=[item_auto, item_user, item_noweight])
    reply = format_food_vision_inventory_reply(inventory)

    # Item 1 must show calculated weight
    assert "1. Драники" in reply
    assert "расчётный вес: ~200 г" in reply
    # Must NOT say confirmed for item 1
    assert "1. Драники" in reply and "вес подтверждён: 200 г" not in reply

    # Item 2 must show user-confirmed weight
    assert "2. Лосось" in reply
    assert "вес подтверждён: 100 г" in reply

    # Item 3 must show unconfirmed
    assert "3. Сыр" in reply
    assert "вес не подтверждён" in reply

    # Reply should guide user to button
    assert "Можно сохранить кнопкой ниже или отправить исправление:" in reply
    assert "• ✅ Сохранить" not in reply


@pytest.fixture
def test_adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = TelegramAdapter(config)
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock()
    return adapter


@pytest.fixture
def isolated_diary(tmp_path, monkeypatch):
    db_file = tmp_path / "test_diary.db"
    diary = HealBiteNutritionDiary(db_path=db_file, background_write=False)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_nutrition_diary", lambda: diary)
    return diary


def _make_callback_query(user_id: int, data: str, message_id: int = 999):
    query = MagicMock()
    query.id = "cb-123"
    query.data = data
    query.from_user = SimpleNamespace(id=user_id, first_name="TestUser")
    msg = MagicMock()
    msg.message_id = message_id
    msg.chat = SimpleNamespace(id=user_id, type="private")
    msg.chat_id = user_id
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    query.message = msg
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


@pytest.mark.asyncio
async def test_food_inventory_callback_save_success(test_adapter, isolated_diary):
    user_id = 101
    inventory_id = "inv-save-success-1"

    _stage_inventory(
        isolated_diary,
        user_id=user_id,
        inv_id=inventory_id,
        items=[
            FoodVisionItem(
                visible_name="Овсяная каша",
                normalized_name="овсяная каша",
                confidence=0.9,
                estimated_grams_min=200,
                estimated_grams_max=200,
            )
        ],
    )

    query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inventory_id}")

    test_adapter._send_food_inventory_callback_reply = AsyncMock()
    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    # 1. Markup removed
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    # 2. Answered callback
    query.answer.assert_awaited_once_with(text="Сохранено в дневник!")
    # 3. Confirmation message sent
    test_adapter._send_food_inventory_callback_reply.assert_awaited_once()
    reply_text = test_adapter._send_food_inventory_callback_reply.await_args.args[1]
    assert "Сохранено" in reply_text

    # 4. Pending state cleared in DB
    assert isolated_diary.get_pending_inventory(user_id) is None
    # 5. Saved record exists in diary
    summary = isolated_diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 1


@pytest.mark.asyncio
async def test_food_inventory_callback_save_no_kbzhu_success(test_adapter, isolated_diary):
    user_id = 102
    inventory_id = "inv-unmapped-success"

    _stage_inventory(
        isolated_diary,
        user_id=user_id,
        inv_id=inventory_id,
        items=[
            FoodVisionItem(
                visible_name="Драконий фрукт",
                normalized_name="драконий фрукт",
                confidence=0.9,
                estimated_grams_min=150,
                estimated_grams_max=150,
            )
        ],
    )

    query = _make_callback_query(user_id=user_id, data=f"food_inv:save_no_kbzhu:{user_id}:{inventory_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query.answer.assert_awaited_once_with(text="Сохранено в дневник!")
    test_adapter._send_food_inventory_callback_reply.assert_awaited_once()
    assert isolated_diary.get_pending_inventory(user_id) is None
    assert isolated_diary.get_daily_summary(user_id=user_id)["entry_count"] == 1


@pytest.mark.asyncio
async def test_food_inventory_callback_replay_protection_no_duplicate(test_adapter, isolated_diary):
    user_id = 103
    inventory_id = "inv-replay-1"

    _stage_inventory(
        isolated_diary,
        user_id=user_id,
        inv_id=inventory_id,
        items=[
            FoodVisionItem(
                visible_name="Яйцо вареное",
                normalized_name="яйцо вареное",
                confidence=0.9,
                estimated_grams_min=60,
                estimated_grams_max=60,
            )
        ],
    )

    query1 = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inventory_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    # First click
    await test_adapter._handle_healbite_food_inventory_callback(query1, query1.data)
    assert query1.answer.await_args.kwargs.get("text") == "Сохранено в дневник!"
    assert isolated_diary.get_daily_summary(user_id=user_id)["entry_count"] == 1

    # Second click (replay / stale button)
    query2 = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inventory_id}")
    await test_adapter._handle_healbite_food_inventory_callback(query2, query2.data)
    assert "Запись уже сохранена или отменена" in query2.answer.await_args.kwargs.get("text", "")
    assert query2.answer.await_args.kwargs.get("show_alert") is True
    # Still exactly 1 entry, NO duplicate!
    assert isolated_diary.get_daily_summary(user_id=user_id)["entry_count"] == 1


@pytest.mark.asyncio
async def test_food_inventory_callback_cross_user_isolation(test_adapter, isolated_diary):
    user_a = 201
    user_b = 202
    inventory_id = "inv-user-a"

    _stage_inventory(
        isolated_diary,
        user_id=user_a,
        inv_id=inventory_id,
        items=[
            FoodVisionItem(
                visible_name="Творог",
                normalized_name="творог",
                confidence=0.9,
                estimated_grams_min=100,
                estimated_grams_max=100,
            )
        ],
    )

    # User B clicks User A's button
    query_b = _make_callback_query(user_id=user_b, data=f"food_inv:save:{user_a}:{inventory_id}")
    await test_adapter._handle_healbite_food_inventory_callback(query_b, query_b.data)

    # Denied with alert
    assert "Это действие доступно только автору фото" in query_b.answer.await_args.kwargs.get("text", "")
    assert query_b.answer.await_args.kwargs.get("show_alert") is True

    # User A's inventory is still pending and untouched
    pending_a = isolated_diary.get_pending_inventory(user_a)
    assert pending_a is not None
    assert pending_a.inventory_id == inventory_id
    assert isolated_diary.get_daily_summary(user_id=user_a)["entry_count"] == 0


@pytest.mark.asyncio
async def test_food_inventory_callback_stale_inventory_rejected(test_adapter, isolated_diary):
    user_id = 301
    inv_old = "inv-old-1"
    inv_new = "inv-new-2"

    # User stages old photo
    _stage_inventory(
        isolated_diary,
        user_id=user_id,
        inv_id=inv_old,
        items=[
            FoodVisionItem(
                visible_name="Яблоко",
                normalized_name="яблоко",
                confidence=0.9,
                estimated_grams_min=150,
                estimated_grams_max=150,
            )
        ],
    )

    # User stages new photo, replacing the old one
    _stage_inventory(
        isolated_diary,
        user_id=user_id,
        inv_id=inv_new,
        items=[
            FoodVisionItem(
                visible_name="Банан",
                normalized_name="банан",
                confidence=0.9,
                estimated_grams_min=120,
                estimated_grams_max=120,
            )
        ],
    )

    # User clicks old button
    query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_old}")
    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    # Rejected as stale
    assert "Это подтверждение уже заменено новым фото" in query.answer.await_args.kwargs.get("text", "")
    assert query.answer.await_args.kwargs.get("show_alert") is True
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    # The new inventory is still intact!
    pending = isolated_diary.get_pending_inventory(user_id)
    assert pending is not None
    assert pending.inventory_id == inv_new


@pytest.mark.asyncio
async def test_food_inventory_callback_cancel(test_adapter, isolated_diary):
    user_id = 401
    inventory_id = "inv-cancel-test"

    _stage_inventory(
        isolated_diary,
        user_id=user_id,
        inv_id=inventory_id,
        items=[
            FoodVisionItem(
                visible_name="Омлет",
                normalized_name="омлет",
                confidence=0.9,
                estimated_grams_min=150,
                estimated_grams_max=150,
            )
        ],
    )

    query = _make_callback_query(user_id=user_id, data=f"food_inv:cancel:{user_id}:{inventory_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query.answer.assert_awaited_once_with(text="Отменено.")
    test_adapter._send_food_inventory_callback_reply.assert_awaited_once()
    assert "Отменено" in test_adapter._send_food_inventory_callback_reply.await_args.args[1]

    # Pending state cleared
    assert isolated_diary.get_pending_inventory(user_id) is None


@pytest.mark.asyncio
async def test_food_inventory_callback_edit(test_adapter, isolated_diary):
    user_id = 501
    inventory_id = "inv-edit-test"

    _stage_inventory(
        isolated_diary,
        user_id=user_id,
        inv_id=inventory_id,
        items=[
            FoodVisionItem(
                visible_name="Салат",
                normalized_name="салат",
                confidence=0.9,
                estimated_grams_min=100,
                estimated_grams_max=100,
            )
        ],
    )

    query = _make_callback_query(user_id=user_id, data=f"food_inv:edit:{user_id}:{inventory_id}")

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    # Answers alert with edit instructions
    alert_text = query.answer.await_args.kwargs.get("text", "")
    assert "Отправьте исправление" in alert_text
    assert query.answer.await_args.kwargs.get("show_alert") is True
    # Markup is NOT removed so user can still save or cancel later
    query.edit_message_reply_markup.assert_not_called()

    # Pending state is still intact
    assert isolated_diary.get_pending_inventory(user_id) is not None


@pytest.mark.asyncio
async def test_telegram_send_converts_metadata_markup_to_inline_keyboard(test_adapter):
    raw_markup = [
        [{"text": "✅ Сохранить", "callback_data": "food_inv:save:1:inv-1"}],
        [{"text": "❌ Отмена", "callback_data": "food_inv:cancel:1:inv-1"}],
    ]
    test_adapter._bot.send_message = AsyncMock()

    await test_adapter.send(
        chat_id="12345",
        content="Test content",
        metadata={"reply_markup": raw_markup, "parse_mode": "HTML"},
    )

    test_adapter._bot.send_message.assert_awaited_once()
    kwargs = test_adapter._bot.send_message.await_args.kwargs
    assert "reply_markup" in kwargs
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_runner_photo_success_attaches_inline_keyboard(tmp_path):
    from tests.gateway.test_telegram_photo_vision_flow import (
        _make_text_only_runner,
        _runner_photo_event,
        _runner_source,
        _vision_success_payload,
    )

    runner, adapter = _make_text_only_runner()
    source = _runner_source()
    event = _runner_photo_event(source)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    runner._get_healbite_nutrition_diary = lambda: diary

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new=AsyncMock(return_value=_vision_success_payload(meal_name="Лосось")),
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is None
    adapter.send.assert_awaited_once()
    kwargs = adapter.send.await_args.kwargs
    metadata = kwargs.get("metadata")
    assert metadata is not None
    assert "reply_markup" in metadata
    markup = metadata["reply_markup"]
    assert len(markup) == 2
    assert markup[0][0]["text"] == "✅ Сохранить"
    assert markup[0][0]["callback_data"].startswith("food_inv:save:1:")

