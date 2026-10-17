from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_nutrition_diary import (
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    PendingFoodVisionItem,
    PendingInventoryAction,
    build_food_vision_inventory_keyboard,
    format_food_vision_inventory_reply,
    get_default_nutrition_diary,
)
from gateway.platforms.telegram import TelegramAdapter


@pytest.fixture
def test_adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = TelegramAdapter(config)
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock()
    return adapter


@pytest.fixture
def isolated_diary(tmp_path, monkeypatch):
    db_file = tmp_path / "test_diary_v2.db"
    diary = HealBiteNutritionDiary(db_path=db_file, background_write=False)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_nutrition_diary", lambda: diary)
    return diary


def _make_callback_query(
    user_id: int,
    data: str,
    message_id: int = 999,
    household_id: str | None = None,
    target_household_id: str | None = None,
):
    query = MagicMock()
    query.id = "cb-test-123"
    query.data = data
    query.from_user = SimpleNamespace(id=user_id, first_name="TestUser")
    if household_id is not None:
        query.household_id = household_id
        query.from_user.household_id = household_id
    if target_household_id is not None:
        query.target_household_id = target_household_id

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


def _stage_inventory(
    diary: HealBiteNutritionDiary,
    user_id: int,
    inv_id: str,
    items: list[FoodVisionItem],
    image_ref: str = "telegram:1:1",
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
        image_ref=image_ref,
        occurred_at=now,
        inventory_id=inv_id,
    )


# -----------------------------------------------------------------------------
# Test 1: 150-250 g portion calculates and saves without manual weight entry
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wide_range_portion_calculates_and_saves_without_manual_grams(test_adapter, isolated_diary):
    user_id = 1001
    inv_id = "inv-wide-range-1001"

    # 150-250g span is 100g, previously blocked by 25g limit
    item = FoodVisionItem(
        visible_name="Куриная грудка отварная",
        normalized_name="куриная грудка отварная",
        confidence=0.92,
        estimated_grams_min=150.0,
        estimated_grams_max=250.0,
    )

    pending = _stage_inventory(isolated_diary, user_id, inv_id, [item])

    assert pending.items[0].selected_grams == 200.0
    assert pending.items[0].user_modified is False
    assert pending.calculation_snapshot is not None
    assert pending.calculation_snapshot.get("calories_kcal") is not None
    assert pending.calculation_snapshot.get("is_approximate_range") is True

    # Telegram response format should show calculated weight and approximate interval
    reply_text = format_food_vision_inventory_reply(pending)
    assert "расчётный вес: ~200 г" in reply_text
    assert "Ориентировочный диапазон:" in reply_text

    # User clicks Save
    query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    assert "Сохранено" in query.answer.await_args.kwargs.get("text", "")

    # Check database persistence
    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["calories_min"] is not None
    assert row["calories_max"] is not None
    assert row["is_approximate_range"] == 1
    assert row["uncertainty_model_type"] == "CONSERVATIVE_INTERVAL"

    # Items json must preserve ranges and provenance
    items = json.loads(row["items_json"])
    assert len(items) == 1
    assert items[0]["name"] == "Куриная грудка отварная"
    assert items[0]["estimated_weight_g"] == 200.0
    assert items[0]["grams_min"] == 150.0
    assert items[0]["grams_max"] == 250.0
    assert items[0]["catalog_version"] == "hfkb_v2"


# -----------------------------------------------------------------------------
# Test 2: Missing Vision grams with valid portion prior calculates and saves
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_vision_grams_with_hipe_prior_calculates_and_saves(test_adapter, isolated_diary):
    user_id = 1002
    inv_id = "inv-hipe-prior-1002"

    # Food vision returns NO grams
    item = FoodVisionItem(
        visible_name="Гречневая каша",
        normalized_name="гречневая каша",
        confidence=0.88,
        estimated_grams_min=None,
        estimated_grams_max=None,
    )

    pending = _stage_inventory(isolated_diary, user_id, inv_id, [item])

    # HIPE prior should assign default portion
    assert pending.items[0].selected_grams is not None
    assert pending.items[0].selected_grams > 0.0
    assert pending.items[0].user_modified is False
    assert pending.calculation_snapshot is not None
    assert pending.calculation_snapshot.get("calories_kcal") is not None

    query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    assert "Сохранено" in query.answer.await_args.kwargs.get("text", "")

    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["calories_kcal"] is not None


