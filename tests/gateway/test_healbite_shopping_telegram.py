from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.platforms import telegram as telegram_platform
from gateway.config import PlatformConfig
from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_shopping import (
    HealBiteShoppingStore,
    ManualShoppingItemInput,
    ShoppingItemOrigin,
)
from gateway.healbite_shopping_runtime import HealBiteShoppingRuntimeService
from gateway.healbite_shopping_telegram import (
    SHOPPING_ACTION_UNAVAILABLE_REPLY,
    SHOPPING_ADD_COMMAND,
    SHOPPING_CALLBACK_PREFIX,
    SHOPPING_COMMAND,
    SHOPPING_GENERATION_FAILED_REPLY,
    SHOPPING_GENERATION_MISSING_MENU_REPLY,
    SHOPPING_GENERATION_SUCCESS_REPLY,
    SHOPPING_MAX_CALLBACK_BYTES,
    SHOPPING_PLACEHOLDER_REPLY,
    HealBiteShoppingTelegramController,
    build_shopping_telegram_controller,
    parse_shopping_add_command,
    parse_shopping_callback,
    shopping_delivery_idempotency_key,
)
from gateway.healbite_shopping_schema import (
    SHOPPING_IDEMPOTENCY_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LISTS_TABLE,
)
from gateway.healbite_weekly_menu_schema import WEEKLY_MENU_INGREDIENTS_TABLE
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuEntryInput,
    WeeklyMenuIngredientInput,
    WeeklyMenuMealSlot,
)
from gateway.platforms.telegram import HEALBITE_REPLY_KEYBOARD_ACTIONS, TelegramAdapter


ACTOR = 101
OTHER_ACTOR = 202
WEEK_START = "2026-07-06"
_DEFAULT_MESSAGE = object()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_user(db_path: Path, actor: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(user_id INTEGER PRIMARY KEY, username TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (actor, "synthetic"),
        )


def _runtime(
    db_path: Path,
    *,
    enabled: bool = True,
    allowlist: frozenset[int] = frozenset({ACTOR}),
) -> HealBiteShoppingRuntimeService:
    return HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(
            enabled=enabled,
            allowlist=allowlist,
            configuration_valid=True,
        ),
        db_path=db_path,
    )


def _controller(
    db_path: Path,
    *,
    enabled: bool = True,
    allowlist: frozenset[int] = frozenset({ACTOR}),
) -> HealBiteShoppingTelegramController:
    runtime = _runtime(db_path, enabled=enabled, allowlist=allowlist)
    return HealBiteShoppingTelegramController(
        runtime_factory=lambda: runtime,
        now_factory=lambda: datetime(2026, 7, 8, 12, tzinfo=timezone.utc),
    )


def _seed_list(db_path: Path, actor: int = ACTOR):
    _seed_user(db_path, actor)
    households = HealBiteHouseholdStore(db_path=db_path)
    household = households.get_or_create_personal_household(actor)
    context = households.resolve_actor_context(actor)
    HealBiteWeeklyMenuStore(db_path=db_path).initialize_schema()
    store = HealBiteShoppingStore(db_path=db_path)
    store.initialize_schema()
    view = store.create_shopping_list(
        context,
        household.household.id,
        week_start=WEEK_START,
        idempotency_key=f"seed-list-{actor}",
    )
    active = store.activate_shopping_list(
        context,
        view.shopping_list.id,
        expected_version=view.shopping_list.version,
        idempotency_key=f"activate-list-{actor}",
    )
    return context, store, active


def _ingredient(
    name: str,
    quantity: str,
    unit: str,
    *,
    base_servings: str,
    position: int,
) -> WeeklyMenuIngredientInput:
    return WeeklyMenuIngredientInput(
        display_name=name,
        quantity_value=quantity,
        quantity_unit=unit,
        recipe_base_servings=base_servings,
        position=position,
    )


def _weekly_entries(*, structured: bool = True) -> list[WeeklyMenuEntryInput]:
    ingredients = (
        _ingredient("Молоко", "1", "l", base_servings="2", position=1),
        _ingredient("Хлеб", "1", "package", base_servings="2", position=2),
    ) if structured else ()
    return [
        WeeklyMenuEntryInput(
            local_date=WEEK_START,
            meal_slot=WeeklyMenuMealSlot.BREAKFAST,
            position=1,
            title="Синтетический завтрак",
            servings="2",
            ingredients=ingredients,
        )
    ]


