from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_weekly_menu_runtime import HealBiteWeeklyMenuRuntimeService
from gateway.healbite_weekly_menu_schema import WeeklyMenuMealSlot
from gateway.healbite_weekly_menu_telegram import (
    WEEKLY_MENU_COMMAND,
    WEEKLY_MENU_EMPTY_REPLY,
    WEEKLY_MENU_PLACEHOLDER_REPLY,
    WEEKLY_MENU_UNAVAILABLE_REPLY,
    WEEKLY_MENU_MAX_CHUNK_LENGTH,
    current_week_start,
    render_weekly_menu,
    resolve_weekly_menu_presentation,
)
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore, WeeklyMenuEntryInput
from gateway.platforms.telegram import HEALBITE_REPLY_KEYBOARD_ACTIONS, TelegramAdapter


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


def _weekly_entries(
    *,
    breakfast_title: str = "Овсяная каша",
    lunch_title: str = "Куриный суп",
    dinner_title: str = "Рыба с овощами",
) -> list[WeeklyMenuEntryInput]:
    return [
        WeeklyMenuEntryInput(local_date="2026-07-06", meal_slot=WeeklyMenuMealSlot.BREAKFAST, position=1, title=breakfast_title),
        WeeklyMenuEntryInput(local_date="2026-07-06", meal_slot=WeeklyMenuMealSlot.LUNCH, position=1, title=lunch_title),
        WeeklyMenuEntryInput(local_date="2026-07-07", meal_slot=WeeklyMenuMealSlot.DINNER, position=1, title=dinner_title),
    ]


def _seed_household(db_path: Path, *, actor_user_id: int) -> object:
    _create_users_table(db_path)
    _insert_user(db_path, actor_user_id)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(actor_user_id)
    return household_store.resolve_actor_context(actor_user_id)


def _publish_week(db_path: Path, *, actor_user_id: int, entries: list[WeeklyMenuEntryInput] | None = None):
    context = _seed_household(db_path, actor_user_id=actor_user_id)
    store = HealBiteWeeklyMenuStore(db_path=db_path)
    store.initialize_schema()
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = store.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key=f"draft-{actor_user_id}",
    )
    ready = store.replace_draft_entries(
        context,
        draft.revision.id,
        entries or _weekly_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key=f"replace-{actor_user_id}",
    )
    published = store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key=f"publish-{actor_user_id}",
    )
    return store, context, published


def _draft_only_week(db_path: Path, *, actor_user_id: int, entries: list[WeeklyMenuEntryInput] | None = None):
    context = _seed_household(db_path, actor_user_id=actor_user_id)
    store = HealBiteWeeklyMenuStore(db_path=db_path)
    store.initialize_schema()
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = store.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key=f"draft-only-{actor_user_id}",
    )
    draft_view = store.replace_draft_entries(
        context,
        draft.revision.id,
        entries or _weekly_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key=f"replace-draft-only-{actor_user_id}",
    )
    return store, context, draft_view


def _runtime(db_path: Path, *, allowlist: set[int], enabled: bool = True) -> HealBiteWeeklyMenuRuntimeService:
    return HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=enabled, allowlist=frozenset(allowlist), configuration_valid=True),
        db_path=db_path,
    )


def _message(*, text: str | None = None, user_id: int = 101):
    effective_text = text if text is not None else _weekly_button_label()
    return SimpleNamespace(
        text=effective_text,
        chat=SimpleNamespace(id=555, type="private"),
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester"),
        message_id=42,
        message_thread_id=None,
    )


def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={}))
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter._healbite_now_utc = lambda: datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    return adapter


def _weekly_button_label() -> str:
    for label, action in HEALBITE_REPLY_KEYBOARD_ACTIONS.items():
        if action == WEEKLY_MENU_COMMAND:
            return label
    raise AssertionError("weekly menu button label missing")


def _table_count(db_path: Path, table: str) -> int:
    with _connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _install_abort_trigger(db_path: Path, table: str) -> None:
    with _connect(db_path) as conn:
        for operation in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"):
            if operation in {"CREATE", "DROP", "ALTER"}:
                continue
            conn.execute(
                f"CREATE TRIGGER trg_{table}_{operation.lower()}_blocked BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'writes_blocked'); END"
            )


def test_current_week_start_uses_injected_timezone():
    assert current_week_start(
        now=datetime(2026, 7, 5, 23, 30, tzinfo=timezone.utc),
        timezone_name="Asia/Barnaul",
    ) == "2026-07-06"
    assert current_week_start(
        now=datetime(2026, 7, 5, 23, 30, tzinfo=timezone.utc),
        timezone_name="UTC",
    ) == "2026-06-29"