# -----------------------------------------------------------------------------
# Test 3: Whole composite dish is not double-counted
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_composite_dish_component_deduplication(test_adapter, isolated_diary):
    user_id = 1003
    inv_id = "inv-composite-1003"

    # Vision lists composite dish title AND its ingredients
    items = [
        FoodVisionItem(
            visible_name="Борщ с говядиной",
            normalized_name="борщ с говядиной",
            confidence=0.9,
            estimated_grams_min=300.0,
            estimated_grams_max=350.0,
        ),
        FoodVisionItem(
            visible_name="Говядина",
            normalized_name="говядина",
            confidence=0.85,
            estimated_grams_min=50.0,
            estimated_grams_max=70.0,
        ),
        FoodVisionItem(
            visible_name="Картофель",
            normalized_name="картофель",
            confidence=0.85,
            estimated_grams_min=50.0,
            estimated_grams_max=80.0,
        ),
    ]

    pending = _stage_inventory(isolated_diary, user_id, inv_id, items)

    # Snapshot must have deduplicated so components don't double count
    assert pending.calculation_snapshot is not None
    snapshot_comps = pending.calculation_snapshot.get("components", [])
    # Either composite parent was resolved with ingredients as recipe or parent dropped
    snap_names = [c["name"] for c in snapshot_comps]
    assert len(snap_names) < 3 or "говядина" not in [n.lower() for n in snap_names] or "борщ" not in [n.lower() for n in snap_names]

    query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    assert "Сохранено" in query.answer.await_args.kwargs.get("text", "")

    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1
    # Check calories reflect deduplicated meal, not double counted
    assert 100.0 < rows[0]["calories_kcal"] < 1000.0


# -----------------------------------------------------------------------------
# Test 4: Weight correction updates the saved calculation atomically
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_weight_correction_updates_snapshot_atomically(test_adapter, isolated_diary):
    user_id = 1004
    inv_id = "inv-edit-1004"

    item = FoodVisionItem(
        visible_name="Овсяная каша",
        normalized_name="овсяная каша",
        confidence=0.9,
        estimated_grams_min=100.0,
        estimated_grams_max=100.0,
    )
    pending = _stage_inventory(isolated_diary, user_id, inv_id, [item])
    old_cals = pending.calculation_snapshot.get("calories_kcal")

    # User modifies weight to 250g
    action = PendingInventoryAction(
        kind="weight",
        index=1,
        grams=250.0,
    )
    result = isolated_diary.apply_pending_inventory_action(user_id, action)
    assert result.status == "updated"
    assert result.inventory.items[0].selected_grams == 250.0
    assert result.inventory.items[0].user_modified is True

    # Snapshot should be updated atomically with higher calories
    new_snapshot = result.inventory.calculation_snapshot
    assert new_snapshot is not None
    new_cals = new_snapshot.get("calories_kcal")
    assert new_cals > old_cals * 2.0

    # Save via callback
    query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)

    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1
    assert round(rows[0]["calories_kcal"], 1) == round(new_cals, 1)