def _seed_weekly_source(
    db_path: Path,
    *,
    structured: bool = True,
    create_list: bool = False,
):
    _seed_user(db_path, ACTOR)
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(ACTOR)
    context = households.resolve_actor_context(ACTOR)
    weekly = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly.initialize_schema()
    shopping = HealBiteShoppingStore(db_path=db_path)
    shopping.initialize_schema()
    series = weekly.create_or_get_weekly_menu_series(
        context,
        personal.household.id,
        WEEK_START,
    )
    draft = weekly.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key=f"draft-{structured}",
    )
    ready = weekly.replace_draft_entries(
        context,
        draft.revision.id,
        _weekly_entries(structured=structured),
        expected_revision_version=draft.revision.version,
        idempotency_key=f"replace-{structured}",
    )
    published = weekly.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key=f"publish-{structured}",
    )
    view = None
    if create_list:
        created = shopping.create_shopping_list(
            context,
            personal.household.id,
            week_start=WEEK_START,
            idempotency_key="seed-generation-list",
        )
        view = shopping.activate_shopping_list(
            context,
            created.shopping_list.id,
            expected_version=created.shopping_list.version,
            idempotency_key="activate-generation-list",
        )
    return households, shopping, context, published, view


def _table_snapshot(conn: sqlite3.Connection, table: str):
    columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    order = "id" if "id" in columns else columns[0]
    return tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"))


def _callbacks(result) -> list[tuple[str, str]]:
    return [button for row in result.screen.rows for button in row]


def _find_callback(result, label: str) -> str:
    return next(
        data for button_label, data in _callbacks(result) if button_label == label
    )


def _shopping_button_label() -> str:
    return next(
        label
        for label, action in HEALBITE_REPLY_KEYBOARD_ACTIONS.items()
        if action == SHOPPING_COMMAND
    )


def _message(*, text: str, actor: int = ACTOR, message_id: int = 77):
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        from_user=SimpleNamespace(id=actor, username="ignored", first_name="Ignored"),
        chat=SimpleNamespace(id=555, type="private"),
        chat_id=555,
        message_thread_id=None,
    )


def _query(
    *,
    data: str,
    actor: int = ACTOR,
    query_id: str = "query-one",
    message: object = _DEFAULT_MESSAGE,
    inline_message_id: str | None = None,
):
    source_message = (
        _message(text="old", actor=actor) if message is _DEFAULT_MESSAGE else message
    )
    return SimpleNamespace(
        id=query_id,
        data=data,
        from_user=SimpleNamespace(id=actor, username="ignored", first_name="Ignored"),
        message=source_message,
        inline_message_id=inline_message_id,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _adapter(controller: HealBiteShoppingTelegramController) -> TelegramAdapter:
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={})
    )
    adapter._shopping_telegram = controller
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True
    return adapter


def test_feature_is_disabled_by_default_without_opening_database(tmp_path):
    db_path = tmp_path / "disabled.db"
    controller = build_shopping_telegram_controller(env={}, db_path=db_path)

    result = controller.home(ACTOR)
    add_result = controller.add_from_command(
        ACTOR,
        "/shopping_add Must not persist",
        delivery_id="disabled-add",
    )
    callback_result = controller.handle_callback(
        ACTOR,
        "shopping:v1:r",
        callback_query_id="disabled-callback",
    )
    generation_result = controller.handle_callback(
        ACTOR,
        "shopping:v1:gc:20260706:0",
        callback_query_id="disabled-generation",
    )

    assert result.state == "disabled"
    assert result.screen.text == SHOPPING_PLACEHOLDER_REPLY
    assert add_result.state == "disabled"
    assert callback_result.state == "disabled"
    assert generation_result.state == "disabled"
    assert not db_path.exists()


def test_command_parser_and_callback_contract_are_strict():
    simple = parse_shopping_add_command("/shopping_add Milk")
    detailed = parse_shopping_add_command("/shopping_add Milk | 2 | l")

    assert simple is not None and (simple.name, simple.quantity, simple.unit) == (
        "Milk",
        None,
        "unknown",
    )
    assert detailed is not None and (
        detailed.name,
        detailed.quantity,
        detailed.unit,
    ) == ("Milk", "2", "l")
    assert parse_shopping_add_command("/shopping_add") is None
    assert parse_shopping_add_command("/shopping_add Milk | 2") is None
    assert parse_shopping_callback("shopping:v2:r") is None
    assert parse_shopping_callback("shopping:v1:t:not-a-uuid:1:1") is None
    request = parse_shopping_callback("shopping:v1:gr:20260706:0")
    confirm = parse_shopping_callback("shopping:v1:gc:20260706:17")
    cancel = parse_shopping_callback("shopping:v1:gx:20260706:17")
    assert request is not None and (
        request.action,
        request.week_start,
        request.version,
    ) == ("gr", WEEK_START, 0)
    assert confirm is not None and (
        confirm.action,
        confirm.week_start,
        confirm.version,
    ) == ("gc", WEEK_START, 17)
    assert cancel is not None and cancel.action == "gx"
    assert parse_shopping_callback("shopping:v1:gr:20260707:0") is None
    assert parse_shopping_callback("shopping:v1:gc:20260706:-1") is None
    assert parse_shopping_callback("shopping:v1:gc:20260706:not-a-version") is None
    assert parse_shopping_callback("shopping:v1:" + "x" * 80) is None
    assert parse_shopping_callback("shopping:v1:\ud800") is None
    assert len(shopping_delivery_idempotency_key("delivery", operation="add")) <= 128


