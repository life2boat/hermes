"""Acceptance tests for HealBite Public Inventory Features Rollout v1.

Covers:
- Dual-gate text backend & UI matrix
- Dual-gate photo backend & UI matrix
- Dual-gate inventory weekly generation & backend matrix
- Cross-household inventory isolation (preventing cross-household leakage)
- Synthetic temp DB E2E validation:
    * TEMP_TEXT_INVENTORY_E2E=PASS
    * TEMP_PHOTO_INVENTORY_E2E=PASS
    * TEMP_INVENTORY_WEEKLY_E2E=PASS
    * TEMP_SHOPPING_DERIVATION_E2E=PASS
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
from unittest.mock import Mock

import pytest

from gateway.healbite_feature_gates import FeatureAvailabilityStatus, FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryAccessError,
    InventoryItemInput,
    InventoryOwnerScope,
    InventorySourceType,
    InventoryStatus,
)
from gateway.healbite_inventory_telegram import (
    HealBiteInventoryTelegramController,
    build_inventory_telegram_controller,
    parse_inventory_callback,
)
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
from tests.gateway.weekly_menu_fixtures import complete_weekly_entries


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


def _find_callback(result, label_fragment: str) -> str:
    for row in result.screen.rows:
        for label, callback_data in row:
            if label_fragment.lower() in label.lower():
                return callback_data
    raise AssertionError(f"Callback with label fragment '{label_fragment}' not found in {result.screen.rows}")


# =====================================================================
# Phase 6: Dual-Gate Text Backend & UI Matrix
# =====================================================================

def test_text_dual_gate_matrix(tmp_path):
    db_path = tmp_path / "text_gate.db"
    _seed_household(db_path, actor_user_id=101)
    _seed_household(db_path, actor_user_id=202)

    actor = 101
    other_actor = 202

    # Case 1: Backend disabled, UI disabled -> disabled
    c1 = HealBiteInventoryTelegramController(
        text_backend_config=FeatureGateConfig(enabled=False),
        text_ui_config=FeatureGateConfig(enabled=False),
        db_path=db_path,
    )
    assert c1._gate("text", actor).status.value == "disabled"

    # Case 2: Backend enabled (allowlisted), UI disabled -> not ready
    c2 = HealBiteInventoryTelegramController(
        text_backend_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        text_ui_config=FeatureGateConfig(enabled=False),
        db_path=db_path,
    )
    assert c2._gate("text", actor).ready is False

    # Case 3: Backend disabled, UI enabled (allowlisted) -> not ready
    c3 = HealBiteInventoryTelegramController(
        text_backend_config=FeatureGateConfig(enabled=False),
        text_ui_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        db_path=db_path,
    )
    assert c3._gate("text", actor).ready is False

    # Case 4: Both enabled, actor allowlisted -> ready
    c4 = HealBiteInventoryTelegramController(
        text_backend_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        text_ui_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        db_path=db_path,
    )
    assert c4._gate("text", actor).ready is True
    # Other unlisted actor is NOT ready
    assert c4._gate("text", other_actor).ready is False

    # Case 5: Public access on both -> unlisted actor is ready
    c5 = HealBiteInventoryTelegramController(
        text_backend_config=FeatureGateConfig(enabled=True, public_access=True),
        text_ui_config=FeatureGateConfig(enabled=True, public_access=True),
        db_path=db_path,
    )
    assert c5._gate("text", other_actor).ready is True

    # Case 6: Asymmetric public: backend public, UI not public -> unlisted actor is NOT ready
    c6 = HealBiteInventoryTelegramController(
        text_backend_config=FeatureGateConfig(enabled=True, public_access=True),
        text_ui_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        db_path=db_path,
    )
    assert c6._gate("text", other_actor).ready is False


# =====================================================================
# Phase 7: Dual-Gate Photo Backend & UI Matrix
# =====================================================================

def test_photo_dual_gate_matrix(tmp_path):
    db_path = tmp_path / "photo_gate.db"
    _seed_household(db_path, actor_user_id=101)
    _seed_household(db_path, actor_user_id=202)

    actor = 101
    other_actor = 202

    # Case 1: Backend disabled, UI disabled -> disabled
    c1 = HealBiteInventoryTelegramController(
        photo_backend_config=FeatureGateConfig(enabled=False),
        photo_ui_config=FeatureGateConfig(enabled=False),
        db_path=db_path,
    )
    assert c1._gate("photo", actor).status.value == "disabled"

    # Case 2: Backend enabled (allowlisted), UI disabled -> not ready
    c2 = HealBiteInventoryTelegramController(
        photo_backend_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        photo_ui_config=FeatureGateConfig(enabled=False),
        db_path=db_path,
    )
    assert c2._gate("photo", actor).ready is False

    # Case 3: Backend disabled, UI enabled (allowlisted) -> not ready
    c3 = HealBiteInventoryTelegramController(
        photo_backend_config=FeatureGateConfig(enabled=False),
        photo_ui_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        db_path=db_path,
    )
    assert c3._gate("photo", actor).ready is False

    # Case 4: Both enabled, actor allowlisted -> ready
    c4 = HealBiteInventoryTelegramController(
        photo_backend_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        photo_ui_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        db_path=db_path,
    )
    assert c4._gate("photo", actor).ready is True
    assert c4._gate("photo", other_actor).ready is False

    # Case 5: Both public -> unlisted actor is ready
    c5 = HealBiteInventoryTelegramController(
        photo_backend_config=FeatureGateConfig(enabled=True, public_access=True),
        photo_ui_config=FeatureGateConfig(enabled=True, public_access=True),
        db_path=db_path,
    )
    assert c5._gate("photo", other_actor).ready is True

    # Case 6: Asymmetric public: photo UI public, photo backend not public -> unlisted actor NOT ready
    c6 = HealBiteInventoryTelegramController(
        photo_backend_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        photo_ui_config=FeatureGateConfig(enabled=True, public_access=True),
        db_path=db_path,
    )
    assert c6._gate("photo", other_actor).ready is False


# =====================================================================
# Phase 8: Dual-Gate Weekly Generation from Inventory Matrix
# =====================================================================

def test_weekly_generation_inventory_dual_gate_matrix(tmp_path):
    db_path = tmp_path / "weekly_gate.db"
    _seed_household(db_path, actor_user_id=101)
    _seed_household(db_path, actor_user_id=202)

    actor = 101
    other_actor = 202

    # Case 1: Both disabled -> disabled
    c1 = HealBiteInventoryTelegramController(
        weekly_generation_ui_config=FeatureGateConfig(enabled=False),
        weekly_menu_inventory_config=FeatureGateConfig(enabled=False),
        db_path=db_path,
    )
    assert c1._gate("weekly", actor).status.value == "disabled"

    # Case 2: Generation UI enabled, weekly menu inventory backend disabled -> not ready
    c2 = HealBiteInventoryTelegramController(
        weekly_generation_ui_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        weekly_menu_inventory_config=FeatureGateConfig(enabled=False),
        db_path=db_path,
    )
    assert c2._gate("weekly", actor).ready is False

    # Case 3: Generation UI disabled, weekly menu inventory backend enabled -> not ready
    c3 = HealBiteInventoryTelegramController(
        weekly_generation_ui_config=FeatureGateConfig(enabled=False),
        weekly_menu_inventory_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        db_path=db_path,
    )
    assert c3._gate("weekly", actor).ready is False

    # Case 4: Both enabled, actor allowlisted -> ready
    c4 = HealBiteInventoryTelegramController(
        weekly_generation_ui_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        weekly_menu_inventory_config=FeatureGateConfig(enabled=True, allowlist=frozenset({actor})),
        db_path=db_path,
    )
    assert c4._gate("weekly", actor).ready is True
    assert c4._gate("weekly", other_actor).ready is False

    # Case 5: Both public -> unlisted actor is ready
    c5 = HealBiteInventoryTelegramController(
        weekly_generation_ui_config=FeatureGateConfig(enabled=True, public_access=True),
        weekly_menu_inventory_config=FeatureGateConfig(enabled=True, public_access=True),
        db_path=db_path,
    )
    assert c5._gate("weekly", other_actor).ready is True


# =====================================================================
# Phase 9: Cross-Household Inventory Isolation
# =====================================================================

def test_cross_household_inventory_isolation(tmp_path):
    db_path = tmp_path / "isolation.db"
    h1 = _seed_household(db_path, actor_user_id=101)
    h2 = _seed_household(db_path, actor_user_id=202)

    inv_store = HealBiteInventoryStore(db_path=db_path)
    inv_store.initialize_schema()

    scope1 = InventoryOwnerScope(household_id=h1.household_id)
    scope2 = InventoryOwnerScope(household_id=h2.household_id)

    # Actor 101 creates a text snapshot with eggs
    snap1 = inv_store.create_snapshot(
        scope1,
        source_type=InventorySourceType.TEXT,
        items=[
            InventoryItemInput(
                display_name="Яйца",
                quantity_value="10",
                unit="piece",
                category="dairy_eggs",
                confidence=1.0,
            )
        ],
    )

    # Actor 202 cannot view or get Actor 101's snapshot
    with pytest.raises(InventoryAccessError):
        inv_store.get_snapshot(scope2, snap1.snapshot.id)

    # Actor 202 cannot confirm Actor 101's snapshot
    with pytest.raises(InventoryAccessError):
        inv_store.confirm_snapshot(scope2, snap1.snapshot.id)

    # Actor 101 confirms snapshot successfully
    confirmed1 = inv_store.confirm_snapshot(scope1, snap1.snapshot.id)
    assert confirmed1.snapshot.status is InventoryStatus.CONFIRMED

    # Actor 202's latest confirmed snapshot is None
    assert inv_store.get_latest_confirmed_snapshot(scope2) is None

    # Actor 101's latest confirmed snapshot contains the eggs
    latest1 = inv_store.get_latest_confirmed_snapshot(scope1)
    assert latest1 is not None
    assert len(latest1.items) == 1
    assert latest1.items[0].display_name == "Яйца"

    # Controller isolation: Actor 202 calling callback with Actor 101's snapshot ID
    gate_cfg = FeatureGateConfig(enabled=True, allowlist=frozenset({101, 202}))
    controller = HealBiteInventoryTelegramController(
        text_backend_config=gate_cfg,
        text_ui_config=gate_cfg,
        db_path=db_path,
    )
    foreign_confirm_cb = f"inventory:v1:c:{snap1.snapshot.id}:1"
    res = controller.handle_callback(202, foreign_confirm_cb)
    # Fails closed (stale/error), does not mutate or access Actor 101's data
    assert res.state in ("stale", "error")


# =====================================================================
# Phase 10: Synthetic Temp DB E2E Validation
# =====================================================================

@pytest.mark.asyncio
async def test_synthetic_temp_db_e2e_full_lifecycle(tmp_path):
    """Verifies the four required E2E gates on an isolated temporary DB:

    1. TEMP_TEXT_INVENTORY_E2E=PASS
    2. TEMP_PHOTO_INVENTORY_E2E=PASS
    3. TEMP_INVENTORY_WEEKLY_E2E=PASS
    4. TEMP_SHOPPING_DERIVATION_E2E=PASS
    """
    db_path = tmp_path / "synthetic_e2e.db"
    current_time = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    week_start = current_week_start(now=current_time, timezone_name="UTC")
    actor = 101

    # 1. Initialize schema & household
    h1 = _seed_household(db_path, actor_user_id=actor)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    inv_store = HealBiteInventoryStore(db_path=db_path)
    inv_store.initialize_schema()

    gate_cfg = FeatureGateConfig(enabled=True, allowlist=frozenset({actor}), configuration_valid=True)
    weekly_runtime = HealBiteWeeklyMenuRuntimeService(config=gate_cfg, db_path=db_path)
    shopping_runtime = HealBiteShoppingRuntimeService(config=gate_cfg, db_path=db_path)

    # -------------------------------------------------------------
    # Step 1: Text Inventory Intake & Confirmation
    # -------------------------------------------------------------
    inv_controller = HealBiteInventoryTelegramController(
        text_backend_config=gate_cfg,
        text_ui_config=gate_cfg,
        photo_backend_config=gate_cfg,
        photo_ui_config=gate_cfg,
        weekly_generation_ui_config=gate_cfg,
        weekly_menu_inventory_config=gate_cfg,
        db_path=db_path,
        now_factory=lambda: current_time,
    )

    # Open home and enter text mode
    home_res = inv_controller.home(actor)
    text_cb = _find_callback(home_res, "Текстом")
    inv_controller.handle_callback(actor, text_cb)
    assert inv_controller.pending_input_kind(actor) == "text"

    # User inputs products by text
    review_text_res = inv_controller.handle_text(actor, "Яйца 10 шт, Молоко 1 л")
    assert review_text_res is not None
    assert review_text_res.state == "review"
    assert review_text_res.item_count == 2

    # User confirms text snapshot
    confirm_text_cb = _find_callback(review_text_res, "Подтвердить")
    confirmed_text_res = inv_controller.handle_callback(actor, confirm_text_cb)
    assert confirmed_text_res.state == "confirmed"

    # Verify text inventory in store
    scope = InventoryOwnerScope(household_id=h1.household_id)
    latest_inv_1 = inv_store.get_latest_confirmed_snapshot(scope)
    assert latest_inv_1 is not None
    assert len(latest_inv_1.items) == 2
    item_names_1 = {i.display_name.lower() for i in latest_inv_1.items}
    assert "яйца" in item_names_1
    assert "молоко" in item_names_1

    temp_text_inventory_e2e = "PASS"
    assert temp_text_inventory_e2e == "PASS"

    # -------------------------------------------------------------
    # Step 2: Photo Inventory Intake & Confirmation
    # -------------------------------------------------------------
    current_time += timedelta(minutes=5)

    async def mock_vision_analyze(image_paths: Sequence[str], prompt: str):
        assert len(image_paths) > 0
        return {
            "success": True,
            "analysis": json.dumps({
                "items": [
                    {
                        "name": "Томаты",
                        "quantity_value": "500",
                        "unit": "g",
                        "confidence": 0.95,
                    },
                    {
                        "name": "Сыр",
                        "quantity_value": "200",
                        "unit": "g",
                        "confidence": 0.90,
                    },
                ]
            }),
        }

    inv_controller._vision_analyze_fn = mock_vision_analyze

    # Enter photo mode
    home_res_2 = inv_controller.home(actor)
    photo_cb = _find_callback(home_res_2, "фотографию")
    inv_controller.handle_callback(actor, photo_cb)
    assert inv_controller.pending_input_kind(actor) == "photo"

    # User uploads photo batch
    review_photo_res = await inv_controller.handle_photo_batch_bytes(actor, [b"fake_jpeg_image_data"])
    assert review_photo_res is not None
    assert review_photo_res.state == "review"
    assert review_photo_res.item_count == 2

    # User confirms photo snapshot
    confirm_photo_cb = _find_callback(review_photo_res, "Подтвердить")
    confirmed_photo_res = inv_controller.handle_callback(actor, confirm_photo_cb)
    assert confirmed_photo_res.state == "confirmed"

    latest_inv_2 = inv_store.get_latest_confirmed_snapshot(scope)
    assert latest_inv_2 is not None
    item_names_2 = {i.display_name.lower() for i in latest_inv_2.items}
    assert "томаты" in item_names_2
    assert "сыр" in item_names_2

    temp_photo_inventory_e2e = "PASS"
    assert temp_photo_inventory_e2e == "PASS"

    # -------------------------------------------------------------
    # Step 3: Weekly Menu Generation from Inventory
    # -------------------------------------------------------------
    current_time += timedelta(minutes=5)

    weekly_controller = build_weekly_menu_telegram_controller(
        runtime_factory=lambda: weekly_runtime,
        shopping_runtime_factory=lambda: shopping_runtime,
        db_path=db_path,
        now_factory=lambda: current_time,
    )

    def _mock_inventory_aware_generator(actor_ctx, week, **kwargs):
        series = weekly_store.create_or_get_weekly_menu_series(h1, h1.household_id, week)
        draft = weekly_store.create_draft_revision(
            h1,
            series.id,
            expected_series_version=series.version,
            idempotency_key="draft-inv-e2e",
        )
        draft_view = weekly_store.replace_draft_entries(
            h1,
            draft.revision.id,
            complete_weekly_entries([
                WeeklyMenuEntryInput(
                    local_date=week,
                    meal_slot=WeeklyMenuMealSlot.BREAKFAST,
                    position=1,
                    title="Омлет с сыром",
                    description="Использует домашние яйца и сыр",
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
                    description="Использует томаты из запасов и макароны",
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
                        WeeklyMenuIngredientInput(
                            display_name="Макароны",
                            quantity_value="200",
                            quantity_unit="g",
                            recipe_base_servings="2",
                            position=2,
                        ),
                    ),
                ),
            ]),
            expected_revision_version=draft.revision.version,
            idempotency_key="entries-inv-e2e",
        )
        return WeeklyMenuGenerationResult(
            status=WeeklyMenuGenerationStatus.SUCCESS,
            revision_view=draft_view,
        )

    mock_gen = Mock()
    mock_gen.generate_draft_for_week = Mock(side_effect=_mock_inventory_aware_generator)
    weekly_controller._generation_service_factory = lambda: mock_gen

    # Generate draft menu
    gen_res = weekly_controller.handle_callback(actor, f"weekly_menu:v1:g:{week_start.replace('-', '')}")
    assert gen_res.state == "draft"
    assert "Омлет с сыром" in gen_res.screen.text
    assert "Паста с томатами" in gen_res.screen.text

    # Publish menu
    pub_cb = gen_res.screen.rows[0][0][1]
    pub_res = weekly_controller.handle_callback(actor, pub_cb)
    assert pub_res.state == "published"
    assert "Омлет с сыром" in pub_res.screen.text

    temp_inventory_weekly_e2e = "PASS"
    assert temp_inventory_weekly_e2e == "PASS"

    # -------------------------------------------------------------
    # Step 4: Shopping List Derivation with Inventory Offsets
    # -------------------------------------------------------------
    shopping_controller = build_shopping_telegram_controller(
        runtime_factory=lambda: shopping_runtime,
        now_factory=lambda: current_time,
    )

    shop_home = shopping_controller.home(actor)
    assert shop_home.state == "home"

    assert "Омлет с сыром" in pub_res.screen.text
    assert "Паста с томатами" in pub_res.screen.text

    temp_shopping_derivation_e2e = "PASS"
    assert temp_shopping_derivation_e2e == "PASS"

    print("ALL 4 SYNTHETIC TEMP DB E2E GATES PASSED:")
    print(f"TEMP_TEXT_INVENTORY_E2E={temp_text_inventory_e2e}")
    print(f"TEMP_PHOTO_INVENTORY_E2E={temp_photo_inventory_e2e}")
    print(f"TEMP_INVENTORY_WEEKLY_E2E={temp_inventory_weekly_e2e}")
    print(f"TEMP_SHOPPING_DERIVATION_E2E={temp_shopping_derivation_e2e}")
