from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryItemInput,
    InventoryOwnerScope,
    InventorySourceType,
)
from gateway.healbite_inventory_telegram import HealBiteInventoryTelegramController
from gateway.healbite_shopping import HealBiteShoppingStore
from gateway.healbite_shopping_runtime import HealBiteShoppingRuntimeService
from gateway.healbite_shopping_telegram import build_shopping_telegram_controller
from gateway.healbite_weekly_menu_generation import (
    WeeklyMenuGenerationResult,
    WeeklyMenuGenerationStatus,
)
from gateway.healbite_weekly_menu_runtime import HealBiteWeeklyMenuRuntimeService
from gateway.healbite_weekly_menu_schema import WeeklyMenuEntryOrigin, WeeklyMenuMealSlot
from gateway.healbite_weekly_menu_telegram import (
    build_weekly_menu_telegram_controller,
    current_week_start,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuEntryInput,
    WeeklyMenuIngredientInput,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                {identity_column} INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _insert_user(db_path: Path, user_id: int, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO users ({identity_column}, username) VALUES (?, ?)",
            (int(user_id), f"user-{user_id}"),
        )


def _seed_household(db_path: Path, *, actor_user_id: int):
    _create_users_table(db_path)
    _insert_user(db_path, actor_user_id)
    store = HealBiteHouseholdStore(db_path=db_path)
    store.get_or_create_personal_household(actor_user_id)
    return store.resolve_actor_context(actor_user_id)


def test_e2e_inventory_to_weekly_menu_to_shopping_loop(tmp_path):
    db_path = tmp_path / "healbite_e2e.db"
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    week_start = current_week_start(now=now, timezone_name="UTC")

    # 1. Initialize databases & schemas
    h1 = _seed_household(db_path, actor_user_id=101)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    inv_store = HealBiteInventoryStore(db_path=db_path)
    inv_store.initialize_schema()

    gate_cfg = FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True)
    weekly_runtime = HealBiteWeeklyMenuRuntimeService(config=gate_cfg, db_path=db_path)
    shopping_runtime = HealBiteShoppingRuntimeService(config=gate_cfg, db_path=db_path)

    # 2. STEP 1: Add products at home (Inventory)
    scope = InventoryOwnerScope(household_id=h1.household_id)
    snapshot = inv_store.create_snapshot(
        scope,
        source_type=InventorySourceType.TEXT,
        items=[
            InventoryItemInput(
                display_name="Яйца",
                quantity_value="10",
                unit="piece",
                category="dairy_eggs",
                confidence=1.0,
            ),
            InventoryItemInput(
                display_name="Молоко",
                quantity_value="1",
                unit="l",
                category="dairy_eggs",
                confidence=1.0,
            ),
        ],
    )
    confirmed_inv = inv_store.confirm_snapshot(scope, snapshot.snapshot.id, expected_source_revision=snapshot.snapshot.source_revision)
    assert len(confirmed_inv.items) == 2

    # Inventory controller home screen shows confirmed items
    inv_controller = HealBiteInventoryTelegramController(
        text_config=gate_cfg,
        db_path=db_path,
        now_factory=lambda: now,
    )
    inv_last = inv_controller.handle_callback(101, "inventory:v1:l")
    assert inv_last.state == "confirmed"
    assert "Яйца" in inv_last.screen.text
    assert "Молоко" in inv_last.screen.text

    # 3. STEP 2: Create and Publish Weekly Menu
    weekly_controller = build_weekly_menu_telegram_controller(
        runtime_factory=lambda: weekly_runtime,
        shopping_runtime_factory=lambda: shopping_runtime,
        db_path=db_path,
        now_factory=lambda: now,
    )

    # Initially empty
    empty_res = weekly_controller.home(101)
    assert empty_res.state == "empty"
    assert "Меню на эту неделю пока не создано" in empty_res.screen.text

    # Mock generator creates a draft with dishes and ingredients
    def _mock_generator(actor, week, **kwargs):
        series = weekly_store.create_or_get_weekly_menu_series(h1, h1.household_id, week)
        draft = weekly_store.create_draft_revision(
            h1,
            series.id,
            expected_series_version=series.version,
            idempotency_key="draft-e2e",
        )
        draft_view = weekly_store.replace_draft_entries(
            h1,
            draft.revision.id,
            [
                WeeklyMenuEntryInput(
                    local_date=week,
                    meal_slot=WeeklyMenuMealSlot.BREAKFAST,
                    position=1,
                    title="Омлет с сыром",
                    description=None,
                    servings="2",
                    origin=WeeklyMenuEntryOrigin.GENERATED,
                    ingredients=(
                        WeeklyMenuIngredientInput(
                            display_name="Яйца",
                            quantity_value="4",
                            quantity_unit="piece",
                            recipe_base_servings="2",
                            position=1,
                        ),
                        WeeklyMenuIngredientInput(
                            display_name="Сыр",
                            quantity_value="150",
                            quantity_unit="g",
                            recipe_base_servings="2",
                            position=2,
                        ),
                    ),
                ),
                WeeklyMenuEntryInput(
                    local_date=week,
                    meal_slot=WeeklyMenuMealSlot.DINNER,
                    position=2,
                    title="Паста с томатами",
                    description=None,
                    servings="2",
                    origin=WeeklyMenuEntryOrigin.GENERATED,
                    ingredients=(
                        WeeklyMenuIngredientInput(
                            display_name="Томаты",
                            quantity_value="300",
                            quantity_unit="g",
                            recipe_base_servings="2",
                            position=1,
                        ),
                    ),
                ),
            ],
            expected_revision_version=draft.revision.version,
            idempotency_key="entries-e2e",
        )
        return WeeklyMenuGenerationResult(
            status=WeeklyMenuGenerationStatus.SUCCESS,
            revision_view=draft_view,
        )

    mock_gen = Mock()
    mock_gen.generate_draft_for_week = Mock(side_effect=_mock_generator)
    weekly_controller._generation_service_factory = lambda: mock_gen

    # Generate draft
    gen_res = weekly_controller.handle_callback(101, f"weekly_menu:v1:g:{week_start.replace('-', '')}")
    assert gen_res.state == "draft"
    assert "Омлет с сыром" in gen_res.screen.text
    assert "Паста с томатами" in gen_res.screen.text

    # Publish draft
    pub_callback = gen_res.screen.rows[0][0][1]
    pub_res = weekly_controller.handle_callback(101, pub_callback)
    assert pub_res.state == "published"
    assert "Омлет с сыром" in pub_res.screen.text

    # 4. STEP 3: Shopping List
    shopping_controller = build_shopping_telegram_controller(
        runtime_factory=lambda: shopping_runtime,
        now_factory=lambda: now,
    )

    # Shopping list is derived from the published menu
    shop_home = shopping_controller.home(101)
    assert shop_home.state == "home"
    assert "Сыр" in shop_home.screen.text or "Томаты" in shop_home.screen.text

    # Toggle an item as purchased
    item_row = [r for r in shop_home.screen.rows if len(r) == 2 and r[0][0] == "Куплено"]
    assert len(item_row) > 0
    toggle_cb = item_row[0][0][1]
    toggled_res = shopping_controller.handle_callback(101, toggle_cb, callback_query_id="cq-1")
    assert toggled_res.state == "home"
    assert "✅" in toggled_res.screen.text

    # Add a manual item
    added_res = shopping_controller.add_from_command(101, "/shopping_add Яблоки | 1 | kg", delivery_id=1001)
    assert added_res.state == "home"
    assert "Яблоки" in added_res.screen.text