def test_generation_request_cancel_and_success_are_explicit_and_actor_scoped(tmp_path):
    db_path = tmp_path / "generation.db"
    _households, _shopping, _context, published, _view = _seed_weekly_source(
        db_path
    )
    controller = _controller(db_path)
    home = controller.home(ACTOR)
    request_data = _find_callback(home, "Сформировать по меню")
    assert len(request_data.encode("utf-8")) <= SHOPPING_MAX_CALLBACK_BYTES
    assert str(ACTOR) not in request_data
    assert published.series.household_id not in request_data
    assert published.revision.id not in request_data

    with _connect(db_path) as conn:
        before = {
            table: _table_snapshot(conn, table)
            for table in (
                SHOPPING_LISTS_TABLE,
                SHOPPING_ITEMS_TABLE,
                SHOPPING_IDEMPOTENCY_TABLE,
            )
        }
    request = controller.handle_callback(
        ACTOR,
        request_data,
        callback_query_id="request-only",
    )
    assert request.state == "generation_confirmation"
    assert "Ручные позиции сохранятся" in request.screen.text
    assert "Недельное меню не изменится" in request.screen.text
    assert published.revision.id not in request.screen.text

    cancelled = controller.handle_callback(
        ACTOR,
        _find_callback(request, "Отмена"),
        callback_query_id="cancel-only",
    )
    assert cancelled.state == "empty"
    with _connect(db_path) as conn:
        assert before == {
            table: _table_snapshot(conn, table)
            for table in before
        }

    generated = controller.handle_callback(
        ACTOR,
        _find_callback(request, "Да, сформировать"),
        callback_query_id="generate-once",
    )
    assert generated.state == "home"
    assert SHOPPING_GENERATION_SUCCESS_REPLY in generated.screen.text
    assert _find_callback(generated, "Обновить по меню")
    current = _runtime(db_path).get_current_shopping_list(ACTOR, WEEK_START)
    assert current is not None
    assert len(current.items) == 2
    assert all(item.origin is ShoppingItemOrigin.MENU_GENERATED for item in current.items)


def test_generation_replay_changed_payload_and_stale_version_fail_closed(tmp_path):
    db_path = tmp_path / "generation-idempotency.db"
    _seed_weekly_source(db_path)
    controller = _controller(db_path)
    request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Сформировать по меню"),
        callback_query_id="request",
    )
    confirm = _find_callback(request, "Да, сформировать")
    once = controller.handle_callback(ACTOR, confirm, callback_query_id="same-delivery")
    with _connect(db_path) as conn:
        after_once = {
            table: _table_snapshot(conn, table)
            for table in (
                SHOPPING_LISTS_TABLE,
                SHOPPING_ITEMS_TABLE,
                SHOPPING_IDEMPOTENCY_TABLE,
            )
        }
    replay = controller.handle_callback(ACTOR, confirm, callback_query_id="same-delivery")
    assert once.screen.text == replay.screen.text
    with _connect(db_path) as conn:
        assert after_once == {
            table: _table_snapshot(conn, table)
            for table in after_once
        }

    update_request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Обновить по меню"),
        callback_query_id="new-request",
    )
    changed_payload = controller.handle_callback(
        ACTOR,
        _find_callback(update_request, "Да, обновить"),
        callback_query_id="same-delivery",
    )
    assert SHOPPING_ACTION_UNAVAILABLE_REPLY in changed_payload.screen.text
    with _connect(db_path) as conn:
        assert after_once == {
            table: _table_snapshot(conn, table)
            for table in after_once
        }

    stale_confirm = _find_callback(update_request, "Да, обновить")
    controller.add_from_command(
        ACTOR,
        "/shopping_add Manual item",
        delivery_id="intervening-mutation",
    )
    with _connect(db_path) as conn:
        before_stale = {
            table: _table_snapshot(conn, table)
            for table in (
                SHOPPING_LISTS_TABLE,
                SHOPPING_ITEMS_TABLE,
                SHOPPING_IDEMPOTENCY_TABLE,
            )
        }
    stale = controller.handle_callback(
        ACTOR,
        stale_confirm,
        callback_query_id="stale-delivery",
    )
    assert SHOPPING_ACTION_UNAVAILABLE_REPLY in stale.screen.text
    with _connect(db_path) as conn:
        assert before_stale == {
            table: _table_snapshot(conn, table)
            for table in before_stale
        }


