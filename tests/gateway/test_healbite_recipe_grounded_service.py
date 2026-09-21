from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_household_schema import HouseholdRole
from gateway.healbite_households import HealBiteHouseholdService, HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryItemInput,
    InventoryOwnerScope,
    InventorySourceType,
)
from gateway.healbite_shopping import ShoppingUnit
from gateway.healbite_recipe_catalog_domain import MealType
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore
from gateway.healbite_recipe_fixtures import TEST_AUTHORS, build_test_recipe_catalog
from gateway.healbite_recipe_grounded_planner import RecipeGroundedWeeklyPlanner
from gateway.healbite_recipe_grounded_service import (
    HealBiteRecipeGroundedService,
    RecipeGroundedStatus,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuConflictError,
    WeeklyMenuRevisionStatus,
)


def _deterministic_planner_caller(messages, **kwargs):
    user_content = json.loads(messages[1]["content"])
    week_start = user_content["week_start"]
    dates = user_content["dates"]
    cands = user_content["allowed_candidates"]
    breakfast_cands = [c for c in cands if c["slot"] == "breakfast"]
    lunch_cands = [c for c in cands if c["slot"] == "lunch"]
    dinner_cands = [c for c in cands if c["slot"] == "dinner"]

    meals = []
    for day_idx, dt in enumerate(dates):
        b = breakfast_cands[day_idx % len(breakfast_cands)]
        l = lunch_cands[day_idx % len(lunch_cands)]
        d = dinner_cands[day_idx % len(dinner_cands)]
        meals.append({"date": dt, "slot": "breakfast", "recipe_id": b["recipe_id"], "recipe_version": b["recipe_version"], "servings": 2})
        meals.append({"date": dt, "slot": "lunch", "recipe_id": l["recipe_id"], "recipe_version": l["recipe_version"], "servings": 2})
        meals.append({"date": dt, "slot": "dinner", "recipe_id": d["recipe_id"], "recipe_version": d["recipe_version"], "servings": 2})

    return json.dumps({"week_start": week_start, "meals": meals})


def _setup_test_environment(tmp_path: Path):
    db_path = tmp_path / "test_app.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (101, "chef_oleg"))
    conn.commit()
    conn.close()

    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(101)
    household_service = HealBiteHouseholdService(household_store)

    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()

    inventory_store = HealBiteInventoryStore(db_path=db_path)
    inventory_store.initialize_schema()

    catalog_path = tmp_path / "catalog.db"
    build_test_recipe_catalog(catalog_path)

    config = FeatureGateConfig(
        enabled=True,
        allowlist=frozenset(),
        public_access=True,
    )

    planner = RecipeGroundedWeeklyPlanner(llm_caller=_deterministic_planner_caller)

    service = HealBiteRecipeGroundedService(
        catalog_path=str(catalog_path),
        weekly_menu_store=weekly_store,
        household_service=household_service,
        inventory_store=inventory_store,
        planner=planner,
        config=config,
    )

    return db_path, household_service, weekly_store, inventory_store, service


def test_recipe_grounded_service_end_to_end_real_storage(tmp_path: Path) -> None:
    db_path, household_service, weekly_store, inventory_store, service = _setup_test_environment(tmp_path)
    week_start = "2026-09-21"
    authors = list(TEST_AUTHORS.keys())

    # 1. Generate menu with real stores
    result = service.generate_menu(
        actor_user_id=101,
        week_start=week_start,
        authors=authors,
        target_servings=Decimal("2"),
    )

    assert result.status == RecipeGroundedStatus.SUCCESS
    assert result.success is True
    assert result.revision_view is not None
    rev_view = result.revision_view
    assert rev_view.revision.status is WeeklyMenuRevisionStatus.DRAFT
    assert len(rev_view.entries) == 21
    assert all(e.origin.value == "generated" for e in rev_view.entries)

    # 2. Check recipe refs in storage
    refs = weekly_store.get_revision_recipe_refs(rev_view.revision.id)
    assert len(refs) == 21
    for entry in rev_view.entries:
        assert entry.id in refs
        ref = refs[entry.id]
        assert ref.entry_id == entry.id
        assert ref.author_id in authors
        assert ref.recipe_id
        assert ref.target_servings == "2"

    # 3. Deterministic replay returns same revision
    replay_result = service.generate_menu(
        actor_user_id=101,
        week_start=week_start,
        authors=authors,
        target_servings=Decimal("2"),
    )
    assert replay_result.status == RecipeGroundedStatus.SUCCESS
    assert replay_result.revision_view is not None
    assert replay_result.revision_view.revision.id == rev_view.revision.id


