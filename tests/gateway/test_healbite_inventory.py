from __future__ import annotations

import sqlite3

import pytest

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryInputService,
    HealBiteInventoryStore,
    InventoryAccessError,
    InventoryItemInput,
    InventoryOwnerScope,
    InventoryStateError,
    InventoryStatus,
    calculate_missing_ingredients,
    parse_inventory_text,
)
from gateway.healbite_inventory_menu_contract import InventoryMenuContractError, parse_inventory_menu_response
from gateway.healbite_shopping_schema import ShoppingUnit
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weekly_menu_generation import (
    CanonicalWeeklyMenuMemberSnapshotProvider,
    HealBiteWeeklyMenuGenerationService,
)
from gateway.healbite_weekly_menu_generation_types import WeeklyMenuGenerationRequest
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore


def _request(*, notes: tuple[str, ...] = ()) -> WeeklyMenuGenerationRequest:
    return WeeklyMenuGenerationRequest(
        week_start="2026-07-06",
        dates=("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"),
        allowed_meal_slots=("breakfast", "lunch", "dinner"),
        locale="ru-RU",
        member_count=1,
        members=(),
        household_dietary_notes=notes,
        max_entries=21,
        inventory_snapshot_id="snapshot",
        inventory_only=True,
    )


def _payload(*, ingredient_name: str = "chicken") -> dict[str, object]:
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    slots = ("breakfast", "lunch", "dinner")
    return {
        "days": [
            {
                "day": day,
                "meals": [
                    {
                        "meal_type": slot,
                        "title": f"{day} {slot}",
                        "instructions": ["cook"],
                        "servings": 2,
                        "estimated_calories_per_serving": 500,
                        "macros_per_serving": {"protein_g": 30, "carbs_g": 40, "fat_g": 15},
                        "ingredients": [{"name": ingredient_name, "quantity_value": "500", "unit": "g"}],
                    }
                    for slot in slots
                ],
            }
            for day in names
        ]
    }


def test_text_parser_handles_lines_commas_quantity_units_and_unknown_values():
    parsed = parse_inventory_text("500 \u0433 \u043a\u0443\u0440\u0438\u0446\u044b, \u0440\u0438\u0441 1 \u043a\u0433\n3 \u044f\u0439\u0446\u0430\n\u0437\u0435\u043b\u0435\u043d\u044c")

    assert [(item.quantity_value, item.unit) for item in parsed] == [
        ("500", "\u0433"),
        ("1", "\u043a\u0433"),
        ("3", ShoppingUnit.PIECE),
        (None, ShoppingUnit.UNKNOWN),
    ]


def test_snapshot_lifecycle_is_confirmed_idempotent_and_isolated(tmp_path):
    store = HealBiteInventoryStore(db_path=tmp_path / "inventory.db")
    owner = InventoryOwnerScope(user_id=101)
    view = store.create_text_snapshot(owner, "0.1 kg rice")

    with pytest.raises(InventoryStateError):
        store.get_confirmed_snapshot(owner, view.snapshot.id)
    with pytest.raises(InventoryAccessError):
        store.get_snapshot(InventoryOwnerScope(user_id=202), view.snapshot.id)

    confirmed = store.confirm_snapshot(owner, view.snapshot.id)
    replay = store.confirm_snapshot(owner, view.snapshot.id)

    assert confirmed.snapshot.status is InventoryStatus.CONFIRMED
    assert replay.snapshot.confirmed_at == confirmed.snapshot.confirmed_at
    assert replay.items[0].quantity_value == "0.1"


def test_photo_candidate_stays_pending_editable_and_gate_fails_closed(tmp_path):
    store = HealBiteInventoryStore(db_path=tmp_path / "inventory.db")
    scope = InventoryOwnerScope(user_id=101)
    disabled = HealBiteInventoryInputService(store)
    with pytest.raises(InventoryStateError):
        disabled.create_photo_candidate(101, scope, [InventoryItemInput("item")])

    enabled = HealBiteInventoryInputService(
        store,
        photo_config=FeatureGateConfig(enabled=True, allowlist=frozenset({101})),
    )
    pending = enabled.create_photo_candidate(101, scope, [InventoryItemInput("item", uncertainty="uncertain")])
    edited = store.replace_pending_items(scope, pending.snapshot.id, [InventoryItemInput("item", "2", "piece")])

    assert pending.snapshot.status is InventoryStatus.PENDING
    assert edited.snapshot.source_revision == 2
    assert edited.items[0].quantity_value == "2"


