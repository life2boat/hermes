from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_shopping import HealBiteShoppingStore
from gateway.healbite_shopping_runtime import HealBiteShoppingRuntimeService
from gateway.healbite_weekly_menu_draft import (
    WeeklyMenuDraftValidationError,
    validate_weekly_menu_draft,
)
from gateway.healbite_weekly_menu_generation import (
    AuxiliaryWeeklyMenuGenerator,
    MAX_WEEKLY_MENU_CONTRACT_ATTEMPTS,
    WeeklyMenuGenerationStatus,
    WeeklyMenuGeneratorValidationError,
    _parse_generation_response,
)
from gateway.healbite_weekly_menu_generation_types import (
    WeeklyMenuGeneratedEntry,
    WeeklyMenuGenerationResponse,
    WeeklyMenuIngredient,
)
from gateway.healbite_weekly_menu_schema import (
    week_dates,
    WEEKLY_MENU_INGREDIENTS_TABLE,
)
from gateway.healbite_weekly_menus import (
    WeeklyMenuEntryInput,
    WeeklyMenuValidationError,
)
from tests.gateway.test_healbite_weekly_menu_generation import (
    _seed_generation_runtime,
    _service,
)

WEEK = "2026-07-06"


def valid_response():
    return WeeklyMenuGenerationResponse(
        tuple(
            WeeklyMenuGeneratedEntry(
                day,
                slot,
                1,
                "Synthetic meal",
                servings="2",
                ingredients=(WeeklyMenuIngredient("Rice", "100", "g"),),
            )
            for day in week_dates(WEEK)
            for slot in ("breakfast", "lunch", "dinner")
        )
    )


def payload():
    return {
        "entries": [
            dict(
                local_date=e.local_date,
                meal_slot=e.meal_slot,
                position=e.position,
                title=e.title,
                servings=e.servings,
                ingredients=[
                    dict(name=i.name, quantity_value=i.quantity_value, unit=i.unit)
                    for i in e.ingredients
                ],
            )
            for e in valid_response().entries
        ]
    }


def request():
    return SimpleNamespace(
        week_start=WEEK,
        dates=week_dates(WEEK),
        allowed_meal_slots=("breakfast", "lunch", "dinner"),
        max_entries=21,
        locale="ru-RU",
        member_count=1,
        members=(),
        household_dietary_notes=(),
        inventory_only=False,
    )


def test_valid_week():
    validate_weekly_menu_draft(valid_response(), week_start=WEEK)


@pytest.mark.parametrize(
    "change",
    ["empty", "missing", "none", "object", "bad_quantity", "bad_unit", "bad_name"],
)
def test_parser_rejects_invalid_ingredients(change):
    data = payload()
    meal = data["entries"][10]
    if change == "missing":
        del meal["ingredients"]
    elif change in {"empty", "none", "object"}:
        meal["ingredients"] = {"empty": [], "none": None, "object": {}}[change]
    else:
        field, value = {
            "bad_quantity": ("quantity_value", "NaN"),
            "bad_unit": ("unit", "unknown"),
            "bad_name": ("name", {}),
        }[change]
        meal["ingredients"][0][field] = value
    with pytest.raises(WeeklyMenuGeneratorValidationError):
        _parse_generation_response(data, request=request())


@pytest.mark.parametrize(
    "change",
    ["empty", "missing_meal", "duplicate", "wrong_day", "wrong_slot", "bad_support"],
)
def test_common_validator_rejects_injected_generator_defects(change):
    entries = list(valid_response().entries)
    if change == "empty":
        entries[10] = replace(entries[10], ingredients=())
    elif change == "missing_meal":
        entries.pop()
    elif change == "duplicate":
        entries[10] = entries[0]
    elif change == "wrong_day":
        entries[10] = replace(entries[10], local_date="2026-07-13")
    elif change == "wrong_slot":
        entries[10] = replace(entries[10], meal_slot="snack")
    else:
        entries[10] = replace(entries[10], ingredients=(object(),))
    with pytest.raises(WeeklyMenuDraftValidationError):
        validate_weekly_menu_draft(
            WeeklyMenuGenerationResponse(tuple(entries)), week_start=WEEK
        )