def test_render_weekly_menu_formats_full_week_and_escapes_dynamic_titles(tmp_path):
    db_path = tmp_path / "healbite.db"
    store, context, published = _publish_week(
        db_path,
        actor_user_id=101,
        entries=_weekly_entries(
            breakfast_title="Овсяная <каша>",
            lunch_title="Салат & суп",
            dinner_title="Рыба 😋",
        ),
    )
    view = store.get_weekly_menu_revision(context, published.revision.id)

    chunks = render_weekly_menu(view)
    text = "\n\n".join(chunks)

    assert "📋 Меню на неделю" in text
    assert "6–12 июля" in text
    assert "Понедельник, 6 июля" in text
    assert "Овсяная &lt;каша&gt;" in text
    assert "Салат &amp; суп" in text
    assert "Рыба 😋" in text
    assert "—" in text


def test_render_weekly_menu_chunks_long_output(tmp_path):
    db_path = tmp_path / "healbite.db"
    long_title = ("Очень длинное блюдо с полезным описанием " * 4).strip()
    entries = []
    for offset in range(7):
        local_date = f"2026-07-{6 + offset:02d}"
        for position in range(1, 9):
            entries.append(
                WeeklyMenuEntryInput(
                    local_date=local_date,
                    meal_slot=WeeklyMenuMealSlot.BREAKFAST,
                    position=position,
                    title=f"{long_title}{offset}-{position}",
                )
            )
    store, context, published = _publish_week(db_path, actor_user_id=101, entries=entries)
    view = store.get_weekly_menu_revision(context, published.revision.id)

    chunks = render_weekly_menu(view)

    assert len(chunks) >= 2
    assert all(len(chunk) <= WEEKLY_MENU_MAX_CHUNK_LENGTH for chunk in chunks)
    assert sum(chunk.count("📋 Меню на неделю") for chunk in chunks) == 1


def test_render_weekly_menu_truncates_oversized_single_entry_and_keeps_chunk_bounded(tmp_path):
    oversized_title = ("<очень длинное блюдо> & " * 400).strip()
    view = SimpleNamespace(
        series=SimpleNamespace(week_start="2026-07-06"),
        entries=[
            SimpleNamespace(
                local_date="2026-07-06",
                meal_slot=SimpleNamespace(value=WeeklyMenuMealSlot.BREAKFAST.value),
                position=1,
                title=oversized_title,
            ),
        ],
    )

    chunks = render_weekly_menu(view)
    text = "\n".join(chunks)

    assert all(len(chunk) <= WEEKLY_MENU_MAX_CHUNK_LENGTH for chunk in chunks)
    assert "…" in text
    assert "&lt;очень длинное блюдо&gt;" in text