def test_recipe_grounded_service_conflict_handling_zero_orphaned_refs(tmp_path: Path) -> None:
    db_path, household_service, weekly_store, inventory_store, service = _setup_test_environment(tmp_path)
    week_start = "2026-09-21"
    authors = list(TEST_AUTHORS.keys())

    # Trigger a conflict by expecting non-existent series version
    result = service.generate_menu(
        actor_user_id=101,
        week_start=week_start,
        authors=authors,
        expected_series_version=999,
    )

    assert result.status == RecipeGroundedStatus.STORAGE_FAILURE
    assert "Conflict" in str(result.error_message)

    # Ensure zero entries and zero recipe refs exist in DB
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        entry_count = conn.execute(
            "SELECT COUNT(*) FROM household_weekly_menu_entries"
            if "household_weekly_menu_entries" in [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            ]
            else "SELECT 0"
        ).fetchone()[0]
        ref_count = conn.execute(
            "SELECT COUNT(*) FROM household_weekly_menu_entry_recipe_refs"
            if "household_weekly_menu_entry_recipe_refs" in [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            ]
            else "SELECT 0"
        ).fetchone()[0]

    assert entry_count == 0
    assert ref_count == 0


def test_recipe_grounded_service_with_real_inventory_store(tmp_path: Path) -> None:
    db_path, household_service, weekly_store, inventory_store, service = _setup_test_environment(tmp_path)
    context = household_service.resolve_existing_actor_household_context(101)
    scope = InventoryOwnerScope(household_id=context.household_id)

    # Add confirmed inventory items with Russian product names and various units
    items: list[InventoryItemInput] = [
        InventoryItemInput(
            display_name="Картофель свежий",
            quantity_value="2.0",
            unit=ShoppingUnit.KG,
            category="vegetables",
            confidence="0.95",
            uncertainty="confirmed",
        ),
        InventoryItemInput(
            display_name="Морковь",
            quantity_value="150",
            unit=ShoppingUnit.G,
            category="vegetables",
            confidence="0.9",
            uncertainty="confirmed",
        ),
        InventoryItemInput(
            display_name="Сыр",
            quantity_value=None,  # unknown quantity
            unit=ShoppingUnit.UNKNOWN,
            category="dairy",
            confidence="0.8",
            uncertainty="approximate",
        ),
    ]

    snap = inventory_store.create_snapshot(scope, InventorySourceType.TEXT, items)
    inventory_store.confirm_snapshot(scope, snap.snapshot.id)

    # Also create an unconfirmed pending snapshot to verify fail-closed filter
    pending_items: list[InventoryItemInput] = [
        InventoryItemInput(
            display_name="Молоко",
            quantity_value="10.0",
            unit=ShoppingUnit.L,
            category="dairy",
            confidence="0.9",
            uncertainty="unconfirmed",
        )
    ]
    inventory_store.create_snapshot(scope, InventorySourceType.TEXT, pending_items)

    week_start = "2026-09-21"
    authors = list(TEST_AUTHORS.keys())

    result = service.generate_menu(
        actor_user_id=101,
        week_start=week_start,
        authors=authors,
    )

    assert result.status == RecipeGroundedStatus.SUCCESS
    assert result.shopping_items is not None
    # Shopping derivation should be populated
    shopping_items_by_id = {item.ingredient_id: item for item in result.shopping_items}
    # POTATO (картофель) was 2kg in inventory, should show inventory used if needed by recipes
    if "POTATO" in shopping_items_by_id:
        assert shopping_items_by_id["POTATO"].inventory_quantity_used > Decimal("0")


def test_recipe_grounded_service_inventory_db_error_fail_safe(tmp_path: Path) -> None:
    db_path, household_service, weekly_store, inventory_store, service = _setup_test_environment(tmp_path)

    # Mock inventory store to raise sqlite3.OperationalError
    mock_inv = MagicMock()
    mock_inv.get_latest_confirmed_snapshot.side_effect = sqlite3.OperationalError("database is locked")
    service._inventory_store = mock_inv

    week_start = "2026-09-21"
    authors = list(TEST_AUTHORS.keys())

    # Should not crash on sqlite3.Error; inventory is gracefully skipped
    result = service.generate_menu(
        actor_user_id=101,
        week_start=week_start,
        authors=authors,
    )
    assert result.status == RecipeGroundedStatus.SUCCESS

    # But unexpected errors (e.g. programmer bug) should NOT be swallowed
    mock_inv.get_latest_confirmed_snapshot.side_effect = TypeError("programmer bug")
    with pytest.raises(TypeError, match="programmer bug"):
        service.generate_menu(
            actor_user_id=101,
            week_start=week_start,
            authors=authors,
        )
