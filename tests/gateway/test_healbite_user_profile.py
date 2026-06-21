from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    format_nutrition_diary_report,
    normalize_nutrition_payload,
)
from gateway.healbite_user_profile import (
    HealBiteUserProfileStore,
    format_healbite_profile_report,
)
from gateway.platforms.telegram import (
    HEALBITE_REPLY_KEYBOARD_ROWS,
    TelegramAdapter,
)


def _build_record(*, meal_name: str, calories_kcal: float, protein_g: float, fat_g: float, carbs_g: float):
    return normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": meal_name,
                "raw_summary": f"{meal_name} summary",
                "confidence": 0.9,
                "totals": {
                    "calories_kcal": calories_kcal,
                    "protein_g": protein_g,
                    "fat_g": fat_g,
                    "carbs_g": carbs_g,
                },
                "items": [{"name": meal_name}],
            },
            ensure_ascii=False,
        )
    )


def test_user_profile_store_runs_basic_onboarding_flow(tmp_path):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")

    prompt = store.begin_onboarding(user_id=101, username="oleg")
    invalid = store.handle_onboarding_reply(user_id=101, text="не знаю", username="oleg")
    completed = store.handle_onboarding_reply(user_id=101, text="2000 ккал", username="oleg")
    profile = store.get_user_profile(101)

    assert "норму калорий" in prompt.casefold()
    assert invalid is not None
    assert invalid.status == "invalid"
    assert "Напиши число" in invalid.text
    assert completed is not None
    assert completed.status == "completed"
    assert profile is not None
    assert profile.daily_kcal_target == 2000
    assert store.get_onboarding_state(101) is None


def test_user_profile_store_reuses_legacy_users_table_schema(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                name TEXT,
                access_status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    store = HealBiteUserProfileStore(db_path=db_path)
    profile = store.upsert_user_profile(
        user_id=102,
        username="legacy-user",
        daily_kcal_target=1850,
    )

    assert profile.daily_kcal_target == 1850
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        saved = conn.execute(
            "SELECT telegram_id, daily_kcal_target FROM users WHERE telegram_id = ?",
            (102,),
        ).fetchone()

    assert "daily_kcal_target" in columns
    assert saved == (102, 1850.0)


def test_user_profile_store_updates_existing_legacy_user_target_from_prefixed_reply(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                name TEXT,
                access_status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                daily_kcal_target REAL,
                daily_protein_target REAL,
                daily_fat_target REAL,
                daily_carbs_target REAL
            );
            INSERT INTO users (telegram_id, username, daily_kcal_target)
            VALUES (103, 'legacy-user', NULL);
            """
        )

    store = HealBiteUserProfileStore(db_path=db_path)
    store.begin_onboarding(user_id=103, username="legacy-user")
    completed = store.handle_onboarding_reply(user_id=103, text="б 1950", username="legacy-user")

    assert completed is not None
    assert completed.status == "completed"
    profile = store.get_user_profile(103)
    assert profile is not None
    assert profile.daily_kcal_target == 1950
    with sqlite3.connect(db_path) as conn:
        saved = conn.execute(
            "SELECT daily_kcal_target FROM users WHERE telegram_id = ?",
            (103,),
        ).fetchone()
    assert saved == (1950.0,)


def test_user_profile_store_uses_profile_table_target_fallback(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                daily_kcal_target REAL,
                daily_protein_target REAL,
                daily_fat_target REAL,
                daily_carbs_target REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE profiles (
                telegram_id INTEGER PRIMARY KEY,
                calories_limit REAL
            );
            INSERT INTO users (telegram_id, username, daily_kcal_target)
            VALUES (104, 'legacy-user', NULL);
            INSERT INTO profiles (telegram_id, calories_limit)
            VALUES (104, 1950);
            """
        )

    store = HealBiteUserProfileStore(db_path=db_path)
    profile = store.get_user_profile(104)

    assert profile is not None
    assert profile.daily_kcal_target == 1950


def test_format_healbite_profile_report_handles_missing_profile():
    report = format_healbite_profile_report(None)

    assert "Профиль" in report
    assert "/start" in report
    assert "не настроена" in report


def test_diary_reads_targets_from_users_table(tmp_path):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    store.upsert_user_profile(
        user_id=501,
        username="target-user",
        daily_kcal_target=1700,
        daily_protein_target=110,
        daily_fat_target=60,
        daily_carbs_target=180,
    )
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    diary.save_record(
        user_id=501,
        source="vision",
        record=_build_record(
            meal_name="Семга",
            calories_kcal=1450,
            protein_g=95,
            fat_g=45,
            carbs_g=120,
        ),
        image_ref="telegram:501:1",
        occurred_at=datetime.now(timezone.utc),
    )

    report = format_nutrition_diary_report(diary.get_daily_summary(user_id=501))

    assert "1450 ккал / 1700 ккал" in report
    assert "95 г / 110 г" in report
    assert "45 г / 60 г" in report


