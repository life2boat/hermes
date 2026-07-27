from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryItemInput,
    InventoryOwnerScope,
    InventorySourceType,
)
from gateway.healbite_inventory_telegram import (
    HealBiteInventoryTelegramController,
    parse_inventory_callback,
)
from gateway.healbite_shopping import (
    HealBiteShoppingStore,
    ManualShoppingItemInput,
)
from gateway.healbite_shopping_schema import (
    ShoppingItemOrigin,
    ShoppingListStatus,
    ShoppingUnit,
)
from gateway.healbite_weekly_menu_schema import (
    WEEKLY_MENU_INGREDIENTS_TABLE,
    WeeklyMenuEntryOrigin,
    WeeklyMenuMealSlot,
    WeeklyMenuRevisionStatus,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuEntryInput,
    WeeklyMenuIngredientInput,
)
from gateway.healbite_weekly_shopping import (
    HealBiteWeeklyShoppingService,
    WEEKLY_SHOPPING_OBSERVABILITY_MARKER,
    WeeklyShoppingStaleError,
    WeeklyShoppingUnavailableError,
    WeeklyShoppingValidationError,
)


ACTOR = 8_000_000_000_000_004_101
OTHER_ACTOR = 8_000_000_000_000_004_102
WEEK_START = "2026-07-06"


def _gate(*actors: int, enabled: bool = True) -> FeatureGateConfig:
    return FeatureGateConfig(
        enabled=enabled,
        allowlist=frozenset(actors),
    )


def _seed(db_path: Path, actor: int = ACTOR):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(user_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users "
            "(user_id, username, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (actor, "synthetic"),
        )
    household_store = HealBiteHouseholdStore(db_path=db_path)
    personal = household_store.get_or_create_personal_household(actor)
    context = household_store.resolve_actor_context(actor)
    weekly = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly.initialize_schema()
    shopping = HealBiteShoppingStore(db_path=db_path)
    shopping.initialize_schema()
    inventory = HealBiteInventoryStore(db_path=db_path)
    inventory.initialize_schema()
    return personal, context, weekly, shopping, inventory


def _inventory(
    inventory: HealBiteInventoryStore,
    household_id: str,
    items: list[InventoryItemInput],
):
    pending = inventory.create_snapshot(
        InventoryOwnerScope(household_id=household_id),
        InventorySourceType.TEXT,
        [
            InventoryItemInput("Вода", "1000", "ml"),
            *items,
        ],
    )
    return inventory.confirm_snapshot(
        InventoryOwnerScope(household_id=household_id),
        pending.snapshot.id,
        expected_source_revision=pending.snapshot.source_revision,
    )


def _complete_entries(
    extras: dict[int, tuple[WeeklyMenuIngredientInput, ...]],
) -> list[WeeklyMenuEntryInput]:
    entries: list[WeeklyMenuEntryInput] = []
    slots = (
        WeeklyMenuMealSlot.BREAKFAST,
        WeeklyMenuMealSlot.LUNCH,
        WeeklyMenuMealSlot.DINNER,
    )
    start = datetime.fromisoformat(WEEK_START).date()
    index = 0
    for offset in range(7):
        local_date = (start + timedelta(days=offset)).isoformat()
        for position, slot in enumerate(slots, start=1):
            ingredients = (
                WeeklyMenuIngredientInput(
                    display_name="Вода",
                    quantity_value="1",
                    quantity_unit="ml",
                    recipe_base_servings="1",
                    position=1,
                ),
                *extras.get(index, ()),
            )
            entries.append(
                WeeklyMenuEntryInput(
                    local_date=local_date,
                    meal_slot=slot,
                    position=position,
                    title=f"Блюдо {index + 1}",
                    servings="1",
                    origin=WeeklyMenuEntryOrigin.GENERATED,
                    ingredients=ingredients,
                )
            )
            index += 1
    return entries


def _ingredient(
    name: str,
    quantity: str,
    unit: str,
    *,
    position: int = 2,
) -> WeeklyMenuIngredientInput:
    return WeeklyMenuIngredientInput(
        display_name=name,
        quantity_value=quantity,
        quantity_unit=unit,
        recipe_base_servings="1",
        position=position,
    )


