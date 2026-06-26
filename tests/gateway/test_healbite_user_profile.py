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
from gateway.platforms.telegram import HEALBITE_REPLY_KEYBOARD_ROWS, TelegramAdapter


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
    adapter.config = SimpleNamespace(extra={})
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
    assert "главное меню" in kwargs["text"].casefold()
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
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_onboarding_reply_short_circuits_and_advances(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=705, username="oleg")
    _patch_telegram_profile_store(monkeypatch, store)

    await adapter._handle_text_message(_make_update("Мужской", user_id=705), SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "полных лет" in kwargs["text"]
    assert kwargs["reply_markup"] is None
    assert store.get_onboarding_state(705).step == "age"
    adapter._enqueue_text_event.assert_not_called()


def test_route_marker_log_is_pii_safe(caplog):
    adapter = object.__new__(TelegramAdapter)
    update = _make_update("/profile", user_id=968323641, username="secret-user")

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_route_selected(route="profile", msg=update.message, update_id=11)

    assert "healbite_route_selected" in caplog.text
    assert "route=profile" in caplog.text
    assert "968323641" not in caplog.text
    assert "secret-user" not in caplog.text
    assert "/profile" not in caplog.text