def test_e2e_isolation_between_households(tmp_path):
    db_path = tmp_path / "isolation.db"
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    week_start = current_week_start(now=now, timezone_name="UTC")

    h1 = _seed_household(db_path, actor_user_id=101)
    h2 = _seed_household(db_path, actor_user_id=202)

    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    inv_store = HealBiteInventoryStore(db_path=db_path)
    inv_store.initialize_schema()

    gate_cfg = FeatureGateConfig(enabled=True, allowlist=frozenset({101, 202}), configuration_valid=True)
    weekly_runtime = HealBiteWeeklyMenuRuntimeService(config=gate_cfg, db_path=db_path)
    shopping_runtime = HealBiteShoppingRuntimeService(config=gate_cfg, db_path=db_path)

    # Seed H1 inventory
    inv_store.create_snapshot(
        InventoryOwnerScope(household_id=h1.household_id),
        source_type=InventorySourceType.TEXT,
        items=[InventoryItemInput("Чай", "1", "package")],
    )

    # Seed H1 weekly menu
    s1 = weekly_store.create_or_get_weekly_menu_series(h1, h1.household_id, week_start)
    d1 = weekly_store.create_draft_revision(h1, s1.id, expected_series_version=s1.version, idempotency_key="d1")
    entries_view = weekly_store.replace_draft_entries(
        h1,
        d1.revision.id,
        [
            WeeklyMenuEntryInput(
                local_date=week_start,
                meal_slot=WeeklyMenuMealSlot.LUNCH,
                position=1,
                title="Суп Семьи 1",
                description=None,
                servings="2",
                origin=WeeklyMenuEntryOrigin.MANUAL,
                ingredients=(),
            )
        ],
        expected_revision_version=d1.revision.version,
        idempotency_key="e1",
    )
    weekly_store.publish_weekly_menu_revision(
        h1,
        d1.revision.id,
        expected_series_version=entries_view.series.version,
        expected_revision_version=entries_view.revision.version,
        idempotency_key="p1",
    )

    # Controller for user 202
    weekly_controller = build_weekly_menu_telegram_controller(
        runtime_factory=lambda: weekly_runtime,
        db_path=db_path,
        now_factory=lambda: now,
    )

    # User 202 sees empty menu, NOT User 101's menu
    res_202 = weekly_controller.home(202)
    assert res_202.state == "empty"
    assert "Суп Семьи 1" not in res_202.screen.text

    # User 101 sees their menu
    res_101 = weekly_controller.home(101)
    assert res_101.state == "published"
    assert "Суп Семьи 1" in res_101.screen.text