def _draft(
    weekly: HealBiteWeeklyMenuStore,
    context,
    extras: dict[int, tuple[WeeklyMenuIngredientInput, ...]],
):
    series = weekly.create_or_get_weekly_menu_series(
        context,
        context.household_id,
        WEEK_START,
    )
    return weekly.apply_generated_draft_entries(
        context,
        week_start=WEEK_START,
        entries=_complete_entries(extras),
        expected_series_version=series.version,
        expected_draft_revision_id=None,
        expected_draft_revision_version=None,
        idempotency_key="synthetic-generation-1",
        payload_hash="1" * 64,
    )


def _service(
    db_path: Path,
    *,
    enabled: bool = True,
    fault_hook=None,
) -> HealBiteWeeklyShoppingService:
    return HealBiteWeeklyShoppingService(
        db_path=db_path,
        config=_gate(ACTOR, enabled=enabled),
        fault_hook=fault_hook,
    )


def _prepare(
    tmp_path: Path,
    inventory_items: list[InventoryItemInput],
    extras: dict[int, tuple[WeeklyMenuIngredientInput, ...]],
):
    db_path = tmp_path / "weekly-shopping.db"
    personal, context, weekly, shopping, inventory = _seed(db_path)
    confirmed = _inventory(
        inventory,
        personal.household.id,
        inventory_items,
    )
    draft = _draft(weekly, context, extras)
    service = _service(db_path)
    delta = service.preview(
        ACTOR,
        revision_id=draft.revision.id,
        inventory_snapshot_id=confirmed.snapshot.id,
    )
    return (
        db_path,
        personal,
        context,
        weekly,
        shopping,
        inventory,
        confirmed,
        draft,
        service,
        delta,
    )


@pytest.mark.parametrize(
    ("inventory_item", "required", "expected"),
    [
        (None, ("Рис", "1400", "g"), ("1400", ShoppingUnit.G)),
        (
            InventoryItemInput("Рис", "1", "kg"),
            ("Рис", "1400", "g"),
            ("400", ShoppingUnit.G),
        ),
        (
            InventoryItemInput("Молоко", "500", "ml"),
            ("Молоко", "2", "l"),
            ("1500", ShoppingUnit.ML),
        ),
        (
            InventoryItemInput("Яйца", "3", "piece"),
            ("Яйца", "10", "piece"),
            ("7", ShoppingUnit.PIECE),
        ),
    ],
)
def test_missing_quantity_uses_safe_unit_conversion(
    tmp_path,
    inventory_item,
    required,
    expected,
):
    prepared = _prepare(
        tmp_path,
        [] if inventory_item is None else [inventory_item],
        {0: (_ingredient(*required),)},
    )
    delta = prepared[-1]

    target = next(item for item in delta.items if item.display_name != "Вода")
    assert (target.quantity_value, target.unit) == expected
    assert not target.needs_review


def test_inventory_has_enough_adds_nothing(tmp_path):
    *_, delta = _prepare(
        tmp_path,
        [InventoryItemInput("Рис", "2", "kg")],
        {0: (_ingredient("Рис", "1400", "g"),)},
    )

    assert [item for item in delta.items if item.display_name != "Вода"] == []


@pytest.mark.parametrize(
    "inventory_item",
    [
        InventoryItemInput("Рис"),
        InventoryItemInput("Рис", "1", "l"),
    ],
)
def test_unknown_or_incompatible_inventory_needs_review(
    tmp_path,
    inventory_item,
):
    *_, delta = _prepare(
        tmp_path,
        [inventory_item],
        {0: (_ingredient("Рис", "500", "g"),)},
    )

    target = next(item for item in delta.items if item.display_name != "Вода")
    assert target.needs_review
    assert target.quantity_value is None
    assert target.unit is ShoppingUnit.UNKNOWN