def test_regeneration_preserves_manual_checked_policy_and_restores_deleted_generated(
    tmp_path,
):
    db_path = tmp_path / "generation-policy.db"
    _seed_weekly_source(db_path, create_list=True)
    controller = _controller(db_path)
    controller.add_from_command(
        ACTOR,
        "/shopping_add Ручная позиция",
        delivery_id="manual-add",
    )
    first_request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Сформировать по меню"),
        callback_query_id="first-request",
    )
    controller.handle_callback(
        ACTOR,
        _find_callback(first_request, "Да, обновить"),
        callback_query_id="first-generate",
    )
    runtime = _runtime(db_path)
    first = runtime.get_current_shopping_list(ACTOR, WEEK_START)
    assert first is not None
    generated = [
        item for item in first.items if item.origin is ShoppingItemOrigin.MENU_GENERATED
    ]
    assert len(generated) == 2
    checked = generated[0]
    deleted = generated[1]
    runtime.set_shopping_item_checked(
        ACTOR,
        checked.id,
        True,
        "test-check-generated",
        checked.version,
    )
    runtime.delete_shopping_item(
        ACTOR,
        deleted.id,
        "test-delete-generated",
        deleted.version,
    )

    second_request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Обновить по меню"),
        callback_query_id="second-request",
    )
    regenerated = controller.handle_callback(
        ACTOR,
        _find_callback(second_request, "Да, обновить"),
        callback_query_id="second-generate",
    )
    assert SHOPPING_GENERATION_SUCCESS_REPLY in regenerated.screen.text
    current = runtime.get_current_shopping_list(ACTOR, WEEK_START)
    assert current is not None
    assert any(item.display_name == "Ручная позиция" for item in current.items)
    generated_by_name = {
        item.display_name: item
        for item in current.items
        if item.origin is ShoppingItemOrigin.MENU_GENERATED
    }
    assert checked.display_name in generated_by_name
    assert generated_by_name[checked.display_name].checked_state is True
    assert deleted.display_name in generated_by_name


def test_missing_legacy_and_malformed_weekly_sources_preserve_existing_list(tmp_path):
    cases = ("missing", "legacy", "malformed")
    for case in cases:
        db_path = tmp_path / f"{case}.db"
        if case == "missing":
            _seed_list(db_path)
        else:
            _seed_weekly_source(
                db_path,
                structured=case != "legacy",
                create_list=True,
            )
            if case == "malformed":
                with _connect(db_path) as conn:
                    conn.execute(
                        f"UPDATE {WEEKLY_MENU_INGREDIENTS_TABLE} "
                        "SET quantity_value = 'not-a-number'"
                    )
        controller = _controller(db_path)
        request = controller.handle_callback(
            ACTOR,
            _find_callback(controller.home(ACTOR), "Сформировать по меню"),
            callback_query_id=f"{case}-request",
        )
        with _connect(db_path) as conn:
            before = {
                table: _table_snapshot(conn, table)
                for table in (
                    SHOPPING_LISTS_TABLE,
                    SHOPPING_ITEMS_TABLE,
                    SHOPPING_IDEMPOTENCY_TABLE,
                )
            }
            ingredient_count = conn.execute(
                f"SELECT COUNT(*) FROM {WEEKLY_MENU_INGREDIENTS_TABLE}"
            ).fetchone()[0]
        label = "Да, обновить"
        result = controller.handle_callback(
            ACTOR,
            _find_callback(request, label),
            callback_query_id=f"{case}-confirm",
        )
        expected = (
            SHOPPING_GENERATION_MISSING_MENU_REPLY
            if case == "missing"
            else SHOPPING_GENERATION_FAILED_REPLY
        )
        assert expected in result.screen.text
        with _connect(db_path) as conn:
            assert before == {
                table: _table_snapshot(conn, table)
                for table in before
            }
            assert conn.execute(
                f"SELECT COUNT(*) FROM {WEEKLY_MENU_INGREDIENTS_TABLE}"
            ).fetchone()[0] == ingredient_count