def test_local_delta_uses_decimal_known_compatible_inventory_only(tmp_path):
    store = HealBiteInventoryStore(db_path=tmp_path / "inventory.db")
    scope = InventoryOwnerScope(user_id=101)
    view = store.create_snapshot(
        scope,
        "text",
        [
            InventoryItemInput("chicken", "300", "g"),
            InventoryItemInput("eggs"),
            InventoryItemInput("rice", "2", "piece"),
        ],
    )
    confirmed = store.confirm_snapshot(scope, view.snapshot.id)
    delta = calculate_missing_ingredients(
        [
            ("monday:dinner:1", InventoryItemInput("chicken", "500", "g")),
            ("tuesday:lunch:1", InventoryItemInput("eggs", "2", "piece")),
            ("wednesday:lunch:1", InventoryItemInput("rice", "500", "g")),
        ],
        confirmed,
    )

    assert [(item.normalized_name, item.quantity_value, item.unit.value, item.quantity_unknown) for item in delta.items] == [
        ("chicken", "200", "g", False),
        ("eggs", "2", "piece", False),
        ("rice", "500", "g", False),
    ]


def test_borrowed_connection_is_not_committed_rolled_back_or_closed():
    connection = sqlite3.connect(":memory:")
    store = HealBiteInventoryStore(connection=connection)
    connection.commit()
    connection.execute("BEGIN")

    store.create_text_snapshot(InventoryOwnerScope(user_id=101), "item")

    assert connection.in_transaction is True
    connection.rollback()
    connection.execute("SELECT 1").fetchone()
    connection.close()


def test_inventory_menu_contract_rejects_allergy_extra_fields_and_bad_macros():
    request = _request(notes=("peanut",))
    with pytest.raises(InventoryMenuContractError):
        parse_inventory_menu_response(_payload(ingredient_name="peanut butter"), request=request)

    valid_request = _request()
    payload = _payload()
    payload["extra"] = True
    with pytest.raises(InventoryMenuContractError):
        parse_inventory_menu_response(payload, request=valid_request)

    payload = _payload()
    payload["days"][0]["meals"][0]["macros_per_serving"]["protein_g"] = -1
    with pytest.raises(InventoryMenuContractError):
        parse_inventory_menu_response(payload, request=valid_request)

    response = parse_inventory_menu_response(_payload(), request=valid_request)
    assert len(response.entries) == 21
    assert response.entries[0].ingredients[0].quantity_value == "500"


class _StaticGenerator:
    def generate(self, request):
        return parse_inventory_menu_response(_payload(), request=request)


def test_existing_generation_service_accepts_only_confirmed_household_inventory(tmp_path):
    db_path = tmp_path / "inventory-generation.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT)")
        conn.execute("INSERT INTO users (user_id, username, created_at) VALUES (101, 'user', CURRENT_TIMESTAMP)")
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household = household_store.get_or_create_personal_household(101)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    profile_store = HealBiteUserProfileStore(db_path=db_path)
    profile_store.upsert_user_profile(
        user_id=101,
        username="user",
        daily_kcal_target=1800,
        daily_protein_target=120,
        daily_fat_target=60,
        daily_carbs_target=190,
    )
    inventory_store = HealBiteInventoryStore(db_path=db_path)
    inventory = inventory_store.create_snapshot(
        InventoryOwnerScope(household_id=household.household.id),
        "text",
        [InventoryItemInput("chicken", "10300", "g")],
    )
    service = HealBiteWeeklyMenuGenerationService(
        generator=_StaticGenerator(),
        member_snapshot_provider=CanonicalWeeklyMenuMemberSnapshotProvider(db_path=db_path),
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101})),
        inventory_config=FeatureGateConfig(enabled=True, allowlist=frozenset({101})),
        db_path=db_path,
    )

    pending = service.generate_draft_for_week(101, "2026-07-06", idempotency_key="pending", inventory_snapshot_id=inventory.snapshot.id)
    inventory_store.confirm_snapshot(InventoryOwnerScope(household_id=household.household.id), inventory.snapshot.id)
    result = service.generate_draft_for_week(101, "2026-07-06", idempotency_key="confirmed", inventory_snapshot_id=inventory.snapshot.id)

    assert pending.status.value == "inventory_not_confirmed"
    assert result.success is True
    assert result.revision_view is not None
    assert result.revision_view.revision.status.value == "draft"
    assert result.missing_ingredients is not None
    assert [(item.normalized_name, item.quantity_value) for item in result.missing_ingredients.items] == [("chicken", "200")]