def test_needs_review_item_is_applied_without_guessing_quantity(tmp_path):
    (
        _db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory,
        _confirmed,
        _draft,
        service,
        delta,
    ) = _prepare(
        tmp_path,
        [InventoryItemInput("Рис")],
        {0: (_ingredient("Рис", "500", "g"),)},
    )

    approved = service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=delta.approval_token,
        idempotency_key="approval-needs-review",
    )

    rice = next(item for item in approved.shopping.items if item.display_name == "Рис")
    assert rice.quantity_value is None
    assert rice.quantity_unit_normalized is ShoppingUnit.UNKNOWN


def test_repeated_ingredient_is_aggregated_across_all_meals(tmp_path):
    extras = {index: (_ingredient("Рис", "100", "g"),) for index in range(21)}
    *_, delta = _prepare(tmp_path, [], extras)

    rice = next(item for item in delta.items if item.display_name == "Рис")
    assert rice.quantity_value == "2100"
    assert len(rice.contributions) == 21


def test_existing_exact_identity_normalization_is_reused(tmp_path):
    *_, delta = _prepare(
        tmp_path,
        [],
        {
            0: (_ingredient(" РИС ", "100", "g"),),
            1: (_ingredient("рис", "200", "g"),),
        },
    )

    rice = [item for item in delta.items if item.normalized_name == "рис"]
    assert len(rice) == 1
    assert rice[0].quantity_value == "300"


def test_similar_products_are_not_fuzzy_merged(tmp_path):
    *_, delta = _prepare(
        tmp_path,
        [InventoryItemInput("Рисовая мука", "10", "kg")],
        {0: (_ingredient("Рис", "500", "g"),)},
    )

    rice = next(item for item in delta.items if item.display_name == "Рис")
    assert rice.quantity_value == "500"


def test_preview_is_read_only_and_does_not_publish_or_create_shopping(tmp_path):
    (
        db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory,
        _confirmed,
        draft,
        _service_instance,
        delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )

    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM household_weekly_menus WHERE id = ?",
            (draft.revision.id,),
        ).fetchone()[0]
        shopping_count = conn.execute(
            "SELECT COUNT(*) FROM household_shopping_lists"
        ).fetchone()[0]
    assert status == WeeklyMenuRevisionStatus.DRAFT.value
    assert shopping_count == 0
    assert delta.approval_token


def test_approval_applies_only_missing_delta_without_publishing(tmp_path):
    (
        _db_path,
        _personal,
        context,
        weekly,
        _shopping,
        _inventory,
        _confirmed,
        draft,
        service,
        delta,
    ) = _prepare(
        tmp_path,
        [InventoryItemInput("Рис", "1", "kg")],
        {0: (_ingredient("Рис", "1400", "g"),)},
    )

    result = service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=delta.approval_token,
        idempotency_key="approval-1",
    )

    approved = weekly.get_weekly_menu_revision(
        context,
        draft.revision.id,
    )
    rice = next(item for item in result.shopping.items if item.display_name == "Рис")
    assert approved.revision.status is WeeklyMenuRevisionStatus.APPROVED
    assert approved.revision.published_at is None
    assert rice.quantity_value == "400"
    assert result.shopping.shopping_list.source_menu_id == draft.revision.id
    with sqlite3.connect(_db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM household_weekly_menus WHERE status = ?",
            (WeeklyMenuRevisionStatus.PUBLISHED.value,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT operation FROM household_weekly_menu_idempotency "
            "WHERE revision_id = ? AND operation = 'approve_revision'",
            (draft.revision.id,),
        ).fetchone()[0] == "approve_revision"


def test_double_approval_is_idempotent(tmp_path):
    *_, service, delta = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )
    first = service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=delta.approval_token,
        idempotency_key="approval-repeat",
    )
    second = service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=delta.approval_token,
        idempotency_key="approval-repeat",
    )

    assert second.already_applied
    assert second.shopping.shopping_list.id == first.shopping.shopping_list.id
    assert [
        (item.normalized_name, item.quantity_value) for item in second.shopping.items
    ] == [(item.normalized_name, item.quantity_value) for item in first.shopping.items]