def test_foreign_week_and_revoked_membership_are_denied_without_mutation(tmp_path):
    db_path = tmp_path / "scope-generation.db"
    _seed_weekly_source(db_path, create_list=True)
    controller = _controller(db_path)
    with _connect(db_path) as conn:
        before_foreign = {
            table: _table_snapshot(conn, table)
            for table in (
                SHOPPING_LISTS_TABLE,
                SHOPPING_ITEMS_TABLE,
                SHOPPING_IDEMPOTENCY_TABLE,
            )
        }
    foreign = controller.handle_callback(
        ACTOR,
        "shopping:v1:gc:20260713:1",
        callback_query_id="foreign-week",
    )
    assert SHOPPING_ACTION_UNAVAILABLE_REPLY in foreign.screen.text
    with _connect(db_path) as conn:
        assert before_foreign == {
            table: _table_snapshot(conn, table)
            for table in before_foreign
        }

    request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Сформировать по меню"),
        callback_query_id="revoke-request",
    )
    confirm = _find_callback(request, "Да, обновить")
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {HOUSEHOLD_MEMBERS_TABLE} SET status = 'removed' "
            "WHERE linked_user_id = ?",
            (ACTOR,),
        )
        before_revoked = {
            table: _table_snapshot(conn, table)
            for table in (
                SHOPPING_LISTS_TABLE,
                SHOPPING_ITEMS_TABLE,
                SHOPPING_IDEMPOTENCY_TABLE,
            )
        }
    revoked = controller.handle_callback(
        ACTOR,
        confirm,
        callback_query_id="revoke-confirm",
    )
    assert revoked.state in {"empty", "unavailable"}
    assert SHOPPING_GENERATION_SUCCESS_REPLY not in revoked.screen.text
    with _connect(db_path) as conn:
        assert before_revoked == {
            table: _table_snapshot(conn, table)
            for table in before_revoked
        }


def test_home_add_toggle_delete_and_clear_lifecycle(tmp_path):
    db_path = tmp_path / "shopping.db"
    _seed_list(db_path)
    controller = _controller(db_path)

    empty = controller.home(ACTOR)
    added = controller.add_from_command(
        ACTOR,
        "/shopping_add Milk & Tea | 2 | l",
        delivery_id="message-add",
    )

    assert empty.state == "home"
    assert "Список пуст" in empty.screen.text
    assert "Milk &amp; Tea" in added.screen.text
    assert "Milk & Tea" not in added.screen.text
    toggle_data = _find_callback(added, "Куплено")
    delete_data = _find_callback(added, "Удалить")
    assert len(toggle_data.encode("utf-8")) <= SHOPPING_MAX_CALLBACK_BYTES
    assert len(delete_data.encode("utf-8")) <= SHOPPING_MAX_CALLBACK_BYTES

    toggled = controller.handle_callback(
        ACTOR,
        toggle_data,
        callback_query_id="toggle-one",
    )
    assert "✅" in toggled.screen.text
    stale_delete = controller.handle_callback(
        ACTOR,
        delete_data,
        callback_query_id="delete-stale",
    )
    assert SHOPPING_ACTION_UNAVAILABLE_REPLY in stale_delete.screen.text

    current = controller.home(ACTOR)
    deleted = controller.handle_callback(
        ACTOR,
        _find_callback(current, "Удалить"),
        callback_query_id="delete-current",
    )
    assert "Список пуст" in deleted.screen.text

    controller.add_from_command(
        ACTOR,
        "/shopping_add Bread",
        delivery_id="message-bread",
    )
    clear_request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Очистить"),
        callback_query_id="clear-request",
    )
    assert clear_request.state == "clear_confirmation"
    cleared = controller.handle_callback(
        ACTOR,
        _find_callback(clear_request, "Да, очистить"),
        callback_query_id="clear-confirm",
    )
    assert "Список пуст" in cleared.screen.text


def test_duplicate_delivery_and_callback_do_not_duplicate_mutation(tmp_path):
    db_path = tmp_path / "idempotency.db"
    _seed_list(db_path)
    controller = _controller(db_path)

    first = controller.add_from_command(
        ACTOR,
        "/shopping_add Apples",
        delivery_id="same-message",
    )
    controller.add_from_command(
        ACTOR,
        "/shopping_add Apples",
        delivery_id="same-message",
    )
    assert len(controller.home(ACTOR).screen.text.split("Apples")) - 1 == 1

    toggle_data = _find_callback(first, "Куплено")
    once = controller.handle_callback(
        ACTOR,
        toggle_data,
        callback_query_id="same-query",
    )
    replay = controller.handle_callback(
        ACTOR,
        toggle_data,
        callback_query_id="same-query",
    )
    assert once.screen.text == replay.screen.text
    assert "✅" in replay.screen.text