@pytest.mark.parametrize(
    "invalid_attempts,success", [(0, True), (1, True), (2, False), (100, False)]
)
def test_fast_provider_retry_and_persistence(tmp_path, invalid_attempts, success):
    db = tmp_path / "test.db"
    store, context = _seed_generation_runtime(db)
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        data = payload()
        if len(calls) <= invalid_attempts:
            data["entries"][10]["ingredients"] = []
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))]
        )

    service = _service(
        db,
        actor_ids=frozenset({context.actor_user_id}),
        generator=AuxiliaryWeeklyMenuGenerator(call_llm_fn=provider),
    )
    result = service.generate_draft_for_week(
        context.actor_user_id, WEEK, idempotency_key="bounded"
    )
    assert result.success is success
    assert len(calls) == min(invalid_attempts + 1, MAX_WEEKLY_MENU_CONTRACT_ATTEMPTS)
    assert all(call["call_policy"].max_external_requests == 1 for call in calls)
    assert "ingredients" in calls[0]["messages"][0]["content"]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM household_weekly_menu_entries"
        ).fetchone()[0] == (21 if success else 0)
        assert conn.execute(
            f"SELECT COUNT(*) FROM {WEEKLY_MENU_INGREDIENTS_TABLE}"
        ).fetchone()[0] == (21 if success else 0)
        if not success:
            assert (
                conn.execute("SELECT COUNT(*) FROM household_weekly_menus").fetchone()[
                    0
                ]
                == 0
            )
            assert (
                result.status is WeeklyMenuGenerationStatus.GENERATOR_VALIDATION_FAILED
            )
            return
    draft = result.revision_view
    published = store.publish_weekly_menu_revision(
        context,
        draft.revision.id,
        expected_series_version=draft.series.version,
        expected_revision_version=draft.revision.version,
        idempotency_key="publish",
    )
    assert len(published.entries) == 21
    shopping = HealBiteShoppingStore(db_path=db)
    shopping.initialize_schema()
    runtime = HealBiteShoppingRuntimeService(
        db_path=db,
        config=FeatureGateConfig(
            enabled=True, allowlist=frozenset({context.actor_user_id})
        ),
    )
    derived = runtime.generate_shopping_list_from_weekly_menu(
        context.actor_user_id, WEEK, "derive", None
    )
    assert len(derived.items) == 1
    assert derived.items[0].display_name == "Rice"
    assert derived.items[0].quantity_value == "2100"


def test_invalid_historical_draft_cannot_publish_or_change_rows(tmp_path):
    db = tmp_path / "test.db"
    store, context = _seed_generation_runtime(db)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, WEEK)
    draft = store.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key="draft",
    )
    ready = store.replace_draft_entries(
        context,
        draft.revision.id,
        [WeeklyMenuEntryInput(WEEK, "breakfast", 1, "Legacy title")],
        expected_revision_version=draft.revision.version,
        idempotency_key="partial",
    )
    with sqlite3.connect(db) as conn:
        before = tuple(conn.iterdump())
    with pytest.raises(WeeklyMenuValidationError):
        store.publish_weekly_menu_revision(
            context,
            ready.revision.id,
            expected_series_version=ready.series.version,
            expected_revision_version=ready.revision.version,
            idempotency_key="must-fail",
        )
    with sqlite3.connect(db) as conn:
        assert tuple(conn.iterdump()) == before


def test_fast_parser_preserves_ingredients():
    data = payload()
    before = deepcopy(data)
    parsed = _parse_generation_response(data, request=request())
    assert parsed == valid_response()
    assert data == before


def test_persisted_incomplete_ingredients_fail_publish_and_generation_replay(tmp_path):
    db = tmp_path / "test.db"
    store, context = _seed_generation_runtime(db)
    calls = []

    class Generator:
        def generate(self, request):
            calls.append(request)
            return valid_response()

    service = _service(
        db, actor_ids=frozenset({context.actor_user_id}), generator=Generator()
    )
    valid = service.generate_draft_for_week(
        context.actor_user_id, WEEK, idempotency_key="replay"
    )
    assert valid.success
    view = valid.revision_view
    # Synthetic pre-contract/corrupted historical draft: 21 meals, one lacks support.
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"DELETE FROM {WEEKLY_MENU_INGREDIENTS_TABLE} WHERE menu_entry_id=?",
            (view.entries[0].id,),
        )
    with sqlite3.connect(db) as conn:
        before = tuple(conn.iterdump())
    replay = service.generate_draft_for_week(
        context.actor_user_id, WEEK, idempotency_key="replay"
    )
    assert replay.status is WeeklyMenuGenerationStatus.VALIDATION_FAILED
    assert len(calls) == 1
    with pytest.raises(WeeklyMenuValidationError):
        store.publish_weekly_menu_revision(
            context,
            view.revision.id,
            expected_series_version=view.series.version,
            expected_revision_version=view.revision.version,
            idempotency_key="reject-incomplete",
        )
    with sqlite3.connect(db) as conn:
        assert tuple(conn.iterdump()) == before