def test_transaction_failure_rolls_back_menu_and_shopping(tmp_path):
    (
        db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory,
        _confirmed,
        draft,
        _service_instance,
        delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )

    def fail(phase: str) -> None:
        if phase == "after_shopping_reconcile":
            raise RuntimeError("synthetic transaction failure")

    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        _service(db_path, fault_hook=fail).approve(
            ACTOR,
            week_start=WEEK_START,
            approval_token=delta.approval_token,
            idempotency_key="approval-failure",
        )

    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM household_weekly_menus WHERE id = ?",
                (draft.revision.id,),
            ).fetchone()[0]
            == WeeklyMenuRevisionStatus.DRAFT.value
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM household_shopping_lists").fetchone()[0]
            == 0
        )


def test_manual_items_and_same_item_provenance_are_preserved(tmp_path):
    (
        _db_path,
        personal,
        context,
        _weekly,
        shopping,
        _inventory,
        _confirmed,
        _draft_view,
        service,
        delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Молоко", "2", "l"),)},
    )
    created = shopping.create_shopping_list(
        context,
        personal.household.id,
        week_start=WEEK_START,
        idempotency_key="manual-list",
    )
    active = shopping.activate_shopping_list(
        context,
        created.shopping_list.id,
        expected_version=created.shopping_list.version,
        idempotency_key="manual-active",
    )
    with_toothpaste = shopping.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput("Зубная паста", "1", "piece"),
        expected_list_version=active.shopping_list.version,
        idempotency_key="manual-toothpaste",
    )
    shopping.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput("Молоко", "1", "l"),
        expected_list_version=with_toothpaste.shopping_list.version,
        idempotency_key="manual-milk",
    )

    approved = service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=delta.approval_token,
        idempotency_key="approval-manual",
    )

    manual = [
        item
        for item in approved.shopping.items
        if item.origin is ShoppingItemOrigin.MANUAL
    ]
    generated = [
        item
        for item in approved.shopping.items
        if item.origin is ShoppingItemOrigin.MENU_GENERATED
    ]
    assert {item.display_name for item in manual} == {
        "Зубная паста",
        "Молоко",
    }
    assert [item.display_name for item in generated] == ["Молоко"]


def test_replacement_approval_reconciles_without_accumulation(tmp_path):
    (
        _db_path,
        _personal,
        context,
        weekly,
        _shopping,
        _inventory,
        confirmed,
        _draft,
        service,
        first_delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Молоко", "2", "l"),)},
    )
    service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=first_delta.approval_token,
        idempotency_key="approval-first-menu",
    )
    series = weekly.get_weekly_menu_series(
        context,
        context.household_id,
        WEEK_START,
    )
    assert series is not None
    replacement = weekly.apply_generated_draft_entries(
        context,
        week_start=WEEK_START,
        entries=_complete_entries({0: (_ingredient("Молоко", "1", "l"),)}),
        expected_series_version=series.version,
        expected_draft_revision_id=None,
        expected_draft_revision_version=None,
        idempotency_key="synthetic-replacement",
        payload_hash="3" * 64,
    )
    replacement_delta = service.preview(
        ACTOR,
        revision_id=replacement.revision.id,
        inventory_snapshot_id=confirmed.snapshot.id,
    )

    approved = service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=replacement_delta.approval_token,
        idempotency_key="approval-replacement-menu",
    )

    previous = weekly.get_weekly_menu_revision(
        context,
        first_delta.weekly_revision_id,
    )
    current = weekly.get_weekly_menu_revision(
        context,
        replacement.revision.id,
    )
    assert previous.revision.status is WeeklyMenuRevisionStatus.ARCHIVED
    assert current.revision.status is WeeklyMenuRevisionStatus.APPROVED
    assert current.revision.published_at is None
    milk = [item for item in approved.shopping.items if item.display_name == "Молоко"]
    assert len(milk) == 1
    assert milk[0].quantity_value == "1000"


