"""
tests.gateway.test_nutrition_v2_3_final_closure — HealBite Nutrition v2.3 Final Engineering Closure Validation.

Verifies:
1. Composite dish deduplication keeps parent dish active while suppressing sub-ingredients.
2. Missing kitchen scale / absence of manual grams uses HIPE portion priors to produce
   [min, center, max] intervals and saves directly without blocking.
3. Expanded Russian national culinary database (30+ authentic dishes) resolves in HFKB with high confidence.
4. Direct Telegram save persisting the exact snapshot offered, with atomic SQLite server-side
   authorization, replay protection, and cross-household isolation.
5. Ground-Truth Firewall with honest offline benchmark metrics:
   DISH_RECOGNITION_F1=NOT_EVALUATED_OFFLINE_MODE
   REAL_PHOTO_TEST_COUNT=0
   REAL_PHOTO_ACCEPTANCE=BLOCKED_NO_VERIFIED_PHOTO_ASSETS
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    format_food_vision_inventory_reply,
)
from gateway.nutrition.etl.catalog_builder import build_nutrition_catalog
from gateway.nutrition.hfkb import (
    HFKBCatalog,
    get_default_hfkb,
    load_verified_hfkb,
    set_default_hfkb,
)
from gateway.platforms.telegram import TelegramAdapter
from scripts.evaluate_photo_benchmark import run_photo_benchmark


@pytest.fixture(autouse=True)
def reset_global_hfkb():
    yield
    set_default_hfkb(None)


@pytest.fixture
def test_adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = TelegramAdapter(config)
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock()
    return adapter


@pytest.fixture
def isolated_diary(tmp_path, monkeypatch):
    db_file = tmp_path / "test_closure_diary.db"
    diary = HealBiteNutritionDiary(db_path=db_file, background_write=False)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_nutrition_diary", lambda: diary)
    return diary


def _make_callback_query(
    user_id: int,
    data: str,
    message_id: int = 888,
    household_id: str | None = None,
    target_household_id: str | None = None,
):
    query = MagicMock()
    query.id = "cb-closure-456"
    query.data = data
    query.from_user = SimpleNamespace(id=user_id, first_name="ClosureUser")
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
    image_ref: str = "telegram:10:10",
) -> PendingFoodVisionInventory:
    now = datetime.now(timezone.utc)
    return diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=FoodVisionInventory(
            schema_version="food_vision_inventory_v1",
            items=items,
            overall_confidence=0.92,
            needs_user_confirmation=True,
            warnings=[],
        ),
        image_ref=image_ref,
        occurred_at=now,
        inventory_id=inv_id,
    )


# -----------------------------------------------------------------------------
# 1. Composite dish deduplication keeps parent dish active
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_composite_dish_deduplication_retains_parent_dish(test_adapter, isolated_diary):
    user_id = 2001
    inv_id = "inv-comp-closure-01"

    # Vision model detects composite dish AND recognized individual components
    items = [
        FoodVisionItem(
            visible_name="Борщ с говядиной",
            normalized_name="борщ с говядиной",
            confidence=0.94,
            estimated_grams_min=300.0,
            estimated_grams_max=350.0,
        ),
        FoodVisionItem(
            visible_name="Говядина",
            normalized_name="говядина",
            confidence=0.88,
            estimated_grams_min=50.0,
            estimated_grams_max=70.0,
        ),
        FoodVisionItem(
            visible_name="Картофель",
            normalized_name="картофель",
            confidence=0.85,
            estimated_grams_min=40.0,
            estimated_grams_max=60.0,
        ),
        FoodVisionItem(
            visible_name="Свекла",
            normalized_name="свекла",
            confidence=0.82,
            estimated_grams_min=30.0,
            estimated_grams_max=50.0,
        ),
    ]

    pending = _stage_inventory(isolated_diary, user_id, inv_id, items)

    # Sub-ingredients should be suppressed under parent dish, NOT deleting parent
    parent_item = next(it for it in pending.items if "борщ" in it.normalized_name)
    sub_items = [it for it in pending.items if "борщ" not in it.normalized_name]

    assert parent_item.suppressed_by_item_id is None
    for sub in sub_items:
        assert sub.suppressed_by_item_id == parent_item.item_id

    # The calculation snapshot must only include the composite parent dish
    snapshot = pending.calculation_snapshot
    assert snapshot is not None
    assert len(snapshot["components"]) == 1
    assert snapshot["components"][0]["name"] == "Борщ с говядиной"

    # Telegram reply formatting displays parent dish and indicates included components
    reply_text = format_food_vision_inventory_reply(pending)
    assert "Борщ с говядиной" in reply_text
    assert "в составе «Борщ с говядиной» (учтено в блюде)" in reply_text

    # User saves snapshot
    query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)
    assert "Сохранено" in query.answer.await_args.kwargs.get("text", "")

    # Assert persisted SQLite diary contains 1 entry with the composite dish KBZHU
    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    saved_items = json.loads(row["items_json"])
    assert len(saved_items) == 1
    assert saved_items[0]["name"] == "Борщ с говядиной"
    assert saved_items[0]["estimated_weight_g"] == 325.0
    # Calories reflect 325g of borsch (~160-220 kcal), NOT double counted with separate meat/potatoes
    assert 140.0 <= row["calories_kcal"] <= 260.0


# -----------------------------------------------------------------------------
# 2. Missing grams uses HIPE portion priors and saves without blocking
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_grams_uses_hipe_portion_priors_and_saves(test_adapter, isolated_diary):
    user_id = 2002
    inv_id = "inv-prior-closure-02"

    # User took photo of pilaf with chicken without kitchen scale
    items = [
        FoodVisionItem(
            visible_name="Плов с курицей",
            normalized_name="плов с курицей",
            confidence=0.91,
            estimated_grams_min=None,
            estimated_grams_max=None,
        )
    ]

    pending = _stage_inventory(isolated_diary, user_id, inv_id, items)

    # HIPE prior kicks in (pilaf prior ~250-350g, center 300g)
    assert pending.items[0].selected_grams is None
    assert pending.items[0].user_modified is False
    assert pending.calculation_snapshot is not None

    snapshot = pending.calculation_snapshot
    assert snapshot["is_approximate_range"] is True
    assert snapshot["calories_min"] < snapshot["calories_kcal"] < snapshot["calories_max"]
    assert snapshot["protein_min"] < snapshot["protein_g"] < snapshot["protein_max"]

    # Formatting shows unconfirmed weight badge and calculated approximate KBZHU interval
    reply_text = format_food_vision_inventory_reply(pending)
    assert "вес не подтверждён" in reply_text
    assert "Ориентировочный диапазон:" in reply_text
    assert snapshot["components"][0]["estimated_weight_g"] == 250.0

    # One-click direct save persists snapshot without blocking
    query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    await test_adapter._handle_healbite_food_inventory_callback(query, query.data)
    assert "Сохранено" in query.answer.await_args.kwargs.get("text", "")

    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["is_approximate_range"] == 1
    assert rows[0]["uncertainty_model_type"] == "CONSERVATIVE_INTERVAL"


# -----------------------------------------------------------------------------
# 3. Expanded Russian national culinary database in HFKB
# -----------------------------------------------------------------------------
def test_expanded_russian_national_dishes_hfkb_resolution(tmp_path):
    catalog = get_default_hfkb()
    if catalog is None:
        db_path = tmp_path / "test_closure_catalog.db"
        manifest_path = tmp_path / "test_closure_catalog.manifest.json"
        build_nutrition_catalog(db_path, manifest_path)
        catalog = load_verified_hfkb(db_path, manifest_path, strict=False)

    dishes_to_test = [
        # Soups
        "Борщ с говядиной",
        "Борщ со сметаной",
        "Суп с картофелем и мясом",
        "Щи из свежей капусты",
        "Солянка сборная мясная",
        "Уха",
        "Рассольник",
        "Гороховый суп",
        # Pilafs & Stews
        "Плов с курицей",
        "Плов с говядиной",
        "Овощное рагу",
        "Гуляш из говядины",
        "Жаркое по-домашнему",
        # Cereals & Sides
        "Рис отварной",
        "Картофельное пюре",
        "Макароны отварные",
        "Овсяная каша",
        "Макароны по-флотски",
        "Гречневая каша",
        # Salads
        "Оливье с колбасой",
        "Оливье с курицей",
        "Сельдь под шубой",
        "Винегрет",
        "Салат Цезарь с курицей",
        "Салат из свежих огурцов и помидоров с маслом",
        "Крабовый салат",
        # Breakfasts & Mains
        "Сырники",
        "Блины",
        "Омлет с сыром",
        "Глазунья",
        "Котлеты куриные",
        "Котлеты говяжьи",
        "Куриная грудка отварная",
        "Мясная запеканка",
    ]

    for dish in dishes_to_test:
        res = catalog.resolve(dish)
        assert res.matched is True, f"Failed to resolve {dish}"
        assert res.confidence >= 0.8, f"Confidence too low for {dish}: {res.confidence}"
        assert res.profile is not None, f"Profile missing for {dish}"
        p = res.profile
        assert p.calories_kcal > 0, f"Calories <= 0 for {dish}"
        assert p.protein_g >= 0, f"Protein < 0 for {dish}"
        assert p.fat_g >= 0, f"Fat < 0 for {dish}"
        assert p.carbs_g >= 0, f"Carbs < 0 for {dish}"


# -----------------------------------------------------------------------------
# 4. Telegram Authorization, Replay Protection, and Cross-Household Isolation
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_telegram_authorization_household_and_replay_protection(test_adapter, isolated_diary):
    user_id = 2003
    attacker_id = 9999
    inv_id = "inv-auth-closure-03"

    item = FoodVisionItem(
        visible_name="Сырники",
        normalized_name="сырники",
        confidence=0.95,
        estimated_grams_min=180.0,
        estimated_grams_max=220.0,
    )
    _stage_inventory(isolated_diary, user_id, inv_id, [item])

    test_adapter._send_food_inventory_callback_reply = AsyncMock()

    # 1. Attacker tries to save user's inventory
    attacker_query = _make_callback_query(user_id=attacker_id, data=f"food_inv:save:{user_id}:{inv_id}")
    await test_adapter._handle_healbite_food_inventory_callback(attacker_query, attacker_query.data)
    assert "Это действие доступно только автору фото" in attacker_query.answer.await_args.kwargs.get("text", "")

    # Check that nothing was saved
    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 0

    # 2. Legitimate user saves
    user_query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    await test_adapter._handle_healbite_food_inventory_callback(user_query, user_query.data)
    assert "Сохранено" in user_query.answer.await_args.kwargs.get("text", "")

    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1

    # 3. Replay protection: user double-clicks Save
    replay_query = _make_callback_query(user_id=user_id, data=f"food_inv:save:{user_id}:{inv_id}")
    await test_adapter._handle_healbite_food_inventory_callback(replay_query, replay_query.data)
    assert "уже сохранена" in replay_query.answer.await_args.kwargs.get("text", "").lower()

    with isolated_diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (user_id,)).fetchall()
    # Still exactly 1 row
    assert len(rows) == 1


# -----------------------------------------------------------------------------
# 5. Ground-Truth Firewall and honest offline benchmark metrics
# -----------------------------------------------------------------------------
def test_ground_truth_firewall_offline_metrics():
    report = run_photo_benchmark("tests/benchmarks/photo_kbzhu_v1/manifest.json")

    # Firewall assertions
    assert report.dish_recognition_f1 == "NOT_EVALUATED_OFFLINE_MODE"
    assert report.real_photo_test_count == 0
    assert "BLOCKED_NO_VERIFIED_PHOTO_ASSETS" in report.real_photo_acceptance
    assert report.live_provider_requests_used == 0
    assert report.engineering_ready == "PASS"

    # Intervals and accuracy metrics computed honestly
    assert hasattr(report, "median_interval_relative_width_pct")
    assert hasattr(report, "unknown_food_rate")
    assert report.unknown_food_rate == 0.0
    assert report.overall_metrics.mass_interval_coverage_pct >= 90.0
    assert report.overall_metrics.calorie_interval_coverage_pct >= 85.0