def test_diary_reads_targets_from_legacy_users_identity_column(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                daily_kcal_target REAL,
                daily_protein_target REAL,
                daily_fat_target REAL,
                daily_carbs_target REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                telegram_id,
                username,
                daily_kcal_target,
                daily_protein_target,
                daily_fat_target,
                daily_carbs_target
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (777, "legacy-target-user", 1700, 110, 60, 180),
        )

    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    diary.save_record(
        user_id=777,
        source="vision",
        record=_build_record(
            meal_name="Семга",
            calories_kcal=1450,
            protein_g=95,
            fat_g=45,
            carbs_g=120,
        ),
        image_ref="telegram:777:1",
        occurred_at=datetime.now(timezone.utc),
    )

    report = format_nutrition_diary_report(diary.get_daily_summary(user_id=777))

    assert "1450 ккал / 1700 ккал" in report
    assert "95 г / 110 г" in report
    assert "45 г / 60 г" in report


def _make_adapter() -> TelegramAdapter:
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter._should_process_message = lambda msg, is_command=False: True
    adapter._maybe_handle_healbite_menu_button = AsyncMock(return_value=False)
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    adapter._build_message_event = lambda msg, message_type, update_id=None: SimpleNamespace(
        text=getattr(msg, "text", ""),
        message_type=message_type,
        media_types=[],
    )
    adapter._clean_bot_trigger_text = lambda text: text
    adapter._largest_photo_size = lambda msg: None
    adapter._cache_photo_message_to_event = AsyncMock()
    adapter._cache_image_document_message_to_event = AsyncMock()
    adapter._link_preview_kwargs = lambda: {}
    adapter._healbite_main_menu_keyboard = lambda: None
    adapter._should_observe_unmentioned_group_message = lambda msg: False
    adapter._observe_unmentioned_group_message = Mock()
    adapter.config = SimpleNamespace(extra={})
    return adapter


def _make_update(text: str, *, user_id: int = 1, username: str = "oleg") -> SimpleNamespace:
    msg = SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=user_id, type="private"),
        from_user=SimpleNamespace(id=user_id, username=username, first_name="Oleg"),
        message_thread_id=None,
        reply_to_message=None,
    )
    return SimpleNamespace(update_id=1, message=msg, effective_message=None)


@pytest.mark.asyncio
async def test_telegram_start_for_new_user_starts_onboarding(tmp_path, monkeypatch):
    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    await adapter._handle_command(_make_update("/start", user_id=701), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "норму калорий" in kwargs["text"].casefold()
    assert store.get_onboarding_state(701) is not None
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_start_for_existing_user_without_target_starts_onboarding(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                daily_kcal_target REAL,
                daily_protein_target REAL,
                daily_fat_target REAL,
                daily_carbs_target REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users (telegram_id, username, daily_kcal_target)
            VALUES (709, 'oleg', NULL);
            """
        )

    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )
    store = HealBiteUserProfileStore(db_path=db_path)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    await adapter._handle_command(_make_update("/start", user_id=709), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "норму калорий" in kwargs["text"].casefold()
    assert store.get_onboarding_state(709) is not None
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_profile_command_renders_saved_profile(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=702, username="oleg", daily_kcal_target=2000)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    await adapter._handle_command(_make_update("/profile", user_id=702), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "👤 Профиль" in kwargs["text"]
    assert "2000 ккал" in kwargs["text"]
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_onboarding_reply_saves_profile_and_short_circuits(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=703, username="oleg")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    await adapter._handle_text_message(_make_update("2000", user_id=703), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Базовый профиль сохранён" in kwargs["text"]
    assert store.get_user_profile(703) is not None
    assert store.get_onboarding_state(703) is None
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_onboarding_reply_accepts_prefixed_kcal_input(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=704, username="oleg")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    await adapter._handle_text_message(_make_update("б 1950", user_id=704), SimpleNamespace())

    profile = store.get_user_profile(704)
    assert profile is not None
    assert profile.daily_kcal_target == 1950
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "1950 ккал" in kwargs["text"]
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_rich_menu_profile_button_routes_to_profile(tmp_path, monkeypatch):
    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=705, username="oleg", daily_kcal_target=2000)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    await adapter._handle_text_message(_make_update("👤 Мой профиль", user_id=705), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "👤 Профиль" in kwargs["text"]
    assert "2000 ккал" in kwargs["text"]
    adapter.handle_message.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_profile_button_and_slash_use_same_effective_target_fallback(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                daily_kcal_target REAL,
                daily_protein_target REAL,
                daily_fat_target REAL,
                daily_carbs_target REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE profiles (
                telegram_id INTEGER PRIMARY KEY,
                calories_limit REAL
            );
            INSERT INTO users (telegram_id, username, daily_kcal_target)
            VALUES (715, 'oleg', NULL);
            INSERT INTO profiles (telegram_id, calories_limit)
            VALUES (715, 1950);
            """
        )

    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )
    store = HealBiteUserProfileStore(db_path=db_path)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    await adapter._handle_command(_make_update("/profile", user_id=715), SimpleNamespace())
    slash_text = adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    adapter._send_message_with_thread_fallback.reset_mock()
    await adapter._handle_text_message(_make_update("👤 Мой профиль", user_id=715), SimpleNamespace())
    button_text = adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    assert "1950 ккал" in slash_text
    assert button_text == slash_text
    adapter.handle_message.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_rich_menu_weekly_stats_button_routes_to_stats_7d(monkeypatch):
    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )
    captured: dict[str, int] = {}

    def fake_compute_nutrition_diary_summary(*, user_id: int, days: int):
        captured["user_id"] = user_id
        captured["days"] = days
        return object()

    monkeypatch.setattr(
        "gateway.platforms.telegram.compute_nutrition_diary_summary",
        fake_compute_nutrition_diary_summary,
    )
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_nutrition_diary_report",
        lambda summary: "weekly-report",
    )

    await adapter._handle_text_message(_make_update("📈 Отчет за неделю", user_id=706), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs["text"] == "weekly-report"
    assert captured == {"user_id": 706, "days": 7}
    adapter.handle_message.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_rich_menu_placeholder_button_returns_stub():
    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )

    await adapter._handle_text_message(_make_update("💧 Трекер воды", user_id=707), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs["text"] == "В разработке"
    adapter.handle_message.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_multiline_button_input_gets_local_single_action_reply():
    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )

    await adapter._handle_text_message(
        _make_update("👤 Мой профиль\n📋 Меню на неделю", user_id=716),
        SimpleNamespace(),
    )

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "одну команду" in kwargs["text"]
    adapter.handle_message.assert_not_called()
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_multiline_command_input_gets_local_single_action_reply():
    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )

    await adapter._handle_command(
        _make_update("/start\n/profile", user_id=717),
        SimpleNamespace(),
    )

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "одну команду" in kwargs["text"]
    adapter.handle_message.assert_not_called()