def test_changed_payload_same_delivery_and_invalid_values_fail_closed(tmp_path):
    db_path = tmp_path / "invalid.db"
    _seed_list(db_path)
    controller = _controller(db_path)

    controller.add_from_command(
        ACTOR,
        "/shopping_add Apples",
        delivery_id="shared-delivery",
    )
    changed = controller.add_from_command(
        ACTOR,
        "/shopping_add Oranges",
        delivery_id="shared-delivery",
    )
    invalid_quantity = controller.add_from_command(
        ACTOR,
        "/shopping_add Milk | invalid | l",
        delivery_id="invalid-quantity",
    )
    oversized = controller.add_from_command(
        ACTOR,
        f"/shopping_add {'x' * 600}",
        delivery_id="oversized",
    )
    empty = controller.add_from_command(
        ACTOR,
        "/shopping_add   ",
        delivery_id="empty",
    )
    current = controller.home(ACTOR)

    assert changed.state in {"home", "unavailable"}
    assert invalid_quantity.state in {"home", "unavailable"}
    assert oversized.state in {"home", "unavailable"}
    assert empty.state == "invalid_input"
    assert "Apples" in current.screen.text
    assert "Oranges" not in current.screen.text
    assert "Milk" not in current.screen.text


def test_clear_cancel_stale_and_replay_are_safe(tmp_path):
    db_path = tmp_path / "clear.db"
    _seed_list(db_path)
    controller = _controller(db_path)
    controller.add_from_command(
        ACTOR,
        "/shopping_add First",
        delivery_id="first",
    )
    request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Очистить"),
        callback_query_id="request",
    )
    cancelled = controller.handle_callback(
        ACTOR,
        _find_callback(request, "Отмена"),
        callback_query_id="cancel",
    )
    assert "First" in cancelled.screen.text

    stale_confirm = _find_callback(request, "Да, очистить")
    controller.add_from_command(
        ACTOR,
        "/shopping_add Second",
        delivery_id="second",
    )
    stale = controller.handle_callback(
        ACTOR,
        stale_confirm,
        callback_query_id="stale-confirm",
    )
    assert "First" in stale.screen.text and "Second" in stale.screen.text

    current_request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Очистить"),
        callback_query_id="request-current",
    )
    confirm = _find_callback(current_request, "Да, очистить")
    once = controller.handle_callback(
        ACTOR,
        confirm,
        callback_query_id="confirm-current",
    )
    replay = controller.handle_callback(
        ACTOR,
        confirm,
        callback_query_id="confirm-current",
    )
    assert "Список пуст" in once.screen.text
    assert "Список пуст" in replay.screen.text


def test_delete_replay_preserves_other_items_and_list(tmp_path):
    db_path = tmp_path / "delete.db"
    _seed_list(db_path)
    controller = _controller(db_path)
    controller.add_from_command(
        ACTOR,
        "/shopping_add Keep",
        delivery_id="keep",
    )
    with_both = controller.add_from_command(
        ACTOR,
        "/shopping_add Remove",
        delivery_id="remove",
    )
    remove_callback = [
        data for label, data in _callbacks(with_both) if label == "Удалить"
    ][-1]
    deleted = controller.handle_callback(
        ACTOR,
        remove_callback,
        callback_query_id="delete-once",
    )
    replay = controller.handle_callback(
        ACTOR,
        remove_callback,
        callback_query_id="delete-once",
    )

    assert "Keep" in deleted.screen.text
    assert "Remove" not in deleted.screen.text
    assert replay.screen.text == deleted.screen.text
    assert controller.home(ACTOR).state == "home"


def test_foreign_and_random_item_callbacks_are_indistinguishable(tmp_path):
    db_path = tmp_path / "scope.db"
    _seed_list(db_path, ACTOR)
    _seed_list(db_path, OTHER_ACTOR)
    controller = _controller(
        db_path,
        allowlist=frozenset({ACTOR, OTHER_ACTOR}),
    )
    owner_view = controller.add_from_command(
        ACTOR,
        "/shopping_add Private item",
        delivery_id="owner-add",
    )
    foreign_data = _find_callback(owner_view, "Удалить")
    parsed = parse_shopping_callback(foreign_data)
    assert parsed is not None
    random_data = (
        f"{SHOPPING_CALLBACK_PREFIX}d:"
        f"33333333-3333-4333-8333-333333333333:{parsed.version}"
    )

    foreign = controller.handle_callback(
        OTHER_ACTOR,
        foreign_data,
        callback_query_id="foreign",
    )
    random = controller.handle_callback(
        OTHER_ACTOR,
        random_data,
        callback_query_id="random",
    )

    assert foreign == random
    assert "Private item" in controller.home(ACTOR).screen.text