def test_inventory_is_not_consumed_by_approval(tmp_path):
    (
        db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory_store,
        confirmed,
        _draft_view,
        service,
        delta,
    ) = _prepare(
        tmp_path,
        [InventoryItemInput("Рис", "1", "kg")],
        {0: (_ingredient("Рис", "1400", "g"),)},
    )
    before = _inventory_rows(db_path, confirmed.snapshot.id)

    service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=delta.approval_token,
        idempotency_key="approval-no-consume",
    )

    assert _inventory_rows(db_path, confirmed.snapshot.id) == before


def _inventory_rows(db_path: Path, snapshot_id: str):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT normalized_name, quantity_value, quantity_unit "
            "FROM healbite_inventory_items WHERE snapshot_id = ? ORDER BY position",
            (snapshot_id,),
        ).fetchall()


def test_disabled_shopping_gate_fails_closed(tmp_path):
    (
        db_path,
        *_rest,
        delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )

    with pytest.raises(WeeklyShoppingUnavailableError):
        _service(db_path, enabled=False).approve(
            ACTOR,
            week_start=WEEK_START,
            approval_token=delta.approval_token,
            idempotency_key="approval-disabled",
        )


def test_owner_and_household_scope_are_enforced(tmp_path):
    (
        db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory,
        confirmed,
        draft,
        _service_instance,
        _delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )
    _seed(db_path, OTHER_ACTOR)
    other_service = HealBiteWeeklyShoppingService(
        db_path=db_path,
        config=_gate(OTHER_ACTOR),
    )

    with pytest.raises(WeeklyShoppingStaleError):
        other_service.preview(
            OTHER_ACTOR,
            revision_id=draft.revision.id,
            inventory_snapshot_id=confirmed.snapshot.id,
        )


