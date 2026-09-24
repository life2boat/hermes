"""Full Telegram Ingress & Intent Routing E2E Test Suite.

Validates the complete path:
Telegram update
-> text/button normalization
-> intent/router
-> canonical HealBite controller
-> user-visible response.

Tests Cases A through G + Follow-up context from task specification.
Direct controller calls alone do not satisfy this contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import sqlite3

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.healbite_family_telegram import HealBiteFamilyTelegramController
from gateway.healbite_households import (
    HealBiteHouseholdStore,
    HouseholdFeatureConfig,
)
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryItemInput,
    InventoryOwnerScope,
    InventoryStatus,
    ShoppingUnit,
)
from gateway.healbite_inventory_telegram import (
    HealBiteInventoryTelegramController,
    build_inventory_telegram_controller,
)
from gateway.healbite_shopping import HealBiteShoppingStore, ManualShoppingItemInput
from gateway.healbite_shopping_telegram import (
    SHOPPING_ADD_HELP,
    HealBiteShoppingTelegramController,
    build_shopping_telegram_controller,
)
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore
from gateway.healbite_weekly_menu_telegram import build_weekly_menu_telegram_controller
from gateway.platforms.telegram import (
    TelegramAdapter,
    _is_structured_inventory_input,
    _is_weekly_menu_intent,
    _is_shopping_intent,
)
from gateway.run import (
    _classify_telegram_diary_turn,
)


ACTOR = 968323641  # Same as production actor
NOW_FIXED = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
WEEK_START = "2026-09-21"


def _make_msg(text: str, *, user_id: int = ACTOR, message_id: int = 1001) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, username=f"user_{user_id}"),
        chat=SimpleNamespace(id=user_id, type="private"),
        chat_id=user_id,
        message_id=message_id,
        message_thread_id=None,
        reply_to_message=None,
    )


def _make_update(text: str, *, user_id: int = ACTOR, message_id: int = 1001, update_id: int = 1) -> SimpleNamespace:
    msg = _make_msg(text, user_id=user_id, message_id=message_id)
    return SimpleNamespace(
        update_id=update_id,
        message=msg,
        effective_message=msg,
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


def _setup_test_environment(tmp_path: Path):
    db_path = tmp_path / "healbite_test.db"

    _create_users_table(db_path)
    _insert_user(db_path, ACTOR)

    # Initialize household schema & seed personal household
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(ACTOR)
    context = households.resolve_actor_context(ACTOR)

    # Initialize inventory schema
    inventory_store = HealBiteInventoryStore(db_path=db_path)
    inventory_store.initialize_schema()

    # Initialize weekly menu schema
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()

    # Initialize shopping store
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()

    env = {
        "HEALBITE_DB_PATH": str(db_path),
        "HEALBITE_SHOPPING_LIST_ENABLED": "true",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(ACTOR),
        "HEALBITE_SHOPPING_LIST_PUBLIC": "true",
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_WEEKLY_MENU_ALLOWLIST": str(ACTOR),
        "HEALBITE_WEEKLY_MENU_PUBLIC": "true",
        "HEALBITE_WEEKLY_MENU_INVENTORY_ENABLED": "true",
        "HEALBITE_WEEKLY_MENU_INVENTORY_ALLOWLIST": str(ACTOR),
        "HEALBITE_WEEKLY_MENU_INVENTORY_PUBLIC": "true",
        "HEALBITE_INVENTORY_TEXT_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_PUBLIC": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ENABLED": "true",
        "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST": str(ACTOR),
        "HEALBITE_INVENTORY_TEXT_UI_PUBLIC": "true",
        "HEALBITE_HOUSEHOLDS_ENABLED": "true",
        "HEALBITE_HOUSEHOLDS_ALLOWLIST": str(ACTOR),
        "HEALBITE_HOUSEHOLDS_PUBLIC": "true",
    }

    return db_path, personal, context, households, inventory_store, weekly_store, shopping_store, env


def _build_test_adapter(tmp_path: Path, env: dict[str, str], *, now_factory=None):
    db_path = tmp_path / "healbite_test.db"
    now_fn = now_factory or (lambda: NOW_FIXED)

    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter._should_process_message = lambda msg, is_command=False: True
    adapter._maybe_block_public_feature_lane = AsyncMock(return_value=False)
    adapter._healbite_public_onboarding_enabled = Mock(return_value=False)
    adapter._healbite_now_utc = now_fn
    adapter._healbite_inventory_photo_batches = {}
    adapter._largest_photo_size = Mock(return_value=None)
    adapter._telegram_media_size_allowed = Mock(return_value=(True, None))

    # Bind methods
    adapter._healbite_command_from_text = TelegramAdapter._healbite_command_from_text
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter, TelegramAdapter
    )
    adapter._dispatch_healbite_keyboard_action = TelegramAdapter._dispatch_healbite_keyboard_action.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_shopping_command = TelegramAdapter._maybe_handle_healbite_shopping_command.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_weekly_menu_command = TelegramAdapter._maybe_handle_healbite_weekly_menu_command.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_inventory_pending_text = TelegramAdapter._maybe_handle_healbite_inventory_pending_text.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_unprompted_inventory_text = TelegramAdapter._maybe_handle_healbite_unprompted_inventory_text.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_explicit_intent = TelegramAdapter._maybe_handle_healbite_explicit_intent.__get__(
        adapter, TelegramAdapter
    )
    adapter._handle_text_message = TelegramAdapter._handle_text_message.__get__(
        adapter, TelegramAdapter
    )
    adapter._effective_update_message = TelegramAdapter._effective_update_message.__get__(
        adapter, TelegramAdapter
    )
    adapter._clean_bot_trigger_text = lambda text: text
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    adapter._build_message_event = Mock(return_value=SimpleNamespace(text=""))
    adapter._log_healbite_marker = Mock()
    adapter._log_healbite_route_selected = Mock()
    adapter._healbite_safe_action_label = TelegramAdapter._healbite_safe_action_label
    adapter._maybe_reject_healbite_compound_input = AsyncMock(return_value=False)
    adapter._maybe_handle_healbite_onboarding_reply = AsyncMock(return_value=False)
    adapter._maybe_handle_healbite_fridge_menu_pending_text = AsyncMock(return_value=False)
    adapter._maybe_handle_healbite_weight_pending_reply = AsyncMock(return_value=False)
    adapter._maybe_handle_healbite_water_pending_reply = AsyncMock(return_value=False)
    adapter._maybe_handle_healbite_profile_update = AsyncMock(return_value=False)

    adapter._send_healbite_shopping_result = AsyncMock()
    adapter._send_healbite_inventory_result = AsyncMock()
    adapter._send_healbite_weekly_menu_result = AsyncMock()
    adapter._healbite_shopping_keyboard = Mock(return_value=None)
    adapter._healbite_inventory_keyboard = Mock(return_value=None)
    adapter._healbite_weekly_menu_keyboard = Mock(return_value=None)

    # Wire real controllers backed by test DB
    adapter._shopping_telegram = build_shopping_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=now_fn,
    )
    adapter._weekly_menu_telegram = build_weekly_menu_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=now_fn,
    )
    adapter._inventory_telegram = build_inventory_telegram_controller(
        db_path=db_path,
        env=env,
        now_factory=now_fn,
    )
    adapter._family_telegram = HealBiteFamilyTelegramController(
        db_path=db_path,
        config=HouseholdFeatureConfig(
            enabled=True,
            allowlist=frozenset({ACTOR}),
            allowlist_valid=True,
            public_access=True,
        ),
    )
    adapter._fridge_menu_telegram = Mock()
    adapter._fridge_menu_telegram.cancel_pending = Mock()
    adapter._fridge_menu_telegram.pending_input_kind = Mock(return_value=None)

    return adapter


# =========================================================================
# Case A: Shopping Entrypoint
# =========================================================================

@pytest.mark.asyncio
async def test_case_a_shopping_entrypoint_with_existing_list(tmp_path):
    """Case A: User sends/selects '🛒 Список покупок' with existing active list.

    Must route to HealBiteShoppingTelegramController.home(actor) and display
    existing items, NEVER /shopping_add help.
    """
    db_path, personal, context, _, _, _, shopping_store, env = _setup_test_environment(tmp_path)
    adapter = _build_test_adapter(tmp_path, env)

    # Seed an active shopping list for week 2026-09-21 with multiple items
    created = shopping_store.create_shopping_list(
        context,
        personal.household.id,
        week_start=WEEK_START,
        idempotency_key="seed-case-a",
    )
    activated = shopping_store.activate_shopping_list(
        context,
        created.shopping_list.id,
        expected_version=created.shopping_list.version,
        idempotency_key="activate-case-a",
    )
    v1 = shopping_store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(
            display_name="Картофель",
            quantity_value="30",
            quantity_unit_normalized=ShoppingUnit.KG,
        ),
        expected_list_version=activated.shopping_list.version,
        idempotency_key="item-1-case-a",
    )
    shopping_store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(
            display_name="Молоко",
            quantity_value="2",
            quantity_unit_normalized=ShoppingUnit.L,
        ),
        expected_list_version=v1.shopping_list.version,
        idempotency_key="item-2-case-a",
    )

    for entry_text in ["🛒 Список покупок", "Список покупок", "список покупок"]:
        adapter._send_healbite_shopping_result.reset_mock()
        adapter._enqueue_text_event.reset_mock()

        update = _make_update(entry_text)
        await adapter._handle_text_message(update, SimpleNamespace())

        # Must have sent shopping result directly
        adapter._send_healbite_shopping_result.assert_awaited_once()
        result = adapter._send_healbite_shopping_result.await_args.args[1]

        assert result.state == "home"
        assert "Картофель" in result.screen.text
        assert "Молоко" in result.screen.text
        assert SHOPPING_ADD_HELP not in result.screen.text
        assert "Добавьте товар командой" not in result.screen.text
        adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_case_a_shopping_entrypoint_empty_state(tmp_path):
    """Case A: When no shopping list exists, show canonical empty UI with 'Сформировать по меню'."""
    db_path, personal, context, _, _, _, shopping_store, env = _setup_test_environment(tmp_path)
    adapter = _build_test_adapter(tmp_path, env)

    update = _make_update("🛒 Список покупок")
    await adapter._handle_text_message(update, SimpleNamespace())

    adapter._send_healbite_shopping_result.assert_awaited_once()
    result = adapter._send_healbite_shopping_result.await_args.args[1]

    assert result.state == "empty"
    assert "Список на эту неделю пока не создан" in result.screen.text
    assert any("Сформировать по меню" in btn[0] for row in result.screen.rows for btn in row)
    assert SHOPPING_ADD_HELP not in result.screen.text


# =========================================================================
# Case B: Product List Routing
# =========================================================================

@pytest.mark.asyncio
async def test_case_b_full_product_list_routes_to_inventory_not_search(tmp_path):
    """Case B: Comma-separated product list routes to inventory ingestion, not public search."""
    db_path, personal, context, _, inventory_store, _, _, env = _setup_test_environment(tmp_path)
    adapter = _build_test_adapter(tmp_path, env)

    product_list_text = (
        "лук репчатый 2 кг, морковь 2 кг, картофель 30 кг, "
        "яйца куриные 30 шт, перец болгарский 2 кг, "
        "томаты 5 кг, огурцы 2 кг, "
        "мясо свинина шея 3 кг, "
        "мясо свинина балык 2 кг, "
        "говядина шея для фарша 2 кг, "
        "говядина для тушения 3 кг, "
        "форель радужная 3 кг, "
        "рис 2 кг, горох 2 кг, "
        "гречка 2 кг, крупа пшеничная 2 кг."
    )

    update = _make_update(product_list_text)
    await adapter._handle_text_message(update, SimpleNamespace())

    # Must route to inventory workflow, not search
    adapter._send_healbite_inventory_result.assert_awaited_once()
    result = adapter._send_healbite_inventory_result.await_args.args[1]

    assert result.state == "review"
    assert "лук репчатый" in result.screen.text
    assert "картофель" in result.screen.text
    assert "30" in result.screen.text
    # Generic lane must NOT be called
    adapter._enqueue_text_event.assert_not_called()

    # Verify pending snapshot was created in store
    scope = InventoryOwnerScope(household_id=context.household_id)
    pending = inventory_store.get_latest_pending_snapshot(scope)
    assert pending is not None
    assert len(pending.items) == 16


@pytest.mark.asyncio
async def test_case_b_smaller_product_lists_route_to_inventory(tmp_path):
    """Case B: Smaller explicit product lists also route to inventory."""
    db_path, personal, context, _, inventory_store, _, _, env = _setup_test_environment(tmp_path)
    adapter = _build_test_adapter(tmp_path, env)

    for text in [
        "лук 2 кг, картофель 30 кг, яйца 30 шт",
        "морковь 2 кг, рис 2 кг, гречка 2 кг",
    ]:
        adapter._send_healbite_inventory_result.reset_mock()
        adapter._enqueue_text_event.reset_mock()

        update = _make_update(text)
        await adapter._handle_text_message(update, SimpleNamespace())

        adapter._send_healbite_inventory_result.assert_awaited_once()
        result = adapter._send_healbite_inventory_result.await_args.args[1]
        assert result.state == "review"
        adapter._enqueue_text_event.assert_not_called()


# =========================================================================
# Case C & D: Weekly Menu Intent Collision
# =========================================================================

@pytest.mark.asyncio
async def test_case_c_inventory_plus_weekly_menu_intent_does_not_route_to_stats(tmp_path):
    """Case C: 'картофель 30 кг. составить меню на неделю' routes to weekly/inventory menu, not statistics."""
    db_path, personal, context, _, inventory_store, _, _, env = _setup_test_environment(tmp_path)
    adapter = _build_test_adapter(tmp_path, env)

    text = "картофель 30 кг. составить меню на неделю"
    update = _make_update(text)
    await adapter._handle_text_message(update, SimpleNamespace())

    # Must be handled before generic lane
    adapter._enqueue_text_event.assert_not_called()
    # Inventory was ingested from the text
    adapter._send_healbite_inventory_result.assert_awaited_once()
    res = adapter._send_healbite_inventory_result.await_args.args[1]
    assert res.state == "review"
    assert "картофель" in res.screen.text


@pytest.mark.asyncio
async def test_case_d_weekly_menu_intent_routes_to_weekly_menu(tmp_path):
    """Case D: 'собрать меню на неделю из этих продуктов' routes to weekly/inventory menu path."""
    db_path, personal, context, _, inventory_store, _, _, env = _setup_test_environment(tmp_path)
    adapter = _build_test_adapter(tmp_path, env)

    text = "собрать меню на неделю из этих продуктов"
    update = _make_update(text)
    await adapter._handle_text_message(update, SimpleNamespace())

    # Must be routed locally, not enqueued to LLM or stats
    adapter._enqueue_text_event.assert_not_called()


def test_classify_telegram_diary_turn_excludes_weekly_menu_intents():
    """Verify _classify_telegram_diary_turn explicitly rejects weekly menu intents."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="968323641")

    for menu_phrase in [
        "составить меню на неделю",
        "собрать меню на неделю",
        "меню на неделю из этих продуктов",
        "составь недельное меню",
        "что приготовить на неделю из продуктов дома",
        "картофель 30 кг. составить меню на неделю",
    ]:
        res = _classify_telegram_diary_turn(
            source=source,
            event=None,
            message=menu_phrase,
            history=[{"role": "user", "content": "дневник еды"}],
        )
        assert res == "none", f"Expected 'none' for menu intent '{menu_phrase}', got '{res}'"