def test_callback_payload_has_no_actor_household_or_item_content(tmp_path):
    db_path = tmp_path / "callback-content.db"
    _context, _store, view = _seed_list(db_path)
    controller = _controller(db_path)
    result = controller.add_from_command(
        ACTOR,
        "/shopping_add Sensitive display | 2 | kg",
        delivery_id="content",
    )

    for _label, callback_data in _callbacks(result):
        assert len(callback_data.encode("utf-8")) <= SHOPPING_MAX_CALLBACK_BYTES
        assert str(ACTOR) not in callback_data
        assert view.shopping_list.household_id not in callback_data
        assert "Sensitive" not in callback_data
        assert "kg" not in callback_data


def test_long_list_is_paginated_and_does_not_expose_internal_ids(tmp_path):
    db_path = tmp_path / "pages.db"
    _context, store, view = _seed_list(db_path)
    controller = _controller(db_path)
    for index in range(10):
        result = controller.add_from_command(
            ACTOR,
            f"/shopping_add Item {index}",
            delivery_id=f"add-{index}",
        )
        assert result.state == "home"

    first = controller.home(ACTOR)
    second = controller.handle_callback(
        ACTOR,
        _find_callback(first, "Далее"),
        callback_query_id="page-two",
    )

    assert "Item 0" in first.screen.text
    assert "Item 9" not in first.screen.text
    assert "Item 9" in second.screen.text
    assert view.shopping_list.id not in first.screen.text
    with _connect(db_path) as conn:
        item_ids = [
            row[0] for row in conn.execute("SELECT id FROM household_shopping_items")
        ]
    assert not any(item_id in first.screen.text for item_id in item_ids)
    assert store.audit_schema().schema_state.value == "canonical"


@pytest.mark.asyncio
async def test_button_and_commands_share_local_handler_when_disabled(tmp_path):
    adapter = _adapter(_controller(tmp_path / "disabled.db", enabled=False))
    button_update = SimpleNamespace(
        update_id=1,
        message=_message(text=_shopping_button_label()),
        effective_message=None,
    )
    command_update = SimpleNamespace(
        update_id=2,
        message=_message(text=SHOPPING_COMMAND),
        effective_message=None,
    )

    assert (
        await adapter._maybe_handle_healbite_menu_button(
            button_update,
            SimpleNamespace(),
        )
        is True
    )
    await adapter._handle_command(command_update, SimpleNamespace())

    assert adapter._send_message_with_thread_fallback.await_count == 2
    assert {
        call.kwargs["text"]
        for call in adapter._send_message_with_thread_fallback.await_args_list
    } == {SHOPPING_PLACEHOLDER_REPLY}
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_is_local_redacted_and_edit_fallback_does_not_remutate(
    tmp_path,
    caplog,
):
    db_path = tmp_path / "adapter.db"
    _seed_list(db_path)
    controller = _controller(db_path)
    created = controller.add_from_command(
        ACTOR,
        "/shopping_add Secret item",
        delivery_id="seed-item",
    )
    data = _find_callback(created, "Куплено")
    query = _query(data=data, query_id="opaque-query")
    query.edit_message_text.side_effect = RuntimeError("synthetic edit failure")
    adapter = _adapter(controller)

    with caplog.at_level(logging.INFO):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(),
        )

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    adapter._send_message_with_thread_fallback.assert_awaited_once()
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()
    assert "✅" in controller.home(ACTOR).screen.text
    assert "Secret item" not in caplog.text
    assert data not in caplog.text
    assert str(ACTOR) not in caplog.text


@pytest.mark.asyncio
async def test_generation_callback_is_local_redacted_and_edit_failure_does_not_repeat(
    tmp_path,
    caplog,
):
    db_path = tmp_path / "generation-adapter.db"
    _seed_weekly_source(db_path)
    controller = _controller(db_path)
    request = controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Сформировать по меню"),
        callback_query_id="request",
    )
    data = _find_callback(request, "Да, сформировать")
    controller_spy = Mock(wraps=controller)
    adapter = _adapter(controller_spy)
    query = _query(data=data, query_id="opaque-generation-query")
    query.edit_message_text.side_effect = RuntimeError("synthetic edit failure")

    with caplog.at_level(logging.INFO):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(),
        )

    controller_spy.handle_callback.assert_called_once()
    adapter._send_message_with_thread_fallback.assert_awaited_once()
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()
    current = _runtime(db_path).get_current_shopping_list(ACTOR, WEEK_START)
    assert current is not None and len(current.items) == 2
    assert data not in caplog.text
    assert str(ACTOR) not in caplog.text
    assert "Молоко" not in caplog.text


