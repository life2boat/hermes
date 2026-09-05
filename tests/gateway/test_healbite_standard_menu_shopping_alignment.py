from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest

from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_shopping import HealBiteShoppingStore
from gateway.healbite_shopping_runtime import (
    HealBiteShoppingRuntimeService,
    build_shopping_runtime_service,
)
from gateway.healbite_shopping_telegram import (
    SHOPPING_PLACEHOLDER_REPLY,
    build_shopping_telegram_controller,
)
from gateway.healbite_weekly_menu_runtime import (
    HealBiteWeeklyMenuRuntimeService,
    build_weekly_menu_runtime_service,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuEntryInput,
    WeeklyMenuIngredientInput,
    WeeklyMenuMealSlot,
)
from gateway.healbite_weekly_menu_telegram import (
    WEEKLY_MENU_PLACEHOLDER_REPLY,
    build_weekly_menu_telegram_controller,
)
from gateway.platforms.telegram import TelegramAdapter
from gateway.config import PlatformConfig


ACTOR = 101
OTHER_ACTOR = 202
WEEK_START = "2026-07-06"


def _seed_user(db_path: Path, actor: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(user_id INTEGER PRIMARY KEY, username TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (actor, "synthetic"),
        )


def _seed_household(db_path: Path, actor_id: int):
    _seed_user(db_path, actor_id)
    store = HealBiteHouseholdStore(db_path=db_path, ensure_schema_on_init=True)
    personal = store.get_or_create_personal_household(actor_id)
    return personal.household.id


def _seed_weekly_menu(db_path: Path, actor_id: int, *, with_ingredients: bool) -> tuple[str, str]:
    _seed_household(db_path, actor_id)
    households = HealBiteHouseholdStore(db_path=db_path)
    context = households.resolve_actor_context(actor_id)
    menu_store = HealBiteWeeklyMenuStore(db_path=db_path)
    menu_store.initialize_schema()

    series = menu_store.create_or_get_weekly_menu_series(
        context,
        context.household_id,
        WEEK_START,
    )
    draft = menu_store.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key=f"draft-{actor_id}",
    )
    ingredients = ()
    if with_ingredients:
        ingredients = (
            WeeklyMenuIngredientInput(
                display_name="Томаты",
                quantity_value="2",
                quantity_unit="piece",
                recipe_base_servings="2",
                position=1,
            ),
        )
    entries = [
        WeeklyMenuEntryInput(
            local_date=WEEK_START,
            meal_slot=WeeklyMenuMealSlot.LUNCH,
            position=1,
            title="Обед",
            servings="2",
            ingredients=ingredients,
        )
    ]
    ready = menu_store.replace_draft_entries(
        context,
        draft.revision.id,
        entries,
        expected_revision_version=draft.revision.version,
        idempotency_key=f"entries-{actor_id}",
    )
    published = menu_store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key=f"pub-{actor_id}",
    )
    return context.household_id, published.revision.id


# ============================================================
# 1. Weekly Menu UX Alignment Tests
# ============================================================

def test_weekly_menu_empty_state_exposes_inventory_and_fast_menu_when_inventory_enabled(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_household(db_path, ACTOR)
    store = HealBiteWeeklyMenuStore(db_path=db_path)
    store.initialize_schema()

    env = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_WEEKLY_MENU_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": str(ACTOR),
    }
    controller = build_weekly_menu_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=lambda: datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    result = controller.home(ACTOR)
    assert result.state == "empty"
    # Explanatory copy
    assert "🥕 <b>Меню из продуктов дома</b>" in result.screen.text
    assert "Учитывает продукты дома и автоматически формирует список покупок." in result.screen.text
    assert "⚡ <b>Быстрое меню</b>" in result.screen.text
    assert "План блюд на неделю без автоматического расчёта покупок." in result.screen.text

    labels = [label for row in result.screen.rows for label, cb in row]
    callbacks = [cb for row in result.screen.rows for label, cb in row]

    assert "🥕 Меню из продуктов дома" in labels
    assert any("Быстрое меню" in l for l in labels)
    assert "weekly_menu:v1:inv" in callbacks

    # Callback routing for inv
    cb_result = controller.handle_callback(ACTOR, "weekly_menu:v1:inv")
    assert cb_result.state == "open_inventory"


def test_weekly_menu_empty_state_without_inventory_feature_falls_back_to_single_button(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_household(db_path, ACTOR)
    store = HealBiteWeeklyMenuStore(db_path=db_path)
    store.initialize_schema()

    env = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_WEEKLY_MENU_ALLOWLIST": str(ACTOR),
        # Inventory NOT enabled for ACTOR
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "false",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": "999",
    }
    controller = build_weekly_menu_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=lambda: datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    result = controller.home(ACTOR)
    assert result.state == "empty"
    labels = [label for row in result.screen.rows for label, cb in row]
    callbacks = [cb for row in result.screen.rows for label, cb in row]

    assert "🥕 Меню из продуктов дома" not in labels
    assert "weekly_menu:v1:inv" not in callbacks
    assert any("Создать меню" in l for l in labels)