# =========================================================================
# Case E & F: Statistics Invariant
# =========================================================================

def test_case_e_and_f_statistics_queries_remain_statistics():
    """Case E & F: Statistics queries must be classified as 'summary'."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="968323641")

    stats_queries = [
        "статистика за 7 дней",
        "покажи мою статистику за неделю",
        "калории за последние 7 дней",
        "мой БЖУ за неделю",
    ]

    for query in stats_queries:
        res = _classify_telegram_diary_turn(
            source=source,
            event=None,
            message=query,
            history=[],
        )
        assert res == "summary", f"Expected 'summary' for stats query '{query}', got '{res}'"


# =========================================================================
# Case G: Public Search Fallback Intact
# =========================================================================

@pytest.mark.asyncio
async def test_case_g_unrelated_questions_fall_through_to_generic_search(tmp_path):
    """Case G: Unrelated questions fall through to generic lane / public search."""
    db_path, personal, context, _, _, _, _, env = _setup_test_environment(tmp_path)
    adapter = _build_test_adapter(tmp_path, env)

    for query in [
        "кто открыл Америку?",
        "какая средняя глубина океана?",
        "как написать функцию на Python?",
    ]:
        adapter._send_healbite_shopping_result.reset_mock()
        adapter._send_healbite_inventory_result.reset_mock()
        adapter._send_healbite_weekly_menu_result.reset_mock()
        adapter._enqueue_text_event.reset_mock()

        update = _make_update(query)
        await adapter._handle_text_message(update, SimpleNamespace())

        # Must fall through to generic message queue
        adapter._enqueue_text_event.assert_called_once()
        adapter._send_healbite_shopping_result.assert_not_called()
        adapter._send_healbite_inventory_result.assert_not_called()
        adapter._send_healbite_weekly_menu_result.assert_not_called()


# =========================================================================
# Phase 5: Follow-Up Context
# =========================================================================

@pytest.mark.asyncio
async def test_phase_5_followup_inventory_then_weekly_menu_context(tmp_path):
    """Phase 5: Send <inventory list>, then 'собрать меню на неделю из этих продуктов'.

    The second message must resolve the relevant inventory workflow context
    instead of falling through to statistics.
    """
    db_path, personal, context, _, inventory_store, _, _, env = _setup_test_environment(tmp_path)
    adapter = _build_test_adapter(tmp_path, env)

    # Step 1: Send inventory list
    inv_text = "картофель 30 кг, морковь 2 кг, лук 2 кг"
    update1 = _make_update(inv_text, message_id=2001)
    await adapter._handle_text_message(update1, SimpleNamespace())

    adapter._send_healbite_inventory_result.assert_awaited_once()
    res1 = adapter._send_healbite_inventory_result.await_args.args[1]
    assert res1.state == "review"

    # Step 2: Immediate follow-up
    adapter._send_healbite_inventory_result.reset_mock()
    adapter._send_healbite_weekly_menu_result.reset_mock()
    adapter._enqueue_text_event.reset_mock()

    update2 = _make_update("собрать меню на неделю из этих продуктов", message_id=2002)
    await adapter._handle_text_message(update2, SimpleNamespace())

    # Must NOT fall through to generic lane or statistics
    adapter._enqueue_text_event.assert_not_called()

    # The pending inventory was found and review was shown for confirmation
    adapter._send_healbite_inventory_result.assert_awaited_once()
    res2 = adapter._send_healbite_inventory_result.await_args.args[1]
    assert res2.state == "review"
