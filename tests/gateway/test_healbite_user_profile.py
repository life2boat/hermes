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
    format_healbite_targets_report,
    is_healbite_profile_edit_intent,
    parse_manual_nutrition_targets,
    parse_profile_field_update,
)
from gateway.platforms.telegram import (
    HEALBITE_PUBLIC_REPLY_KEYBOARD_ROWS,
    HEALBITE_REPLY_KEYBOARD_ROWS,
    TelegramAdapter,
)

try:
    from telegram.constants import ParseMode
except ImportError:
    ParseMode = None



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


def _complete_onboarding(store: HealBiteUserProfileStore, *, user_id: int, username: str = "oleg", manual_target: str = "2000"):
    reply = None
    for answer in (
        "Мужской",
        "35",
        "180",
        "85",
        "Поддержание веса",
        "Умеренная активность",
        manual_target,
    ):
        reply = store.handle_onboarding_reply(user_id=user_id, text=answer, username=username)
        assert reply is not None
    return reply


def test_user_profile_store_runs_extended_onboarding_flow(tmp_path):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")

    prompt = store.begin_onboarding(user_id=101, username="oleg")
    first = store.handle_onboarding_reply(user_id=101, text="Мужской", username="oleg")
    invalid_age = store.handle_onboarding_reply(user_id=101, text="abc", username="oleg")
    completed = _complete_onboarding(store, user_id=101, username="oleg", manual_target="2000")
    profile = store.get_user_profile(101)

    assert "настроим профиль" in prompt.casefold()
    assert first is not None and first.status == "next"
    assert invalid_age is not None and invalid_age.status == "invalid"
    assert "число от 18 до 100" in invalid_age.text
    assert completed is not None and completed.status == "completed"
    assert profile is not None
    assert profile.sex == "male"
    assert profile.age == 35
    assert profile.height_cm == 180
    assert profile.weight_kg == 85
    assert profile.goal == "maintain"
    assert profile.activity_level == "moderate"
    assert profile.daily_kcal_target == 2000
    assert profile.daily_protein_g == 136
    assert profile.daily_fat_g == 68
    assert profile.daily_carbs_g == 211
    assert profile.target_source == "manual"
    assert profile.nutrition_calculation_version == "mifflin_v1"
    assert store.get_onboarding_state(101) is None


def test_user_profile_store_reuses_legacy_users_table_schema_and_adds_new_macro_columns(tmp_path):
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
        daily_protein_g=120,
        daily_fat_g=60,
        daily_carbs_g=170,
    )

    assert profile.daily_kcal_target == 1850
    assert profile.daily_protein_g == 120
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        saved = conn.execute(
            "SELECT telegram_id, daily_kcal_target, daily_protein_g, daily_protein_target FROM users WHERE telegram_id = ?",
            (102,),
        ).fetchone()

    assert {"daily_protein_g", "daily_fat_g", "daily_carbs_g", "nutrition_calculation_version", "target_source"} <= columns
    assert saved == (102, 1850.0, 120.0, 120.0)