@pytest.mark.asyncio
async def test_forged_callback_is_consumed_locally(tmp_path):
    adapter = _adapter(_controller(tmp_path / "disabled.db", enabled=False))
    query = _query(data="shopping:v9:forged")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "shopping:v1:r",
        "shopping:v1:p:1",
        "shopping:v1:t:11111111-1111-4111-8111-111111111111:1:1",
        "shopping:v1:d:11111111-1111-4111-8111-111111111111:1",
        "shopping:v1:cr:1",
        "shopping:v1:cc:1",
        "shopping:v1:cx",
        "shopping:v1:gr:20260706:0",
        "shopping:v1:gc:20260706:0",
        "shopping:v1:gx:20260706:0",
        "shopping:v1:b",
        "shopping:v1:unknown",
        "shopping:v9:unknown",
    ],
)
async def test_missing_source_message_blocks_every_shopping_callback(
    data,
    caplog,
):
    controller = Mock()
    adapter = _adapter(controller)
    query = _query(data=data, message=None)

    with caplog.at_level(logging.INFO):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(),
        )

    controller.handle_callback.assert_not_called()
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()
    adapter._send_message_with_thread_fallback.assert_not_awaited()
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()
    assert data not in caplog.text
    assert str(ACTOR) not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "shopping:v1:t:11111111-1111-4111-8111-111111111111:1:1",
        "shopping:v1:d:11111111-1111-4111-8111-111111111111:1",
        "shopping:v1:cc:1",
        "shopping:v1:gc:20260706:0",
    ],
)
async def test_inaccessible_source_message_blocks_mutations(data, monkeypatch):
    class SyntheticMaybeInaccessibleMessage:
        pass

    class SyntheticAccessibleMessage(SyntheticMaybeInaccessibleMessage):
        pass

    class SyntheticInaccessibleMessage(SyntheticMaybeInaccessibleMessage):
        def __init__(self):
            self.chat = SimpleNamespace(id=555, type="private")

    monkeypatch.setattr(
        telegram_platform,
        "MaybeInaccessibleMessage",
        SyntheticMaybeInaccessibleMessage,
    )
    monkeypatch.setattr(
        telegram_platform,
        "Message",
        SyntheticAccessibleMessage,
    )
    controller = Mock()
    adapter = _adapter(controller)
    inaccessible = SyntheticInaccessibleMessage()
    query = _query(data=data, message=inaccessible)
    assert adapter._healbite_shopping_source_message(query) is None

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    controller.handle_callback.assert_not_called()
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_message_without_message_id_blocks_generation_callback():
    controller = Mock()
    adapter = _adapter(controller)
    source = _message(text="old")
    source.message_id = None
    query = _query(data="shopping:v1:gc:20260706:0", message=source)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    controller.handle_callback.assert_not_called()
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_message_without_source_is_not_accepted():
    controller = Mock()
    adapter = _adapter(controller)
    query = _query(
        data="shopping:v1:r",
        message=None,
        inline_message_id="synthetic-inline",
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    controller.handle_callback.assert_not_called()
    query.answer.assert_awaited_once()
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["toggle", "delete", "clear"])
async def test_accessible_edit_failure_invokes_mutation_controller_once(
    tmp_path,
    operation,
):
    db_path = tmp_path / f"edit-{operation}.db"
    _seed_list(db_path)
    controller = _controller(db_path)
    created = controller.add_from_command(
        ACTOR,
        "/shopping_add One item",
        delivery_id=f"seed-{operation}",
    )
    if operation == "toggle":
        data = _find_callback(created, "Куплено")
    elif operation == "delete":
        data = _find_callback(created, "Удалить")
    else:
        clear_request = controller.handle_callback(
            ACTOR,
            _find_callback(created, "Очистить"),
            callback_query_id="clear-request",
        )
        data = _find_callback(clear_request, "Да, очистить")
    controller_spy = Mock(wraps=controller)
    adapter = _adapter(controller_spy)
    query = _query(data=data, query_id=f"edit-{operation}")
    query.edit_message_text.side_effect = RuntimeError("synthetic edit failure")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    controller_spy.handle_callback.assert_called_once()
    adapter._send_message_with_thread_fallback.assert_awaited_once()
    current = controller.home(ACTOR).screen.text
    if operation == "toggle":
        assert "✅" in current
    else:
        assert "One item" not in current