def test_healbite_reply_keyboard_rows_match_rich_layout():
    assert HEALBITE_REPLY_KEYBOARD_ROWS == [
        ["👤 Мой профиль", "🍎 Дневник еды"],
        ["📋 Меню на неделю", "🛒 Список покупок"],
        ["⚖️ Трекер веса", "💧 Трекер воды"],
        ["👨‍👩‍👧 Семья", "📈 Отчет за неделю"],
        ["⚙️ Ограничения", "❓ Помощь"],
    ]



@pytest.mark.asyncio
async def test_telegram_profile_command_logs_profile_route(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=720, username="oleg", daily_kcal_target=1950)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    with caplog.at_level("DEBUG", logger="gateway.platforms.telegram"):
        await adapter._handle_command(_make_update("/profile", user_id=720), SimpleNamespace())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "healbite_route_selected" in joined
    assert "route=profile" in joined
    assert "720" not in joined


@pytest.mark.asyncio
async def test_telegram_stats_command_logs_stats_route(monkeypatch, caplog):
    adapter = _make_adapter()
    monkeypatch.setattr(
        "gateway.platforms.telegram.compute_nutrition_diary_summary",
        lambda **kwargs: {"entries": [], "entry_count": 0, "calories_kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "days": kwargs.get("days", 7)},
    )
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_nutrition_diary_report",
        lambda summary: "weekly report" if summary["days"] == 7 else "daily report",
    )

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._handle_command(_make_update("/stats 7d", user_id=721), SimpleNamespace())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "healbite_route_selected" in joined
    assert "route=stats" in joined
    assert "721" not in joined


@pytest.mark.asyncio
async def test_telegram_keyboard_action_logs_safe_button_label(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter,
        TelegramAdapter,
    )
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=722, username="oleg", daily_kcal_target=1950)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._handle_text_message(_make_update("👤 Мой профиль", user_id=722), SimpleNamespace())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "healbite_route_selected" in joined
    assert "route=keyboard_action" in joined
    assert "action=👤 Мой профиль" in joined
    assert "722" not in joined


@pytest.mark.asyncio
async def test_telegram_multiline_rejection_logs_marker(caplog):
    adapter = _make_adapter()

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._handle_text_message(_make_update("/profile\n/diary", user_id=723), SimpleNamespace())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "route=multiline_rejection" in joined
    assert "/profile" not in joined
    assert "/diary" not in joined


@pytest.mark.asyncio
async def test_telegram_onboarding_reply_logs_marker(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=724, username="oleg")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._handle_text_message(_make_update("2000", user_id=724), SimpleNamespace())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "route=onboarding_reply" in joined
    assert "724" not in joined


@pytest.mark.asyncio
async def test_telegram_generic_lane_text_logs_marker(caplog):
    adapter = _make_adapter()

    with caplog.at_level("DEBUG", logger="gateway.platforms.telegram"):
        await adapter._handle_text_message(_make_update("привет", user_id=725), SimpleNamespace())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "route=generic_lane" in joined
    assert "lane=text" in joined
    assert "привет" not in joined


@pytest.mark.asyncio
async def test_telegram_public_lane_blocked_logs_marker_for_new_user(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    adapter._should_process_message = lambda msg, is_command=False: False
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._handle_text_message(_make_update("что дальше", user_id=726), SimpleNamespace())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "route=public_lane_blocked" in joined
    assert "lane=healbite_public" in joined
    assert "result=missing_profile" in joined
    assert "что дальше" not in joined
    assert "726" not in joined