def test_resolve_weekly_menu_presentation_returns_placeholder_for_disabled_feature(tmp_path):
    db_path = tmp_path / "healbite.db"
    presentation = resolve_weekly_menu_presentation(
        actor_user_id=101,
        runtime_service=_runtime(db_path, allowlist={101}, enabled=False),
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    assert presentation.state == "placeholder"
    assert presentation.chunks == (WEEKLY_MENU_PLACEHOLDER_REPLY,)


def test_resolve_weekly_menu_presentation_returns_empty_for_draft_only_week(tmp_path):
    db_path = tmp_path / "healbite.db"
    _draft_only_week(db_path, actor_user_id=101)

    presentation = resolve_weekly_menu_presentation(
        actor_user_id=101,
        runtime_service=_runtime(db_path, allowlist={101}),
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    assert presentation.state == "empty"
    assert presentation.chunks == (WEEKLY_MENU_EMPTY_REPLY,)


def test_resolve_weekly_menu_presentation_hides_draft_content_when_published_exists(tmp_path):
    db_path = tmp_path / "healbite.db"
    store, context, published = _publish_week(db_path, actor_user_id=101, entries=_weekly_entries(breakfast_title="Публикация"))
    draft = store.create_draft_revision(
        context,
        published.series.id,
        expected_series_version=published.series.version,
        idempotency_key="draft-after-publish",
    )
    store.replace_draft_entries(
        context,
        draft.revision.id,
        _weekly_entries(breakfast_title="Черновик"),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-after-publish",
    )

    presentation = resolve_weekly_menu_presentation(
        actor_user_id=101,
        runtime_service=_runtime(db_path, allowlist={101}),
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )
    text = "\n".join(presentation.chunks)

    assert presentation.state == "published"
    assert "Публикация" in text
    assert "Черновик" not in text
    assert "draft" not in text.casefold()


def test_resolve_weekly_menu_presentation_hides_foreign_household_menu(tmp_path):
    db_path = tmp_path / "healbite.db"
    _publish_week(db_path, actor_user_id=202, entries=_weekly_entries(breakfast_title="Чужое меню"))
    _seed_household(db_path, actor_user_id=101)

    presentation = resolve_weekly_menu_presentation(
        actor_user_id=101,
        runtime_service=_runtime(db_path, allowlist={101, 202}),
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    assert presentation.state == "empty"
    assert presentation.chunks == (WEEKLY_MENU_EMPTY_REPLY,)


@pytest.mark.asyncio
async def test_weekly_menu_keyboard_routes_to_local_read_only_adapter_without_generic_dispatch(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _publish_week(db_path, actor_user_id=101)
    monkeypatch.setattr(
        "gateway.platforms.telegram.build_weekly_menu_runtime_service",
        lambda: _runtime(db_path, allowlist={101}),
    )
    adapter = _adapter()
    adapter._should_process_message = lambda msg, is_command=False: True
    update = SimpleNamespace(update_id=1, message=_message(), effective_message=None)

    handled = await adapter._maybe_handle_healbite_menu_button(update, SimpleNamespace())

    assert handled is True
    adapter._enqueue_text_event.assert_not_called()
    sent = adapter._send_message_with_thread_fallback.await_args_list[0].kwargs
    assert sent["parse_mode"] is not None
    assert "Меню на неделю" in sent["text"]


@pytest.mark.asyncio
async def test_weekly_menu_unavailable_schema_returns_safe_message(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _seed_household(db_path, actor_user_id=101)
    monkeypatch.setattr(
        "gateway.platforms.telegram.build_weekly_menu_runtime_service",
        lambda: _runtime(db_path, allowlist={101}),
    )
    adapter = _adapter()

    handled = await adapter._maybe_handle_healbite_weekly_menu_command(_message(text=WEEKLY_MENU_COMMAND))

    assert handled is True
    assert adapter._send_message_with_thread_fallback.await_args.kwargs["text"] == WEEKLY_MENU_UNAVAILABLE_REPLY


@pytest.mark.asyncio
async def test_weekly_menu_transport_edge_returns_safe_unavailable_on_unexpected_exception(monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.telegram.build_weekly_menu_presentation_for_now",
        Mock(side_effect=RuntimeError("boom")),
    )
    adapter = _adapter()

    handled = await adapter._maybe_handle_healbite_weekly_menu_command(_message(text=WEEKLY_MENU_COMMAND))

    assert handled is True
    assert adapter._send_message_with_thread_fallback.await_args.kwargs["text"] == WEEKLY_MENU_UNAVAILABLE_REPLY


@pytest.mark.asyncio
async def test_weekly_menu_button_flow_does_not_mutate_business_rows_and_survives_write_blocking(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _publish_week(db_path, actor_user_id=101)
    monkeypatch.setattr(
        "gateway.platforms.telegram.build_weekly_menu_runtime_service",
        lambda: _runtime(db_path, allowlist={101}),
    )
    before = {
        "series": _table_count(db_path, "household_weekly_menu_series"),
        "revisions": _table_count(db_path, "household_weekly_menus"),
        "entries": _table_count(db_path, "household_weekly_menu_entries"),
        "idempotency": _table_count(db_path, "household_weekly_menu_idempotency"),
    }
    for table in (
        "household_weekly_menu_series",
        "household_weekly_menus",
        "household_weekly_menu_entries",
        "household_weekly_menu_idempotency",
    ):
        _install_abort_trigger(db_path, table)
    adapter = _adapter()

    handled = await adapter._maybe_handle_healbite_weekly_menu_command(_message(text=WEEKLY_MENU_COMMAND))

    after = {
        "series": _table_count(db_path, "household_weekly_menu_series"),
        "revisions": _table_count(db_path, "household_weekly_menus"),
        "entries": _table_count(db_path, "household_weekly_menu_entries"),
        "idempotency": _table_count(db_path, "household_weekly_menu_idempotency"),
    }
    assert handled is True
    assert before == after


@pytest.mark.asyncio
async def test_weekly_menu_parallel_actor_isolation(tmp_path):
    db_path = tmp_path / "healbite.db"
    _publish_week(db_path, actor_user_id=101, entries=_weekly_entries(breakfast_title="Меню A"))
    _publish_week(db_path, actor_user_id=202, entries=_weekly_entries(breakfast_title="Меню B"))
    runtime = _runtime(db_path, allowlist={101, 202})

    results = await asyncio.gather(
        asyncio.to_thread(
            resolve_weekly_menu_presentation,
            actor_user_id=101,
            runtime_service=runtime,
            now=datetime(2026, 7, 8, tzinfo=timezone.utc),
        ),
        asyncio.to_thread(
            resolve_weekly_menu_presentation,
            actor_user_id=202,
            runtime_service=runtime,
            now=datetime(2026, 7, 8, tzinfo=timezone.utc),
        ),
    )

    text_a = "\n".join(results[0].chunks)
    text_b = "\n".join(results[1].chunks)
    assert "Меню A" in text_a
    assert "Меню B" not in text_a
    assert "Меню B" in text_b
    assert "Меню A" not in text_b


def test_importing_weekly_menu_telegram_module_has_no_startup_side_effects():
    command = (
        "import gateway.healbite_weekly_menu_telegram as mod; "
        "assert mod.WEEKLY_MENU_COMMAND == '/weekly_menu'"
    )
    result = subprocess.run(
        [sys.executable, "-c", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