def test_new_confirmed_inventory_invalidates_old_delta(tmp_path):
    (
        db_path,
        personal,
        _context,
        _weekly,
        _shopping,
        inventory,
        _confirmed,
        _draft_view,
        service,
        delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )
    replacement = _inventory(
        inventory,
        personal.household.id,
        [InventoryItemInput("Рис", "500", "g")],
    )
    with sqlite3.connect(db_path) as conn:
        latest_confirmed_at = conn.execute(
            "SELECT MAX(confirmed_at) FROM healbite_inventory_snapshots "
            "WHERE household_id = ?",
            (personal.household.id,),
        ).fetchone()[0]
        replacement_confirmed_at = (
            datetime.fromisoformat(str(latest_confirmed_at)) + timedelta(seconds=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE healbite_inventory_snapshots "
            "SET confirmed_at = ? WHERE id = ?",
            (replacement_confirmed_at, replacement.snapshot.id),
        )

    with pytest.raises(WeeklyShoppingStaleError):
        service.approve(
            ACTOR,
            week_start=WEEK_START,
            approval_token=delta.approval_token,
            idempotency_key="approval-stale-inventory",
        )


def test_regeneration_invalidates_old_delta(tmp_path):
    (
        _db_path,
        _personal,
        context,
        weekly,
        _shopping,
        _inventory,
        confirmed,
        draft,
        service,
        delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )
    regenerated = weekly.apply_generated_draft_entries(
        context,
        week_start=WEEK_START,
        entries=_complete_entries({0: (_ingredient("Рис", "700", "g"),)}),
        expected_series_version=draft.series.version,
        expected_draft_revision_id=draft.revision.id,
        expected_draft_revision_version=draft.revision.version,
        idempotency_key="synthetic-generation-2",
        payload_hash="2" * 64,
    )
    replacement = service.preview(
        ACTOR,
        revision_id=regenerated.revision.id,
        inventory_snapshot_id=confirmed.snapshot.id,
    )

    assert replacement.approval_token != delta.approval_token
    with pytest.raises(WeeklyShoppingStaleError):
        service.approve(
            ACTOR,
            week_start=WEEK_START,
            approval_token=delta.approval_token,
            idempotency_key="approval-stale-draft",
        )


def test_missing_required_weekly_slot_cannot_create_delta(tmp_path):
    (
        db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory,
        confirmed,
        draft,
        service,
        _delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM household_weekly_menu_entries WHERE id = ?",
            (draft.entries[-1].id,),
        )

    with pytest.raises(WeeklyShoppingValidationError):
        service.preview(
            ACTOR,
            revision_id=draft.revision.id,
            inventory_snapshot_id=confirmed.snapshot.id,
        )


def test_incomplete_weekly_data_cannot_create_delta(tmp_path):
    (
        db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory,
        confirmed,
        draft,
        service,
        _delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"DELETE FROM {WEEKLY_MENU_INGREDIENTS_TABLE} WHERE menu_entry_id = ?",
            (draft.entries[-1].id,),
        )

    with pytest.raises(WeeklyShoppingValidationError):
        service.preview(
            ACTOR,
            revision_id=draft.revision.id,
            inventory_snapshot_id=confirmed.snapshot.id,
        )


def test_telegram_preview_and_approval_use_opaque_revision_bound_callback(
    tmp_path,
):
    (
        db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory,
        confirmed,
        draft,
        _service_instance,
        _delta,
    ) = _prepare(
        tmp_path,
        [InventoryItemInput("Рис", "1", "kg")],
        {0: (_ingredient("Рис", "1400", "g"),)},
    )
    controller = HealBiteInventoryTelegramController(
        text_config=_gate(ACTOR),
        photo_config=_gate(enabled=False),
        weekly_generation_config=_gate(ACTOR),
        shopping_config=_gate(ACTOR),
        db_path=db_path,
        now_factory=lambda: datetime(
            2026,
            7,
            8,
            tzinfo=timezone.utc,
        ),
    )

    rendered = controller._draft(ACTOR, draft, confirmed)
    callback = next(
        callback_data
        for row in rendered.screen.rows
        for label, callback_data in row
        if "Одобрить" in label
    )
    parsed = parse_inventory_callback(callback)
    approved = controller.handle_callback(ACTOR, callback)

    text = "\n".join((rendered.screen.text, *rendered.continuations))
    assert "Нужно докупить" in text
    assert "Рис — 400 г" in text
    assert parsed is not None and parsed.approval_token is not None
    assert confirmed.snapshot.id not in callback
    assert draft.revision.id not in callback
    assert approved.state == "approved"
    assert "Недостающие продукты добавлены" in approved.screen.text


def test_telegram_feature_off_hides_preview_and_approval(tmp_path):
    (
        db_path,
        _personal,
        _context,
        _weekly,
        _shopping,
        _inventory,
        confirmed,
        draft,
        _service_instance,
        _delta,
    ) = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Рис", "500", "g"),)},
    )
    controller = HealBiteInventoryTelegramController(
        text_config=_gate(ACTOR),
        photo_config=_gate(enabled=False),
        weekly_generation_config=_gate(ACTOR),
        shopping_config=_gate(enabled=False),
        db_path=db_path,
    )

    rendered = controller._draft(ACTOR, draft, confirmed)
    text = "\n".join((rendered.screen.text, *rendered.continuations))
    labels = [label for row in rendered.screen.rows for label, _callback_data in row]

    assert "Нужно докупить" not in text
    assert all("Одобрить" not in label for label in labels)


def test_observability_is_privacy_safe(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    *_, service, delta = _prepare(
        tmp_path,
        [],
        {0: (_ingredient("Секретный продукт", "500", "g"),)},
    )
    service.approve(
        ACTOR,
        week_start=WEEK_START,
        approval_token=delta.approval_token,
        idempotency_key="approval-observability",
    )

    messages = [
        record.getMessage()
        for record in caplog.records
        if WEEKLY_SHOPPING_OBSERVABILITY_MARKER in record.getMessage()
    ]
    assert messages
    joined = "\n".join(messages)
    assert "Секретный продукт" not in joined
    assert "500" not in joined
    assert str(ACTOR) not in joined
    assert delta.weekly_revision_id not in joined
    assert delta.inventory_snapshot_id not in joined


def test_service_has_no_provider_dependency():
    import gateway.healbite_weekly_shopping as module

    source_names = set(module.__dict__)
    assert not any(
        token in name.lower()
        for name in source_names
        for token in ("deepseek", "provider", "auxiliary")
    )