# ============================================================
# 2. Shopping Empty State Distinction Tests
# ============================================================

def test_shopping_empty_state_due_to_missing_ingredients_shows_guidance_and_action(tmp_path):
    db_path = tmp_path / "healbite.db"
    # Seed menu WITHOUT ingredients (standard weekly menu)
    hh_id, menu_id = _seed_weekly_menu(db_path, ACTOR, with_ingredients=False)

    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    context = HealBiteHouseholdStore(db_path=db_path).resolve_actor_context(ACTOR)

    # Create empty shopping list derived from that menu
    created = shopping_store.create_shopping_list(
        context,
        hh_id,
        week_start=WEEK_START,
        idempotency_key="create-list-empty-ing",
        source_menu_id=menu_id,
    )
    shopping_store.activate_shopping_list(
        context,
        created.shopping_list.id,
        expected_version=created.shopping_list.version,
        idempotency_key="activate-list-empty-ing",
    )

    env = {
        "HEALBITE_SHOPPING_LIST_ENABLED": "true",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": str(ACTOR),
    }
    controller = build_shopping_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=lambda: datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    result = controller.home(ACTOR)
    assert result.state == "home"
    assert "Список покупок пока пуст." in result.screen.text
    assert "Чтобы Hermes автоматически рассчитал недостающие продукты" in result.screen.text
    assert "составьте меню через «🥕 Продукты дома»." in result.screen.text

    labels = [label for row in result.screen.rows for label, cb in row]
    callbacks = [cb for row in result.screen.rows for label, cb in row]
    assert "🥕 Перейти в Продукты дома" in labels
    assert "shopping:v1:inv" in callbacks

    # Callback routing for inv
    cb_result = controller.handle_callback(ACTOR, "shopping:v1:inv", callback_query_id="q1")
    assert cb_result.state == "open_inventory"


def test_shopping_empty_state_when_all_ingredients_at_home_does_not_show_misleading_guidance(tmp_path):
    db_path = tmp_path / "healbite.db"
    # Seed menu WITH ingredients
    hh_id, menu_id = _seed_weekly_menu(db_path, ACTOR, with_ingredients=True)

    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    context = HealBiteHouseholdStore(db_path=db_path).resolve_actor_context(ACTOR)

    # Empty shopping list derived from menu with ingredients (e.g. pantry covers all)
    created = shopping_store.create_shopping_list(
        context,
        hh_id,
        week_start=WEEK_START,
        idempotency_key="create-list-with-ing",
        source_menu_id=menu_id,
    )
    shopping_store.activate_shopping_list(
        context,
        created.shopping_list.id,
        expected_version=created.shopping_list.version,
        idempotency_key="activate-list-with-ing",
    )

    env = {
        "HEALBITE_SHOPPING_LIST_ENABLED": "true",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": str(ACTOR),
    }
    controller = build_shopping_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=lambda: datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    result = controller.home(ACTOR)
    assert result.state == "home"
    assert "Всё необходимое уже есть дома." in result.screen.text
    assert "составьте меню через" not in result.screen.text

    labels = [label for row in result.screen.rows for label, cb in row]
    assert "🥕 Перейти в Продукты дома" not in labels


def test_shopping_empty_state_manual_list_shows_generic_empty(tmp_path):
    db_path = tmp_path / "healbite.db"
    hh_id = _seed_household(db_path, ACTOR)

    # Initialize menu schema first so shopping foreign keys resolve
    menu_store = HealBiteWeeklyMenuStore(db_path=db_path)
    menu_store.initialize_schema()

    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    context = HealBiteHouseholdStore(db_path=db_path).resolve_actor_context(ACTOR)

    # Manual list without source_menu_id
    created = shopping_store.create_shopping_list(
        context,
        hh_id,
        week_start=WEEK_START,
        idempotency_key="manual-empty-list",
        source_menu_id=None,
    )
    shopping_store.activate_shopping_list(
        context,
        created.shopping_list.id,
        expected_version=created.shopping_list.version,
        idempotency_key="activate-manual-empty-list",
    )

    env = {
        "HEALBITE_SHOPPING_LIST_ENABLED": "true",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": str(ACTOR),
    }
    controller = build_shopping_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=lambda: datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    result = controller.home(ACTOR)
    assert result.state == "home"
    assert "Список пуст." in result.screen.text
    assert "Чтобы Hermes автоматически рассчитал" not in result.screen.text
    labels = [label for row in result.screen.rows for label, cb in row]
    assert "🥕 Перейти в Продукты дома" not in labels


# ============================================================
# 3. Telegram Adapter Callback Routing Tests
# ============================================================