def test_user_profile_store_uses_profile_table_manual_target_fallback(tmp_path):
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
                age INTEGER,
                sex TEXT,
                height_cm INTEGER,
                weight_kg REAL,
                goal TEXT,
                activity TEXT,
                calories_limit REAL
            );
            INSERT INTO users (telegram_id, username, daily_kcal_target)
            VALUES (104, 'legacy-user', NULL);
            INSERT INTO profiles (telegram_id, age, sex, height_cm, weight_kg, goal, activity, calories_limit)
            VALUES (104, 35, 'male', 180, 85, 'maintain', 'moderate', 1950);
            """
        )

    store = HealBiteUserProfileStore(db_path=db_path)
    profile = store.get_user_profile(104)

    assert profile is not None
    assert profile.manual_kcal_target == 1950
    assert profile.daily_kcal_target == 1950


def test_weekly_menu_profile_snapshot_is_read_only_when_profiles_table_is_missing(tmp_path):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    store.upsert_user_profile(
        user_id=108,
        username="legacy-user",
        daily_kcal_target=1850,
        daily_protein_g=120,
        daily_fat_g=60,
        daily_carbs_g=170,
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE profiles")
        conn.commit()

    read_only_store = HealBiteUserProfileStore(db_path=db_path, ensure_schema_on_init=False)
    snapshot = read_only_store.get_weekly_menu_profile_snapshot(108)

    assert snapshot is not None
    assert snapshot.daily_kcal_target == 1850
    assert snapshot.daily_protein_g == 120
    assert snapshot.daily_fat_g == 60
    assert snapshot.daily_carbs_g == 170
    assert snapshot.dietary_notes == ()

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        users_after = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    assert "profiles" not in tables
    assert users_after == 1


def test_format_healbite_profile_report_handles_missing_profile():
    report = format_healbite_profile_report(None)

    assert "Профиль" in report
    assert "/start" in report
    assert "не настроена" in report


def test_format_healbite_profile_report_renders_extended_profile(tmp_path):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=105, username="oleg")
    _complete_onboarding(store, user_id=105, username="oleg", manual_target="2000")
    report = format_healbite_profile_report(store.get_user_profile(105))

    assert "Ваш профиль" in report
    assert "Поддержание веса" in report
    assert "Мужской" in report
    assert "Умеренная активность" in report
    assert "2000 ккал" in report
    assert "Белки" in report
    assert "Расчёт: Mifflin v1" in report
    assert "справочный характер" in report



def test_manual_target_survives_profile_change_and_recalculates_macros(tmp_path):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=106, username="oleg")
    _complete_onboarding(store, user_id=106, username="oleg", manual_target="2000")

    store.upsert_user_profile(user_id=106, username="oleg", weight_kg=90)
    updated = store.recalculate_profile_targets(
        user_id=106,
        username="oleg",
        target_source="manual",
        manual_kcal_target=2000,
    )

    assert updated.manual_kcal_target == 2000
    assert updated.target_source == "manual"
    assert updated.daily_kcal_target == 2000
    assert updated.daily_protein_g == 144
    assert updated.daily_fat_g == 72
    assert updated.daily_carbs_g == 194


def test_manual_target_can_explicitly_return_to_calculated(tmp_path):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=107, username="oleg")
    _complete_onboarding(store, user_id=107, username="oleg", manual_target="2000")

    prompt = store.begin_onboarding(user_id=107, username="oleg", edit_mode=True)
    assert "обновим профиль" in prompt.casefold()
    for answer in (
        "Мужской",
        "35",
        "180",
        "85",
        "Поддержание веса",
        "Умеренная активность",
        "Рассчитать автоматически",
    ):
        reply = store.handle_onboarding_reply(user_id=107, text=answer, username="oleg")
        assert reply is not None

    profile = store.get_user_profile(107)
    assert profile is not None
    assert profile.target_source == "calculated"
    assert profile.daily_kcal_target == 2798
    assert profile.daily_protein_g == 136
    assert profile.daily_fat_g == 68
    assert profile.daily_carbs_g == 410


def test_incomplete_profile_keeps_manual_target_when_calculated_restore_fails(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            '''
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                daily_kcal_target REAL,
                daily_protein_target REAL,
                daily_fat_target REAL,
                daily_carbs_target REAL,
                daily_protein_g REAL,
                daily_fat_g REAL,
                daily_carbs_g REAL,
                target_source TEXT,
                nutrition_calculation_version TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE profiles (
                telegram_id INTEGER PRIMARY KEY,
                calories_limit REAL,
                goal TEXT,
                activity TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users (user_id, username, daily_kcal_target, target_source)
            VALUES (108, 'oleg', 1950, 'manual');
            INSERT INTO profiles (telegram_id, calories_limit, goal, activity)
            VALUES (108, 1950, 'maintain', 'moderate');
            '''
        )

    store = HealBiteUserProfileStore(db_path=db_path)
    with pytest.raises(Exception):
        store.recalculate_profile_targets(user_id=108, username="oleg", target_source="calculated")

    profile = store.get_user_profile(108)
    assert profile is not None
    assert profile.manual_kcal_target == 1950
    assert profile.daily_kcal_target == 1950
    assert profile.target_source == "manual"


def test_diary_reads_targets_from_new_macro_columns(tmp_path):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    store.upsert_user_profile(
        user_id=501,
        username="target-user",
        daily_kcal_target=1700,
        daily_protein_g=110,
        daily_fat_g=60,
        daily_carbs_g=180,
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



def test_profile_recalculation_log_is_pii_safe(tmp_path, caplog):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=109, username="secret-user")
    _complete_onboarding(store, user_id=109, username="secret-user", manual_target="2000")

    with caplog.at_level("INFO", logger="gateway.healbite_user_profile"):
        store.recalculate_profile_targets(
            user_id=109,
            username="secret-user",
            target_source="manual",
            manual_kcal_target=2000,
        )

    assert "nutrition_profile_recalculated" in caplog.text
    assert "target_source=manual" in caplog.text
    assert "goal=maintain" in caplog.text
    assert "activity_level=moderate" in caplog.text
    assert "secret-user" not in caplog.text
    assert "109" not in caplog.text
    assert "35" not in caplog.text
    assert "180" not in caplog.text
    assert "85" not in caplog.text
    assert "2000" not in caplog.text


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
    adapter._healbite_main_menu_keyboard = lambda: HEALBITE_REPLY_KEYBOARD_ROWS
    adapter._healbite_reply_keyboard = lambda rows: rows
    adapter._should_observe_unmentioned_group_message = lambda msg: False
    adapter._observe_unmentioned_group_message = Mock()
    adapter.config = SimpleNamespace(extra={"allowed_users": ["*"]})
    return adapter


def _patch_telegram_profile_store(monkeypatch, store: HealBiteUserProfileStore) -> None:
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.platforms.telegram.get_existing_healbite_user_profile", lambda: store)


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
async def test_telegram_start_for_new_user_starts_extended_onboarding(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    _patch_telegram_profile_store(monkeypatch, store)

    await adapter._handle_command(_make_update("/start", user_id=701), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "настроим профиль" in kwargs["text"].casefold()
    assert kwargs["reply_markup"] == [["Мужской", "Женский"]]
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}
    assert store.get_onboarding_state(701) is not None
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_start_for_existing_user_returns_menu_without_reset(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=702, username="oleg")
    _complete_onboarding(store, user_id=702, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    await adapter._handle_command(_make_update("/start", user_id=702), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs["text"]
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}
    assert kwargs["reply_markup"] == HEALBITE_REPLY_KEYBOARD_ROWS
    assert store.get_user_profile(702).daily_kcal_target == 2000


@pytest.mark.asyncio
async def test_telegram_start_edit_opens_safe_reconfiguration_flow(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=703, username="oleg")
    _complete_onboarding(store, user_id=703, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    await adapter._handle_command(_make_update("/start edit", user_id=703), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "обновим профиль" in kwargs["text"].casefold()
    assert kwargs["reply_markup"] == [["Мужской", "Женский"]]
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}
    assert store.get_user_profile(703).daily_kcal_target == 2000
    assert store.get_onboarding_state(703) is not None


@pytest.mark.asyncio
async def test_telegram_profile_command_renders_extended_profile(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=704, username="oleg")
    _complete_onboarding(store, user_id=704, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    await adapter._handle_command(_make_update("/profile", user_id=704), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Ваш профиль" in kwargs["text"]
    assert "2000 ккал" in kwargs["text"]
    assert "Мужской" in kwargs["text"]
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_onboarding_reply_short_circuits_and_advances(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=705, username="oleg")
    _patch_telegram_profile_store(monkeypatch, store)
    water_factory = Mock(side_effect=AssertionError("water tracker must not be created during onboarding"))
    monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", water_factory)

    await adapter._handle_text_message(_make_update("Мужской", user_id=705), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "полных лет" in kwargs["text"]
    assert kwargs["reply_markup"] is None
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}
    assert store.get_onboarding_state(705).step == "age"
    adapter._enqueue_text_event.assert_not_called()
    water_factory.assert_not_called()


def test_route_marker_log_is_pii_safe(caplog):
    adapter = object.__new__(TelegramAdapter)
    update = _make_update("/profile", user_id=3131313131, username="secret-user")

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_route_selected(route="profile", msg=update.message, update_id=11)

    assert "healbite_route_selected" in caplog.text
    assert "route=profile" in caplog.text
    assert "3131313131" not in caplog.text
    assert "secret-user" not in caplog.text
    assert "/profile" not in caplog.text


@pytest.mark.asyncio
async def test_telegram_start_for_existing_user_renders_rich_dashboard(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=801, username="oleg")
    _complete_onboarding(store, user_id=801, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    adapter._healbite_main_menu_keyboard = TelegramAdapter._healbite_main_menu_keyboard.__get__(adapter, TelegramAdapter)
    adapter._healbite_menu_rows = TelegramAdapter._healbite_menu_rows.__get__(adapter, TelegramAdapter)
    adapter._is_feature_allowlisted = lambda feat, uid: False

    await adapter._handle_command(_make_update("/start", user_id=801), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "С возвращением в HealBite!" in kwargs["text"]
    assert "2000 ккал" in kwargs["text"]
    assert "Питание сегодня:" in kwargs["text"]
    assert "Вода сегодня:" in kwargs["text"]
    assert "Быстрые действия:" in kwargs["text"]
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}
    assert kwargs["reply_markup"] == HEALBITE_PUBLIC_REPLY_KEYBOARD_ROWS


@pytest.mark.asyncio
async def test_telegram_help_command_renders_friendly_guide_and_menu(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    _patch_telegram_profile_store(monkeypatch, store)
    adapter._healbite_main_menu_keyboard = TelegramAdapter._healbite_main_menu_keyboard.__get__(adapter, TelegramAdapter)
    adapter._healbite_menu_rows = TelegramAdapter._healbite_menu_rows.__get__(adapter, TelegramAdapter)
    adapter._is_feature_allowlisted = lambda feat, uid: False
    adapter._maybe_handle_healbite_help_command = TelegramAdapter._maybe_handle_healbite_help_command.__get__(adapter, TelegramAdapter)
    adapter._dispatch_healbite_keyboard_action = TelegramAdapter._dispatch_healbite_keyboard_action.__get__(adapter, TelegramAdapter)

    # Test via /help slash command
    await adapter._handle_command(_make_update("/help", user_id=802), SimpleNamespace())
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Справка по возможностям HealBite" in kwargs["text"]
    assert "Распознавание еды:" in kwargs["text"]
    assert "Трекер воды:" in kwargs["text"]
    assert "Трекер веса:" in kwargs["text"]
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}
    assert kwargs["reply_markup"] == HEALBITE_PUBLIC_REPLY_KEYBOARD_ROWS

    # Test via keyboard button "❓ Помощь"
    await adapter._handle_command(_make_update("❓ Помощь", user_id=802), SimpleNamespace())
    kwargs_btn = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Справка по возможностям HealBite" in kwargs_btn["text"]
    assert kwargs_btn.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}


def test_healbite_main_menu_keyboard_layout_and_feature_gating():
    adapter = object.__new__(TelegramAdapter)
    adapter._healbite_reply_keyboard = lambda rows: rows
    adapter._healbite_menu_rows = TelegramAdapter._healbite_menu_rows.__get__(adapter, TelegramAdapter)
    adapter._healbite_main_menu_keyboard = TelegramAdapter._healbite_main_menu_keyboard.__get__(adapter, TelegramAdapter)

    # User without advanced access gets compact 3-row layout
    adapter._is_feature_allowlisted = lambda feat, uid: False
    public_rows = adapter._healbite_main_menu_keyboard(actor_user_id=101)
    assert public_rows == HEALBITE_PUBLIC_REPLY_KEYBOARD_ROWS
    assert len(public_rows) == 3
    assert public_rows[0] == ["🍎 Дневник", "📊 Статистика"]
    assert public_rows[1] == ["💧 Вода", "⚖️ Вес"]
    assert public_rows[2] == ["👤 Профиль", "❓ Помощь"]

    # Granular gating: only weekly menu enabled
    adapter._is_feature_allowlisted = lambda feat, uid: feat == "HEALBITE_WEEKLY_MENU"
    weekly_only_rows = adapter._healbite_main_menu_keyboard(actor_user_id=101)
    assert ["📋 Меню на неделю"] in weekly_only_rows
    assert not any("🛒 Список покупок" in row for row in weekly_only_rows)
    assert not any("👨‍👩‍👧 Семья" in row for row in weekly_only_rows)

    # Granular gating: weekly menu + shopping list paired
    adapter._is_feature_allowlisted = lambda feat, uid: feat in {"HEALBITE_WEEKLY_MENU", "HEALBITE_SHOPPING_LIST"}
    weekly_shopping_rows = adapter._healbite_main_menu_keyboard(actor_user_id=101)
    assert ["📋 Меню на неделю", "🛒 Список покупок"] in weekly_shopping_rows

    # Granular gating: only households enabled
    adapter._is_feature_allowlisted = lambda feat, uid: feat == "HEALBITE_HOUSEHOLDS"
    family_only_rows = adapter._healbite_main_menu_keyboard(actor_user_id=101)
    assert ["👨‍👩‍👧 Семья"] in family_only_rows
    assert not any("📋 Меню на неделю" in row for row in family_only_rows)

    # All advanced features enabled
    adapter._is_feature_allowlisted = lambda feat, uid: True
    all_rows = adapter._healbite_main_menu_keyboard(actor_user_id=968323641)
    assert ["🍎 Дневник", "📊 Статистика"] in all_rows
    assert ["📋 Меню на неделю", "🛒 Список покупок"] in all_rows
    assert ["🥘 Из холодильника в меню"] in all_rows
    assert ["🥕 Продукты дома"] in all_rows
    assert ["💧 Вода", "⚖️ Вес"] in all_rows
    assert ["👨‍👩‍👧 Семья"] in all_rows
    assert ["👤 Профиль", "❓ Помощь"] in all_rows

    # Verify dead-end restrictions button is NEVER present in any layout
    for rows in (public_rows, weekly_only_rows, weekly_shopping_rows, family_only_rows, all_rows):
        assert not any("⚙️ Ограничения" in row for row in rows)



def test_onboarding_step_progress_and_recovery_formatting(tmp_path):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    prompt_step1 = store.begin_onboarding(user_id=803, username="oleg")
    assert "Шаг 1 из 7 • Пол" in prompt_step1
    assert "настроим профиль" in prompt_step1.casefold()

    reply_step2 = store.handle_onboarding_reply(user_id=803, text="Мужской")
    assert "Шаг 2 из 7 • Возраст" in reply_step2.text

    # Resuming in-progress session
    resumed_prompt = store.begin_onboarding(user_id=803, username="oleg")
    assert "Продолжаем настройку профиля!" in resumed_prompt
    assert "Шаг 2 из 7 • Возраст" in resumed_prompt


@pytest.mark.asyncio
async def test_telegram_onboarding_reply_completion_sets_parse_mode_and_main_menu(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=804, username="oleg")
    # Advance first 6 steps
    for answer in ("Мужской", "35", "180", "85", "Поддержание веса", "Умеренная активность"):
        store.handle_onboarding_reply(user_id=804, text=answer, username="oleg")
    _patch_telegram_profile_store(monkeypatch, store)

    adapter._healbite_main_menu_keyboard = TelegramAdapter._healbite_main_menu_keyboard.__get__(adapter, TelegramAdapter)
    adapter._healbite_menu_rows = TelegramAdapter._healbite_menu_rows.__get__(adapter, TelegramAdapter)
    adapter._is_feature_allowlisted = lambda feat, uid: False

    # Send 7th step through telegram adapter
    await adapter._handle_text_message(_make_update("2000", user_id=804), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}
    assert kwargs["reply_markup"] == HEALBITE_PUBLIC_REPLY_KEYBOARD_ROWS
    assert "Профиль успешно настроен" in kwargs["text"]
    assert store.get_onboarding_state(804) is None


def test_healbite_profile_and_dashboard_escapes_untrusted_html(tmp_path):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=999, username="<attacker>")
    _complete_onboarding(store, user_id=999, username="<attacker>", manual_target="2200")

    # Set untrusted fields with special HTML characters directly on the profile
    profile = store.get_user_profile(999)
    assert profile is not None
    profile.allergies = "<script>alert(1)</script>"
    profile.stop_products = "<b>poison</b> & more"
    profile.preferences = "vegan <raw> \"quotes\""
    profile.budget = "low < cost > 100 &"
    profile.cooking_frequency = "rare < 1"

    report = format_healbite_profile_report(profile)
    assert "<script>" not in report
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report
    assert "<b>poison</b>" not in report
    assert "&lt;b&gt;poison&lt;/b&gt; &amp; more" in report
    assert "&lt;raw&gt;" in report
    assert "&quot;quotes&quot;" in report
    assert "low &lt; cost &gt; 100 &amp;" in report
    assert "rare &lt; 1" in report

    # Test returning dashboard escaping
    adapter = object.__new__(TelegramAdapter)
    adapter._build_healbite_returning_dashboard = TelegramAdapter._build_healbite_returning_dashboard.__get__(adapter, TelegramAdapter)
    mock_store = Mock()
    mock_store.get_user_profile = Mock(return_value=profile)
    mock_store.db_path = tmp_path / "healbite.db"
    import gateway.platforms.telegram as tg_mod
    orig_get_profile = tg_mod.get_default_healbite_user_profile
    try:
        tg_mod.get_default_healbite_user_profile = lambda: mock_store
        dashboard = adapter._build_healbite_returning_dashboard(999)
        assert "<script>" not in dashboard
        assert "<b>С возвращением в HealBite!</b>" in dashboard  # Template bold tag preserved
        assert "<b>Быстрые действия:</b>" in dashboard
    finally:
        tg_mod.get_default_healbite_user_profile = orig_get_profile


def test_is_healbite_profile_edit_intent():
    positive_phrases = [
        "Я хочу внести изменения в профиль",
        "я хочу внести изменения в профиль",
        "Я хочу внести правки в профиль",
        "хочу внести изменения в анкету",
        "хочу изменить профиль",
        "изменить профиль",
        "измени профиль",
        "обновить профиль",
        "обнови анкету",
        "редактировать профиль",
        "отредактировать профиль",
        "поменять профиль",
        "поменять данные профиля",
        "перенастроить профиль",
        "заполнить заново анкету",
        "настроить заново профиль",
        "профиль изменить",
        "анкету обновить",
        "Я хочу изменить свой профиль!",
    ]
    for phrase in positive_phrases:
        assert is_healbite_profile_edit_intent(phrase), f"Failed for positive: {phrase}"

    negative_phrases = [
        "как изменить профиль",
        "где изменить профиль",
        "почему изменить профиль",
        "что такое профиль",
        "покажи профиль",
        "посмотреть профиль",
        "открой профиль",
        "какой у меня профиль",
        "сколько калорий в профиле",
        "мой вес 75",
        "съел банан на 100 ккал",
        "выпил 300 мл воды",
        "привет",
        "/start",
        "/profile",
        "/help",
        "прочитай файл",
        "",
        "   ",
    ]
    for phrase in negative_phrases:
        assert not is_healbite_profile_edit_intent(phrase), f"Failed for negative: {phrase}"


@pytest.mark.asyncio
async def test_telegram_profile_command_includes_edit_button(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=704, username="oleg")
    _complete_onboarding(store, user_id=704, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    await adapter._handle_command(_make_update("/profile", user_id=704), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Ваш профиль" in kwargs["text"]
    keyboard = kwargs.get("reply_markup")
    assert keyboard is not None
    import gateway.platforms.telegram as tg_mod
    if hasattr(keyboard, "inline_keyboard") and not isinstance(keyboard.inline_keyboard, Mock):
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "✏️ Изменить профиль"
        assert button.callback_data == "profile:edit:704"
    elif hasattr(tg_mod.InlineKeyboardButton, "call_args") and tg_mod.InlineKeyboardButton.call_args:
        call = tg_mod.InlineKeyboardButton.call_args
        assert "✏️ Изменить профиль" in call.args
        assert call.kwargs.get("callback_data") == "profile:edit:704"


@pytest.mark.asyncio
async def test_telegram_profile_edit_callback_starts_edit_mode_and_preserves_profile(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=704, username="oleg")
    _complete_onboarding(store, user_id=704, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    initial_profile = store.get_user_profile(704)
    assert initial_profile.daily_kcal_target == 2000
    assert store.get_onboarding_state(704) is None

    # Construct callback query from user 704
    update_msg = _make_update("old", user_id=704).message
    query = SimpleNamespace(
        id="q_edit_704",
        data="profile:edit:704",
        from_user=SimpleNamespace(id=704, username="oleg", first_name="Oleg"),
        message=update_msg,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(update_id=1, callback_query=query, effective_message=None)

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    # Saved profile must NOT be modified by simply entering edit mode
    preserved_profile = store.get_user_profile(704)
    assert preserved_profile.daily_kcal_target == 2000
    assert preserved_profile.weight_kg == 85
    assert preserved_profile.height_cm == 180

    # Onboarding state must be active at step 'sex'
    state = store.get_onboarding_state(704)
    assert state is not None
    assert state.step == "sex"

    # Message sent with step 1 prompt and buttons
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "обновим профиль" in kwargs["text"].casefold()
    assert kwargs["reply_markup"] == [["Мужской", "Женский"]]
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}


@pytest.mark.asyncio
async def test_telegram_profile_edit_callback_enforces_ownership(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=704, username="oleg")
    _complete_onboarding(store, user_id=704, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    # Different user (999) attempts to click edit on user 704's profile
    update_msg = _make_update("old", user_id=704).message
    query = SimpleNamespace(
        id="q_edit_forged",
        data="profile:edit:704",
        from_user=SimpleNamespace(id=999, username="intruder", first_name="Intruder"),
        message=update_msg,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(update_id=1, callback_query=query, effective_message=None)

    await adapter._handle_callback_query(update, SimpleNamespace())

    # Must reject with alert
    query.answer.assert_awaited_once_with(text="Действие доступно только владельцу профиля.", show_alert=True)
    query.edit_message_reply_markup.assert_not_called()
    adapter._send_message_with_thread_fallback.assert_not_called()

    # User 704's state is untouched
    assert store.get_onboarding_state(704) is None
    assert store.get_onboarding_state(999) is None


@pytest.mark.asyncio
async def test_telegram_text_profile_edit_intent_starts_editing(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=706, username="oleg")
    _complete_onboarding(store, user_id=706, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    # User types reported exact text: "Я хочу внести изменения в профиль"
    update = _make_update("Я хочу внести изменения в профиль", user_id=706)
    await adapter._handle_text_message(update, SimpleNamespace())

    # Saved profile must remain unchanged
    profile = store.get_user_profile(706)
    assert profile.daily_kcal_target == 2000
    assert profile.weight_kg == 85

    # Onboarding re-entry must be active
    state = store.get_onboarding_state(706)
    assert state is not None
    assert state.step == "sex"

    # Step 1 prompt sent
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "обновим профиль" in kwargs["text"].casefold()
    assert kwargs["reply_markup"] == [["Мужской", "Женский"]]
    assert kwargs.get("parse_mode") in {"HTML", getattr(ParseMode, "HTML", "HTML")}


@pytest.mark.asyncio
async def test_telegram_text_profile_edit_intent_with_incomplete_registration(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=707, username="oleg")
    # Advance partially (steps: sex -> age -> height -> weight)
    store.handle_onboarding_reply(user_id=707, text="Мужской")
    store.handle_onboarding_reply(user_id=707, text="35")
    store.handle_onboarding_reply(user_id=707, text="180")
    _patch_telegram_profile_store(monkeypatch, store)

    assert store.get_onboarding_state(707).step == "weight_kg"

    # Incomplete user sends profile edit intent
    update = _make_update("Я хочу внести изменения в профиль", user_id=707)
    await adapter._handle_text_message(update, SimpleNamespace())

    # Should resume existing step without throwing validation errors for weight
    state = store.get_onboarding_state(707)
    assert state is not None
    assert state.step == "weight_kg"

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Продолжаем настройку профиля!" in kwargs["text"]
    assert "Шаг 4 из 7 • Вес" in kwargs["text"]


@pytest.mark.asyncio
async def test_telegram_profile_edit_slash_commands(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=708, username="oleg")
    _complete_onboarding(store, user_id=708, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    # Test /profile edit
    await adapter._handle_command(_make_update("/profile edit", user_id=708), SimpleNamespace())
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "обновим профиль" in kwargs["text"].casefold()
    assert kwargs["reply_markup"] == [["Мужской", "Женский"]]

    # Clear onboarding state to test /profile_edit
    store.clear_onboarding_state(708)

    await adapter._handle_command(_make_update("/profile_edit", user_id=708), SimpleNamespace())
    kwargs2 = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "обновим профиль" in kwargs2["text"].casefold()


# ---------------------------------------------------------------------------
# HER-9 Phase 3: callback-data parsing and callback-context authorization
# ---------------------------------------------------------------------------


def _make_callback_update(
    *,
    data: str,
    user_id: int,
    chat_id: int | None = None,
    chat_type: str = "private",
) -> SimpleNamespace:
    """Build a minimal Telegram callback update for the HealBite profile lane."""
    effective_chat_id = chat_id if chat_id is not None else user_id
    message = SimpleNamespace(
        text="old",
        chat_id=effective_chat_id,
        chat=SimpleNamespace(id=effective_chat_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id, username="oleg", first_name="Oleg"),
        message_thread_id=None,
        reply_to_message=None,
    )
    query = SimpleNamespace(
        id="q_profile",
        data=data,
        from_user=SimpleNamespace(id=user_id, username="oleg", first_name="Oleg"),
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(update_id=1, callback_query=query, effective_message=None)


def _seeded_edit_user(tmp_path, monkeypatch, *, user_id: int = 704):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=user_id, username="oleg")
    _complete_onboarding(store, user_id=user_id, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)
    return adapter, store


def test_profile_callback_parser_accepts_only_exact_format():
    from gateway.platforms.telegram import parse_healbite_profile_callback

    assert parse_healbite_profile_callback("profile:edit:704") == ("edit", 704)

    rejected = [
        "profile:edit",  # missing target
        "profile:edit:",  # empty target
        "profile:edit:abc",  # non-numeric target
        "profile:edit:-1",  # negative target
        "profile:edit:0",  # zero target
        "profile:edit:+704",  # explicit sign
        "profile:edit: 704",  # whitespace
        "profile:edit:704 ",  # trailing whitespace
        "profile:edit:704:705",  # unexpected extra field
        "profile:delete:704",  # unknown action
        "profile:704",  # missing action
        "profile:",  # missing action + target
        "profile",  # missing separators
        "",  # empty
        "Profile:Edit:704",  # wrong case prefix/action
        "xprofile:edit:704",  # wrong root
        "profile:edit:٧٠٤",  # non-ASCII digits
        "profile:edit:" + "9" * 5000,  # oversized payload
    ]
    for payload in rejected:
        assert parse_healbite_profile_callback(payload) is None, payload


@pytest.mark.asyncio
async def test_profile_callback_malformed_payloads_never_mutate_state(tmp_path, monkeypatch):
    adapter, store = _seeded_edit_user(tmp_path, monkeypatch)

    malformed = [
        "profile:edit",  # missing target -> must NOT fall back to caller
        "profile:edit:",  # empty target
        "profile:edit:abc",  # non-numeric target
        "profile:edit:704:extra",  # unexpected extra field
        "profile:edit:704:705",  # unexpected extra field
        "profile:delete:704",  # unknown action
        "profile:704",  # missing action
        "profile:",  # missing action + target
        "profile:edit: 704",  # whitespace-padded target
        "profile:edit:+704",  # signed target
        "profile:edit:٧٠٤",  # non-ASCII digits
        "profile:edit:" + "9" * 5000,  # oversized payload (int() digit cap path)
    ]

    for payload in malformed:
        adapter._send_message_with_thread_fallback.reset_mock()
        update = _make_callback_update(data=payload, user_id=704)
        await adapter._handle_callback_query(update, SimpleNamespace())

        assert store.get_onboarding_state(704) is None, f"mutated onboarding for {payload!r}"
        profile = store.get_user_profile(704)
        assert profile is not None and profile.daily_kcal_target == 2000, payload
        adapter._send_message_with_thread_fallback.assert_not_called()
        # Fail closed with feedback: never leave the Telegram spinner hanging.
        assert update.callback_query.answer.await_count >= 1, payload

    # A payload without the `profile:` root is not a profile callback at all: it
    # must not be hijacked into the profile lane and must not change any state.
    foreign = _make_callback_update(data="profile", user_id=704)
    await adapter._handle_callback_query(foreign, SimpleNamespace())
    assert store.get_onboarding_state(704) is None
    adapter._send_message_with_thread_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_profile_callback_requires_private_chat_context(tmp_path, monkeypatch):
    adapter, store = _seeded_edit_user(tmp_path, monkeypatch)

    # Same owner, correct payload, but the button lives in a group chat.
    update = _make_callback_update(
        data="profile:edit:704",
        user_id=704,
        chat_id=-1009876543210,
        chat_type="group",
    )
    await adapter._handle_callback_query(update, SimpleNamespace())

    assert store.get_onboarding_state(704) is None
    profile = store.get_user_profile(704)
    assert profile is not None and profile.daily_kcal_target == 2000
    # No onboarding prompt may be posted into a group.
    adapter._send_message_with_thread_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_profile_callback_rejects_cross_user_and_group_targets(tmp_path, monkeypatch):
    adapter, store = _seeded_edit_user(tmp_path, monkeypatch)

    cases = [
        # foreign user clicking user 704's button in a private chat
        dict(data="profile:edit:704", user_id=999, chat_type="private"),
        # foreign user clicking user 704's button in a group
        dict(data="profile:edit:704", user_id=999, chat_id=-1009876543210, chat_type="group"),
    ]
    for case in cases:
        adapter._send_message_with_thread_fallback.reset_mock()
        update = _make_callback_update(**case)
        await adapter._handle_callback_query(update, SimpleNamespace())

        assert store.get_onboarding_state(704) is None, case
        assert store.get_onboarding_state(999) is None, case
        adapter._send_message_with_thread_fallback.assert_not_called()
        assert update.callback_query.answer.await_count >= 1, case
    assert store.get_user_profile(704).daily_kcal_target == 2000


@pytest.mark.asyncio
async def test_profile_callback_private_owner_still_starts_editing(tmp_path, monkeypatch):
    """The legitimate public-lane path must keep working (no over-blocking)."""
    adapter, store = _seeded_edit_user(tmp_path, monkeypatch)

    update = _make_callback_update(data="profile:edit:704", user_id=704, chat_type="private")
    await adapter._handle_callback_query(update, SimpleNamespace())

    state = store.get_onboarding_state(704)
    assert state is not None and state.step == "sex"
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "обновим профиль" in kwargs["text"].casefold()
    # Profile data preserved while re-entering onboarding.
    assert store.get_user_profile(704).daily_kcal_target == 2000


@pytest.mark.asyncio
async def test_text_profile_edit_intent_requires_private_chat(tmp_path, monkeypatch):
    """The natural-language lane honours the same private-chat binding."""
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=709, username="oleg")
    _complete_onboarding(store, user_id=709, username="oleg", manual_target="2000")
    _patch_telegram_profile_store(monkeypatch, store)

    # Group thread: the intent must not be claimed and no prompt may be posted.
    group_msg = _make_update("Я хочу внести изменения в профиль", user_id=709).message
    group_msg.chat = SimpleNamespace(id=-1009876543210, type="group")

    consumed = await adapter._maybe_handle_healbite_explicit_intent(group_msg)

    assert consumed is False
    assert store.get_onboarding_state(709) is None
    assert store.get_user_profile(709).daily_kcal_target == 2000
    adapter._send_message_with_thread_fallback.assert_not_called()

    # Same text in a private chat is still consumed and starts editing.
    private_msg = _make_update("Я хочу внести изменения в профиль", user_id=709).message
    consumed_private = await adapter._maybe_handle_healbite_explicit_intent(private_msg)

    assert consumed_private is True
    assert store.get_onboarding_state(709) is not None


# ==============================================================================
# R1-01 .. R1-06 COMPREHENSIVE CONTRACT TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_r1_01_telegram_registration_and_idempotence(tmp_path, monkeypatch):
    """R1-01: Telegram registration, ID binding, idempotency, and resume."""
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    _patch_telegram_profile_store(monkeypatch, store)

    adapter._healbite_main_menu_keyboard = TelegramAdapter._healbite_main_menu_keyboard.__get__(adapter, TelegramAdapter)
    adapter._healbite_menu_rows = TelegramAdapter._healbite_menu_rows.__get__(adapter, TelegramAdapter)
    adapter._is_feature_allowlisted = lambda feat, uid: False

    # 1. New user starts via /start - no passwords, linked to Telegram ID 12345
    await adapter._handle_command(_make_update("/start", user_id=12345, username="alice"), SimpleNamespace())
    state = store.get_onboarding_state(12345)
    assert state is not None
    assert state.step == "sex"
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Давайте настроим профиль HealBite" in kwargs["text"]

    # 2. Complete onboarding
    _complete_onboarding(store, user_id=12345, username="alice", manual_target="пропустить")
    profile = store.get_user_profile(12345)
    assert profile is not None
    assert profile.user_id == 12345
    assert profile.username == "alice"
    assert profile.target_source == "calculated"
    assert profile.daily_kcal_target is not None

    # 3. Repeated /start for existing user does not reset profile and returns dashboard
    adapter._send_message_with_thread_fallback.reset_mock()
    await adapter._handle_command(_make_update("/start", user_id=12345, username="alice"), SimpleNamespace())
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "С возвращением в HealBite!" in kwargs["text"]
    assert str(int(profile.daily_kcal_target)) in kwargs["text"]
    # Profile remains untouched
    refreshed = store.get_user_profile(12345)
    assert refreshed.daily_kcal_target == profile.daily_kcal_target

    # 4. Partial onboarding resume: user stops at step 3, returns later
    store.begin_onboarding(user_id=54321, username="bob")
    store.handle_onboarding_reply(user_id=54321, text="Мужской")
    store.handle_onboarding_reply(user_id=54321, text="28")
    assert store.get_onboarding_state(54321).step == "height_cm"

    # User sends /start again
    adapter._send_message_with_thread_fallback.reset_mock()
    await adapter._handle_command(_make_update("/start", user_id=54321, username="bob"), SimpleNamespace())
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Продолжаем настройку профиля" in kwargs["text"]
    assert "Рост" in kwargs["text"]


def test_r1_02_questionnaire_input_validation(tmp_path):
    """R1-02: Initial questionnaire strictly validates bounds and rejects invalid inputs."""
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    uid = 202
    store.begin_onboarding(user_id=uid, username="tester")

    # Step 1: Sex
    rep = store.handle_onboarding_reply(user_id=uid, text="что-то странное")
    assert rep.status == "invalid"
    assert "Не понял пол" in rep.text
    rep = store.handle_onboarding_reply(user_id=uid, text="Мужской")
    assert rep.status == "next"
    assert rep.next_step == "age"

    # Step 2: Age (18-100)
    rep = store.handle_onboarding_reply(user_id=uid, text="17")
    assert rep.status == "invalid"
    assert "Не понял возраст" in rep.text
    rep = store.handle_onboarding_reply(user_id=uid, text="101")
    assert rep.status == "invalid"
    rep = store.handle_onboarding_reply(user_id=uid, text="тридцать")
    assert rep.status == "invalid"
    rep = store.handle_onboarding_reply(user_id=uid, text="30")
    assert rep.status == "next"
    assert rep.next_step == "height_cm"

    # Step 3: Height (120-230)
    rep = store.handle_onboarding_reply(user_id=uid, text="115")
    assert rep.status == "invalid"
    assert "Не понял рост" in rep.text
    rep = store.handle_onboarding_reply(user_id=uid, text="240")
    assert rep.status == "invalid"
    rep = store.handle_onboarding_reply(user_id=uid, text="178")
    assert rep.status == "next"
    assert rep.next_step == "weight_kg"

    # Step 4: Weight (35-300)
    rep = store.handle_onboarding_reply(user_id=uid, text="30")
    assert rep.status == "invalid"
    assert "Не понял вес" in rep.text
    rep = store.handle_onboarding_reply(user_id=uid, text="350")
    assert rep.status == "invalid"
    rep = store.handle_onboarding_reply(user_id=uid, text="75.5")
    assert rep.status == "next"
    assert rep.next_step == "goal"

    # Step 5: Goal
    rep = store.handle_onboarding_reply(user_id=uid, text="хочу летать")
    assert rep.status == "invalid"
    assert "Не понял цель" in rep.text
    rep = store.handle_onboarding_reply(user_id=uid, text="Снижение веса")
    assert rep.status == "next"
    assert rep.next_step == "activity_level"

    # Step 6: Activity
    rep = store.handle_onboarding_reply(user_id=uid, text="непонятно")
    assert rep.status == "invalid"
    assert "Не понял активность" in rep.text
    rep = store.handle_onboarding_reply(user_id=uid, text="Умеренная активность")
    assert rep.status == "next"
    assert rep.next_step == "manual_kcal_target"


def test_r1_03_automatic_mifflin_calculation_and_macros(tmp_path):
    """R1-03: Automatic Mifflin calculation, macros ratio and energy consistency."""
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    uid = 303
    store.begin_onboarding(user_id=uid, username="mifflin_tester")
    _complete_onboarding(store, user_id=uid, username="mifflin_tester", manual_target="пропустить")

    profile = store.get_user_profile(uid)
    assert profile is not None
    assert profile.target_source == "calculated"
    assert profile.nutrition_calculation_version == "mifflin_v1"

    # Check macro distribution for weight 85kg:
    # protein = 85 * 1.6 = 136g
    # fat = 85 * 0.8 = 68g
    assert profile.daily_protein_g == 136.0
    assert profile.daily_fat_g == 68.0
    assert profile.daily_carbs_g is not None and profile.daily_carbs_g > 0

    # Check energy consistency: |kcal - (P*4 + F*9 + C*4)| <= 15 kcal
    cal = profile.daily_kcal_target
    macro_energy = profile.daily_protein_g * 4.0 + profile.daily_fat_g * 9.0 + profile.daily_carbs_g * 4.0
    assert abs(cal - macro_energy) <= 15.0


def test_r1_04_manual_kbju_settings_and_persistence(tmp_path):
    """R1-04: Manual setting of calories AND macros, range/energy validation, and diary pickup."""
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    uid = 404
    store.begin_onboarding(user_id=uid, username="manual_tester")
    _complete_onboarding(store, user_id=uid, username="manual_tester", manual_target="пропустить")

    # 1. Parse valid manual targets: 2100 kcal, 150g P, 70g F, 220g C
    # 150*4 + 70*9 + 220*4 = 600 + 630 + 880 = 2110 kcal (delta 10 kcal <= 250 kcal)
    cal, p, f, c, err = parse_manual_nutrition_targets("2100 150 70 220")
    assert err is None
    assert (cal, p, f, c) == (2100.0, 150.0, 70.0, 220.0)

    # 2. Reject out of range calories
    _, _, _, _, err_low = parse_manual_nutrition_targets("400")
    assert err_low is not None and "вне допустимого диапазона" in err_low

    # 3. Reject huge energy mismatch (>250 kcal)
    # 2000 kcal vs 100g P, 30g F, 100g C = 400 + 270 + 400 = 1070 kcal (delta 930 kcal)
    _, _, _, _, err_mismatch = parse_manual_nutrition_targets("2000 100 30 100")
    assert err_mismatch is not None and "Несоответствие калорий" in err_mismatch

    # 4. Save manual targets
    saved_profile = store.set_manual_nutrition_targets(
        user_id=uid,
        username="manual_tester",
        calories_kcal=2100.0,
        protein_g=150.0,
        fat_g=70.0,
        carbs_g=220.0,
    )
    assert saved_profile.target_source == "manual"
    assert saved_profile.daily_kcal_target == 2100.0
    assert saved_profile.daily_protein_g == 150.0
    assert saved_profile.daily_fat_g == 70.0
    assert saved_profile.daily_carbs_g == 220.0
    assert saved_profile.manual_protein_g == 150.0
    assert saved_profile.manual_fat_g == 70.0
    assert saved_profile.manual_carbs_g == 220.0

    # 5. Persistence across new store instance
    store2 = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    reloaded = store2.get_user_profile(uid)
    assert reloaded.target_source == "manual"
    assert reloaded.daily_kcal_target == 2100.0
    assert reloaded.daily_protein_g == 150.0
    assert reloaded.daily_fat_g == 70.0
    assert reloaded.daily_carbs_g == 220.0

    # 6. Verify diary directly reads these manual targets
    from gateway.healbite_nutrition_diary import load_nutrition_targets
    diary_targets = load_nutrition_targets(tmp_path / "healbite.db", user_id=uid)
    assert diary_targets.calories_kcal == 2100.0
    assert diary_targets.protein_g == 150.0
    assert diary_targets.fat_g == 70.0
    assert diary_targets.carbs_g == 220.0


def test_r1_05_profile_editing_and_user_isolation(tmp_path):
    """R1-05: Update profile parameters, strict user isolation, and target preservation."""
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")

    # User A has calculated targets
    store.begin_onboarding(user_id=501, username="user_a")
    _complete_onboarding(store, user_id=501, username="user_a", manual_target="пропустить")
    profile_a_before = store.get_user_profile(501)
    assert profile_a_before.target_source == "calculated"
    kcal_a_before = profile_a_before.daily_kcal_target

    # User B has manual targets
    store.begin_onboarding(user_id=502, username="user_b")
    _complete_onboarding(store, user_id=502, username="user_b", manual_target="пропустить")
    store.set_manual_nutrition_targets(user_id=502, calories_kcal=2500, protein_g=180, fat_g=80, carbs_g=265)
    profile_b_before = store.get_user_profile(502)
    assert profile_b_before.target_source == "manual"

    # User A updates weight: 85 -> 95 kg
    updated_a, recalculated = store.update_profile_field(user_id=501, field="weight_kg", value=95)
    assert recalculated is True
    assert updated_a.weight_kg == 95.0
    # Because target_source was calculated, daily_kcal_target recalculates higher
    assert updated_a.daily_kcal_target > kcal_a_before

    # Verify strict user isolation: User B's profile was completely unaffected!
    profile_b_after = store.get_user_profile(502)
    assert profile_b_after.weight_kg == profile_b_before.weight_kg
    assert profile_b_after.daily_kcal_target == 2500.0
    assert profile_b_after.daily_protein_g == 180.0

    # User B updates weight: 85 -> 80 kg
    updated_b, recalculated_b = store.update_profile_field(user_id=502, field="weight_kg", value=80)
    assert recalculated_b is False  # NOT recalculated because target_source == manual!
    assert updated_b.weight_kg == 80.0
    assert updated_b.daily_kcal_target == 2500.0  # Custom goals preserved!
    assert updated_b.daily_protein_g == 180.0

    # Declarative field parsing
    assert parse_profile_field_update("рост 175") == ("height_cm", 175.0)
    assert parse_profile_field_update("возраст 32 года") == ("age", 32)
    assert parse_profile_field_update("цель похудение") == ("goal", "lose")
    assert parse_profile_field_update("активность высокая") == ("activity_level", "high")
    assert parse_profile_field_update("пол мужской") == ("sex", "male")


@pytest.mark.asyncio
async def test_r1_06_view_personal_norm_and_telegram_triggers(tmp_path, monkeypatch):
    """R1-06: View targets via /targets, /norm, text triggers, report formatting and callbacks."""
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=601, username="norm_user")
    _complete_onboarding(store, user_id=601, username="norm_user", manual_target="пропустить")
    _patch_telegram_profile_store(monkeypatch, store)

    adapter._maybe_handle_healbite_targets_command = TelegramAdapter._maybe_handle_healbite_targets_command.__get__(adapter, TelegramAdapter)
    adapter._healbite_targets_keyboard = TelegramAdapter._healbite_targets_keyboard.__get__(adapter, TelegramAdapter)
    adapter._dispatch_healbite_keyboard_action = TelegramAdapter._dispatch_healbite_keyboard_action.__get__(adapter, TelegramAdapter)

    # 1. View targets via /targets command
    await adapter._handle_command(_make_update("/targets", user_id=601), SimpleNamespace())
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Ваша суточная норма КБЖУ" in kwargs["text"]
    assert "Калории:" in kwargs["text"]
    assert "Белки:" in kwargs["text"]
    assert "Жиры:" in kwargs["text"]
    assert "Углеводы:" in kwargs["text"]
    assert "Mifflin v1" in kwargs["text"]

    # 2. View targets via /norm alias
    adapter._send_message_with_thread_fallback.reset_mock()
    await adapter._handle_command(_make_update("/norm", user_id=601), SimpleNamespace())
    assert "Ваша суточная норма КБЖУ" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    # 3. View targets via text trigger "моя норма"
    adapter._send_message_with_thread_fallback.reset_mock()
    consumed = await adapter._maybe_handle_healbite_explicit_intent(_make_update("моя норма", user_id=601).message)
    assert consumed is True
    assert "Ваша суточная норма КБЖУ" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    # 4. Set manual targets via text message: "кбжу 2200 160 70 230"
    adapter._send_message_with_thread_fallback.reset_mock()
    consumed_edit = await adapter._maybe_handle_healbite_explicit_intent(_make_update("кбжу 2200 160 70 230", user_id=601).message)
    assert consumed_edit is True
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Ваша персональная суточная норма сохранена!" in kwargs["text"]
    assert "2200 ккал" in kwargs["text"]
    assert "160" in kwargs["text"]
    profile_updated = store.get_user_profile(601)
    assert profile_updated.target_source == "manual"
    assert profile_updated.daily_kcal_target == 2200.0

    # 5. Targets callback: "targets:recalc:601" resets back to calculated
    adapter._send_message_with_thread_fallback.reset_mock()
    recalc_update = _make_callback_update(data="targets:recalc:601", user_id=601, chat_type="private")
    await adapter._handle_callback_query(recalc_update, SimpleNamespace())
    profile_recalculated = store.get_user_profile(601)
    assert profile_recalculated.target_source == "calculated"
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "пересчитана автоматически" in kwargs["text"]