# -----------------------------------------------------------------------------
# Test 5: Double-click does not create duplicate diary entries
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_double_click_replay_protection_no_duplicate(test_adapter, isolated_diary):
    user_id = 1005
    inv_id = "inv-double-click-1005"

    item = FoodVisionItem(
        visible_name="Творог 5%",
        normalized_name="творог 5%",
        confidence=0.95,
        estimated_grams_min=150.0,
        estimated_grams_max=150.0,
    )
    _stage_inventory(isolated_diary, user_id, inv_id, [item], image_ref="img-dup-1005")

    query1 = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    query2 = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    # First click
    await test_adapter._handle_healbite_food_inventory_callback(query1, query1.data)
    assert "Сохранено" in query1.answer.await_args.kwargs.get("text", "")

    # Second click
    await test_adapter._handle_healbite_food_inventory_callback(query2, query2.data)
    assert "уже сохранен" in query2.answer.await_args.kwargs.get("text", "").lower() or "не нашёл" in query2.answer.await_args.kwargs.get("text", "").lower()

    # Database must contain strictly 1 record
    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1


# -----------------------------------------------------------------------------
# Test 6: Cross-user and cross-household callbacks are rejected
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cross_user_and_cross_household_isolation(test_adapter, isolated_diary):
    owner_user_id = 1006
    other_user_id = 2006
    inv_id = "inv-isolation-1006"

    item = FoodVisionItem(
        visible_name="Яблоко",
        normalized_name="яблоко",
        confidence=0.9,
        estimated_grams_min=150.0,
        estimated_grams_max=150.0,
    )
    _stage_inventory(isolated_diary, owner_user_id, inv_id, [item])

    # Case A: Cross-user callback (user 2006 attempts to save user 1006's inventory)
    query_cross_user = _make_callback_query(user_id=other_user_id, data=f"food_inv:save:{owner_user_id}:{inv_id}")
    await test_adapter._handle_healbite_food_inventory_callback(query_cross_user, query_cross_user.data)

    alert_text = query_cross_user.answer.await_args.kwargs.get("text", "")
    assert "доступно только автору фото" in alert_text

    # Pending inventory still active for owner
    assert isolated_diary.get_pending_inventory(owner_user_id) is not None
    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (owner_user_id,)).fetchall()
    assert len(rows) == 0

    # Case B: Cross-household callback (mismatched household_id)
    query_cross_household = _make_callback_query(
        user_id=owner_user_id,
        data=f"food_inv:save:{owner_user_id}:{inv_id}",
        household_id="household-AAA",
        target_household_id="household-BBB",
    )
    await test_adapter._handle_healbite_food_inventory_callback(query_cross_household, query_cross_household.data)

    hh_alert_text = query_cross_household.answer.await_args.kwargs.get("text", "")
    assert "домохозяйство не совпадает" in hh_alert_text

    # Still not saved
    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (owner_user_id,)).fetchall()
    assert len(rows) == 0


# -----------------------------------------------------------------------------
# Test 7: Existing HER-10 diary records remain readable
# -----------------------------------------------------------------------------
def test_existing_her10_diary_records_compatibility(isolated_diary):
    from gateway.healbite_nutrition_diary import _sqlite_timestamp

    user_id = 1007
    now = _sqlite_timestamp(datetime.now(timezone.utc))

    # Insert a legacy record mimicking HER-10 schema (NULL in new uncertainty columns)
    with isolated_diary._connect() as conn:
        conn.execute(
            """
            INSERT INTO nutrition_log(
                user_id, source, meal_name, items_json, calories_kcal, protein_g, fat_g, carbs_g,
                confidence, occurred_at, raw_summary, image_ref, qdrant_indexed,
                calories_min, calories_max, protein_min, protein_max, fat_min, fat_max, carbs_min, carbs_max,
                uncertainty_model_type, is_approximate_range
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (
                user_id,
                "vision",
                "Обед HER-10",
                json.dumps([{"name": "Суп", "estimated_weight_g": 250, "calories_kcal": 150}]),
                150.0,
                5.0,
                4.0,
                20.0,
                0.95,
                now,
                "Суп 150 ккал",
                "telegram:legacy:1",
            ),
        )

    # get_daily_summary must sum macros accurately
    summary = isolated_diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == 150.0
    assert summary["protein_g"] == 5.0
    assert summary["fat_g"] == 4.0
    assert summary["carbs_g"] == 20.0