@pytest.mark.asyncio
async def test_telegram_adapter_routes_inventory_callback_from_weekly_menu(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_household(db_path, ACTOR)
    store = HealBiteWeeklyMenuStore(db_path=db_path)
    store.initialize_schema()

    env = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_WEEKLY_MENU_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": str(ACTOR),
    }
    controller = build_weekly_menu_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=lambda: datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={}))
    adapter._weekly_menu_telegram = controller
    adapter._inventory_telegram = Mock()
    adapter._inventory_telegram.home = Mock(return_value=SimpleNamespace(
        state="home",
        screen=SimpleNamespace(text="<b>🥕 Продукты дома</b>", parse_mode="HTML", rows=()),
        continuations=(),
        error_class=None,
    ))
    adapter._send_healbite_inventory_result = AsyncMock()

    query = SimpleNamespace(
        id="query-1",
        data="weekly_menu:v1:inv",
        from_user=SimpleNamespace(id=ACTOR, username="test", first_name="Test"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=555, type="private"),
            message_id=42,
            message_thread_id=None,
        ),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )

    await adapter._handle_healbite_weekly_menu_callback(query, "weekly_menu:v1:inv")

    query.answer.assert_awaited_once_with(text="Продукты дома.")
    adapter._inventory_telegram.home.assert_called_once_with(ACTOR)
    adapter._send_healbite_inventory_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_adapter_routes_inventory_callback_from_shopping(tmp_path):
    db_path = tmp_path / "healbite.db"
    hh_id, menu_id = _seed_weekly_menu(db_path, ACTOR, with_ingredients=False)
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    context = HealBiteHouseholdStore(db_path=db_path).resolve_actor_context(ACTOR)
    shopping_store.create_shopping_list(
        context,
        hh_id,
        week_start=WEEK_START,
        idempotency_key="create-list-empty-ing-2",
        source_menu_id=menu_id,
    )

    env = {
        "HEALBITE_SHOPPING_LIST_ENABLED": "true",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": str(ACTOR),
    }
    controller = build_shopping_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=lambda: datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={}))
    adapter._shopping_telegram = controller
    adapter._inventory_telegram = Mock()
    adapter._inventory_telegram.home = Mock(return_value=SimpleNamespace(
        state="home",
        screen=SimpleNamespace(text="<b>🥕 Продукты дома</b>", parse_mode="HTML", rows=()),
        continuations=(),
        error_class=None,
    ))
    adapter._send_healbite_inventory_result = AsyncMock()

    query = SimpleNamespace(
        id="query-2",
        data="shopping:v1:inv",
        from_user=SimpleNamespace(id=ACTOR, username="test", first_name="Test"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=555, type="private"),
            message_id=43,
            message_thread_id=None,
        ),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )

    await adapter._handle_healbite_shopping_callback(query, "shopping:v1:inv")

    query.answer.assert_awaited_once_with(text="Продукты дома.")
    adapter._inventory_telegram.home.assert_called_once_with(ACTOR)
    adapter._send_healbite_inventory_result.assert_awaited_once()


# ============================================================
# 4. Security & Household Scope Isolation Tests
# ============================================================

def test_store_ingredient_count_cross_household_isolation(tmp_path):
    db_path = tmp_path / "healbite.db"
    hh_a, menu_a = _seed_weekly_menu(db_path, ACTOR, with_ingredients=True)
    hh_b, menu_b = _seed_weekly_menu(db_path, OTHER_ACTOR, with_ingredients=True)

    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    households = HealBiteHouseholdStore(db_path=db_path)

    context_a = households.resolve_actor_context(ACTOR)
    context_b = households.resolve_actor_context(OTHER_ACTOR)

    # Actor A can see ingredient count of menu A
    count_a_own = shopping_store.get_source_menu_ingredient_count(context_a, menu_a)
    assert count_a_own == 1

    # Actor B querying menu A gets 0 (isolated by context.household_id)
    count_b_query_a = shopping_store.get_source_menu_ingredient_count(context_b, menu_a)
    assert count_b_query_a == 0

    # Invalid UUID returns 0
    assert shopping_store.get_source_menu_ingredient_count(context_a, "not-a-uuid") == 0


def test_feature_gate_denial_enforced_for_non_allowlisted_users(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_household(db_path, OTHER_ACTOR)

    env = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_WEEKLY_MENU_ALLOWLIST": str(ACTOR),
        "HEALBITE_SHOPPING_LIST_ENABLED": "true",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": str(ACTOR),
    }
    menu_ctrl = build_weekly_menu_telegram_controller(db_path=db_path, env=env)
    shop_ctrl = build_shopping_telegram_controller(db_path=db_path, env=env)

    # Non-allowlisted user OTHER_ACTOR is denied
    menu_res = menu_ctrl.home(OTHER_ACTOR)
    assert menu_res.state == "disabled"
    assert menu_res.screen.text == WEEKLY_MENU_PLACEHOLDER_REPLY

    shop_res = shop_ctrl.home(OTHER_ACTOR)
    assert shop_res.state == "disabled"
    assert shop_res.screen.text == SHOPPING_PLACEHOLDER_REPLY
