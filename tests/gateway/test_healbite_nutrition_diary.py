from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, is_gateway_known_command, resolve_command
from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    compute_nutrition_diary_summary,
    format_nutrition_diary_report,
    normalize_nutrition_payload,
)
from gateway.platforms.telegram import TelegramAdapter


class _RecordingQdrantAdapter:
    def __init__(self, *, should_succeed: bool = True) -> None:
        self.should_succeed = should_succeed
        self.calls: list[dict] = []

    def upsert_fact(self, **kwargs):
        self.calls.append(kwargs)
        return self.should_succeed


def _build_record(
    *,
    meal_name: str,
    calories_kcal: float,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    confidence: float = 0.9,
):
    return normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": meal_name,
                "raw_summary": f"{meal_name} summary",
                "confidence": confidence,
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


def _install_target_tables(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                telegram_id INTEGER PRIMARY KEY,
                calories_limit REAL
            );
            CREATE TABLE IF NOT EXISTS structured_user_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL,
                trust_score REAL DEFAULT 1,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS memory_os_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                entity TEXT,
                "key" TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT,
                trust_score REAL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def _seed_targets(
    db_path,
    *,
    user_id: int,
    calories_kcal: float | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    carbs_g: float | None = None,
    use_memory_facts: bool = False,
):
    _install_target_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        if calories_kcal is not None and not use_memory_facts:
            conn.execute(
                "INSERT OR REPLACE INTO profiles(telegram_id, calories_limit) VALUES (?, ?)",
                (user_id, calories_kcal),
            )

        target_rows = {
            "target_calories": calories_kcal,
            "target_protein_g": protein_g,
            "target_fat_g": fat_g,
            "target_carbs_g": carbs_g,
        }
        for fact_key, fact_value in target_rows.items():
            if fact_value is None:
                continue
            if use_memory_facts:
                conn.execute(
                    'INSERT INTO memory_os_facts(user_id, entity, "key", value, source, trust_score) VALUES (?, ?, ?, ?, ?, ?)',
                    (user_id, "nutrition", fact_key, str(fact_value), "test", 1.0),
                )
            else:
                conn.execute(
                    "INSERT INTO structured_user_facts(user_id, fact_key, fact_value, trust_score) VALUES (?, ?, ?, ?)",
                    (user_id, fact_key, str(fact_value), 1.0),
                )


def test_nutrition_diary_commands_are_registered_for_gateway_dispatch():
    diary_def = resolve_command("/diary")
    stats_def = resolve_command("/stats")

    assert diary_def is not None
    assert diary_def.name == "diary"
    assert diary_def.gateway_only is True

    assert stats_def is not None
    assert stats_def.name == "stats"
    assert stats_def.gateway_only is True

    assert "diary" in GATEWAY_KNOWN_COMMANDS
    assert "stats" in GATEWAY_KNOWN_COMMANDS
    assert is_gateway_known_command("diary") is True
    assert is_gateway_known_command("stats") is True


def test_unknown_slash_command_remains_unknown():
    assert resolve_command("/totally_unknown_diary_command") is None
    assert is_gateway_known_command("totally_unknown_diary_command") is False


def test_normalize_nutrition_payload_extracts_structured_json():
    payload = json.dumps(
        {
            "is_food": True,
            "meal_name": "\u0413\u0443\u043b\u044f\u0448 \u0441 \u043f\u044e\u0440\u0435",
            "raw_summary": "\u0422\u0430\u0440\u0435\u043b\u043a\u0430 \u0433\u0443\u043b\u044f\u0448\u0430 \u0441 \u043a\u0430\u0440\u0442\u043e\u0444\u0435\u043b\u044c\u043d\u044b\u043c \u043f\u044e\u0440\u0435 \u0438 \u0441\u043e\u0443\u0441\u043e\u043c.",
            "confidence": 0.91,
            "items": [
                {"name": "\u0413\u0443\u043b\u044f\u0448", "calories_kcal": 260, "protein_g": 18, "fat_g": 14, "carbs_g": 10},
                {"name": "\u041a\u0430\u0440\u0442\u043e\u0444\u0435\u043b\u044c\u043d\u043e\u0435 \u043f\u044e\u0440\u0435", "calories_kcal": 180, "protein_g": 4, "fat_g": 6, "carbs_g": 28},
            ],
        },
        ensure_ascii=False,
    )

    record = normalize_nutrition_payload(payload)

    assert record is not None
    assert record.is_food is True
    assert record.meal_name == "\u0413\u0443\u043b\u044f\u0448 \u0441 \u043f\u044e\u0440\u0435"
    assert record.calories_kcal == pytest.approx(440.0)
    assert record.protein_g == pytest.approx(22.0)
    assert record.fat_g == pytest.approx(20.0)
    assert record.carbs_g == pytest.approx(38.0)


@pytest.mark.asyncio
async def test_malformed_vision_output_does_not_create_bad_log(tmp_path, monkeypatch):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    monkeypatch.setattr(
        "tools.vision_tools.vision_analyze_tool",
        AsyncMock(return_value=json.dumps({"success": True, "analysis": "looks tasty but no json"})),
    )

    outcome = await diary.analyze_and_maybe_log(
        user_id=1,
        image_path="/tmp/meal.jpg",
        user_text="\u043f\u043e\u0441\u0447\u0438\u0442\u0430\u0439 \u043a\u0431\u0436\u0443",
        image_ref="telegram:1:10",
    )

    summary = diary.get_daily_summary(user_id=1)
    assert outcome.available is True
    assert outcome.record is not None
    assert outcome.saved is False
    assert summary["entry_count"] == 0


def test_sqlite_nutrition_log_save_and_read_works(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "\u041e\u043c\u043b\u0435\u0442",
                "raw_summary": "\u041a\u043b\u0430\u0441\u0441\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u043e\u043c\u043b\u0435\u0442.",
                "confidence": 0.88,
                "totals": {"calories_kcal": 120, "protein_g": 3, "fat_g": 7, "carbs_g": 11},
                "items": [{"name": "\u041e\u043c\u043b\u0435\u0442", "estimated_weight_g": 180}],
            },
            ensure_ascii=False,
        )
    )

    sqlite_id, duplicate = diary.save_record(
        user_id=5,
        source="vision",
        record=record,
        image_ref="telegram:5:42",
        occurred_at=datetime.now(timezone.utc),
    )

    summary = diary.get_daily_summary(user_id=5)
    report = format_nutrition_diary_report(summary)

    assert duplicate is False
    assert sqlite_id is not None
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == pytest.approx(120.0)
    assert "\u041e\u043c\u043b\u0435\u0442" in report
    assert "\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a" in report
    assert "\u041a\u0430\u043b\u043e\u0440\u0438\u0438" in report
    assert "\u0411\u0435\u043b\u043a\u0438" in report
    assert "????" not in report


def test_format_nutrition_diary_report_localized_and_html_safe():
    summary = {
        "entries": [
            {
                "meal_name": "\u0421\u0435\u043c\u0433\u0430 & \u043e\u0432\u043e\u0449\u0438 <\u0433\u0440\u0438\u043b\u044c>",
                "calories_kcal": 520,
                "protein_g": 41,
                "fat_g": 24,
                "carbs_g": 18,
            }
        ],
        "entry_count": 1,
        "calories_kcal": 520,
        "protein_g": 41,
        "fat_g": 24,
        "carbs_g": 18,
        "days": 1,
    }

    report = format_nutrition_diary_report(summary)

    assert "\U0001f4ca <b>\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u0437\u0430 \u0441\u0435\u0433\u043e\u0434\u043d\u044f:</b>" in report
    assert "\U0001f525 \u041a\u0430\u043b\u043e\u0440\u0438\u0438: 520 \u043a\u043a\u0430\u043b" in report
    assert "\u0421\u0435\u043c\u0433\u0430 &amp; \u043e\u0432\u043e\u0449\u0438 &lt;\u0433\u0440\u0438\u043b\u044c&gt;" in report
    assert "\U0001f37d <b>\u041f\u0440\u0438\u0435\u043c\u044b \u043f\u0438\u0449\u0438:</b>" in report
    assert "????" not in report



def test_diary_without_targets_shows_no_target_hint(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = _build_record(
        meal_name="\u0417\u0430\u0432\u0442\u0440\u0430\u043a",
        calories_kcal=410,
        protein_g=18,
        fat_g=14,
        carbs_g=35,
    )
    diary.save_record(
        user_id=21,
        source="vision",
        record=record,
        image_ref="telegram:21:1",
        occurred_at=datetime.now(timezone.utc),
    )

    report = format_nutrition_diary_report(diary.get_daily_summary(user_id=21))

    assert "\u0426\u0435\u043b\u044c \u0435\u0449\u0451 \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d\u0430" in report
    assert "/ 0 \u043a\u043a\u0430\u043b" not in report
    assert "????" not in report


def test_diary_with_targets_shows_progress(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    _seed_targets(
        db_path,
        user_id=31,
        calories_kcal=1700,
        protein_g=120,
        fat_g=60,
        carbs_g=180,
    )
    record = _build_record(
        meal_name="\u0421\u0435\u043c\u0433\u0430",
        calories_kcal=1450,
        protein_g=95,
        fat_g=45,
        carbs_g=120,
    )
    diary.save_record(
        user_id=31,
        source="vision",
        record=record,
        image_ref="telegram:31:1",
        occurred_at=datetime.now(timezone.utc),
    )

    summary = diary.get_daily_summary(user_id=31)
    report = format_nutrition_diary_report(summary)

    assert summary["targets_available"] is True
    assert "\u041a\u0430\u043b\u043e\u0440\u0438\u0438: 1450 \u043a\u043a\u0430\u043b / 1700 \u043a\u043a\u0430\u043b (85%)" in report
    assert "\u0411\u0435\u043b\u043a\u0438: 95 \u0433 / 120 \u0433 (79%)" in report
    assert "\u0416\u0438\u0440\u044b: 45 \u0433 / 60 \u0433 (75%)" in report
    assert "\u0423\u0433\u043b\u0435\u0432\u043e\u0434\u044b: 120 \u0433 / 180 \u0433 (67%)" in report
    assert "\u0426\u0435\u043b\u044c \u0435\u0449\u0451 \u043d\u0435 \u043dа\u0441\u0442\u0440\u043e\u0435\u043d\u0430" not in report
    assert "????" not in report


def test_diary_low_calories_and_protein_show_soft_hints(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    _seed_targets(
        db_path,
        user_id=32,
        calories_kcal=1700,
        protein_g=120,
        fat_g=60,
        carbs_g=180,
    )
    record = _build_record(
        meal_name="\u041f\u0435\u0440\u0435\u043a\u0443\u0441",
        calories_kcal=600,
        protein_g=40,
        fat_g=18,
        carbs_g=55,
    )
    diary.save_record(
        user_id=32,
        source="vision",
        record=record,
        image_ref="telegram:32:1",
        occurred_at=datetime.now(timezone.utc),
    )

    report = format_nutrition_diary_report(diary.get_daily_summary(user_id=32))

    assert "\u0421\u0435\u0433\u043e\u0434\u043d\u044f \u043f\u043e\u043a\u0430 \u043c\u0430\u043b\u043e \u043a\u0430\u043b\u043e\u0440\u0438\u0439" in report
    assert "\u0411\u0435\u043b\u043a\u0430 \u043f\u043e\u043a\u0430 \u043c\u0430\u043b\u043e\u0432\u0430\u0442\u043e" in report


def test_weekly_stats_use_daily_averages_for_targets(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    _seed_targets(
        db_path,
        user_id=33,
        calories_kcal=1700,
        protein_g=120,
        fat_g=60,
        carbs_g=180,
    )
    record = _build_record(
        meal_name="\u0420\u0438\u0441 \u0441 \u043a\u0443\u0440\u0438\u0446\u0435\u0439",
        calories_kcal=1450,
        protein_g=95,
        fat_g=45,
        carbs_g=120,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for day_offset in range(7):
        diary.save_record(
            user_id=33,
            source="vision",
            record=record,
            image_ref=f"telegram:33:{day_offset}",
            occurred_at=now - timedelta(days=day_offset, seconds=1),
        )

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=33, now=now, days=7)
    report = format_nutrition_diary_report(summary)

    assert summary["days"] == 7
    assert "\u0412 \u0441\u0440\u0435\u0434\u043d\u0435\u043c \u0437\u0430 \u0434\u0435\u043d\u044c" in report
    assert "\u041a\u0430\u043b\u043e\u0440\u0438\u0438: 1450 \u043a\u043a\u0430\u043b / 1700 \u043a\u043a\u0430\u043b (85%)" in report
    assert "\u0418\u0442\u043e\u0433\u043e \u0437\u0430 7 \u0434\u043d\u0435\u0439" in report
    assert "10150 \u043a\u043a\u0430\u043b" in report
    assert "????" not in report


def test_diary_targets_can_fall_back_to_memory_os_facts(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    _seed_targets(
        db_path,
        user_id=34,
        calories_kcal=1700,
        protein_g=120,
        fat_g=60,
        carbs_g=180,
        use_memory_facts=True,
    )
    record = _build_record(
        meal_name="\u0422\u0432\u043e\u0440\u043e\u0433",
        calories_kcal=800,
        protein_g=60,
        fat_g=22,
        carbs_g=55,
    )
    diary.save_record(
        user_id=34,
        source="vision",
        record=record,
        image_ref="telegram:34:1",
        occurred_at=datetime.now(timezone.utc),
    )

    summary = diary.get_daily_summary(user_id=34)
    report = format_nutrition_diary_report(summary)

    assert summary["targets_available"] is True
    assert summary["progress"]["protein_g"]["target"] == pytest.approx(120.0)
    assert "\u0411\u0435\u043b\u043a\u0438: 60 \u0433 / 120 \u0433 (50%)" in report


def test_diary_targets_are_isolated_by_user_id(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    _seed_targets(
        db_path,
        user_id=41,
        calories_kcal=1700,
        protein_g=120,
        fat_g=60,
        carbs_g=180,
    )
    record = _build_record(
        meal_name="\u041e\u0431\u0435\u0434",
        calories_kcal=900,
        protein_g=45,
        fat_g=35,
        carbs_g=80,
    )
    for user_id in (41, 42):
        diary.save_record(
            user_id=user_id,
            source="vision",
            record=record,
            image_ref=f"telegram:{user_id}:1",
            occurred_at=datetime.now(timezone.utc),
        )

    report_with_targets = format_nutrition_diary_report(diary.get_daily_summary(user_id=41))
    report_without_targets = format_nutrition_diary_report(diary.get_daily_summary(user_id=42))

    assert "/ 1700 \u043a\u043a\u0430\u043b" in report_with_targets
    assert "/ 1700 \u043a\u043a\u0430\u043b" not in report_without_targets
    assert "\u0426\u0435\u043b\u044c \u0435\u0449\u0451 \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d\u0430" in report_without_targets


def test_diary_summary_is_isolated_by_user_id(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "\u0421\u0430\u043b\u0430\u0442",
                "raw_summary": "\u041f\u043e\u0440\u0446\u0438\u044f \u0441\u0430\u043b\u0430\u0442\u0430.",
                "confidence": 0.8,
                "totals": {"calories_kcal": 300, "protein_g": 10, "fat_g": 8, "carbs_g": 45},
            },
            ensure_ascii=False,
        )
    )
    diary.save_record(user_id=1, source="vision", record=record, image_ref="telegram:1:11", occurred_at=datetime.now(timezone.utc))
    diary.save_record(user_id=2, source="vision", record=record, image_ref="telegram:2:11", occurred_at=datetime.now(timezone.utc))

    user_one = diary.get_daily_summary(user_id=1)
    user_two = diary.get_daily_summary(user_id=2)

    assert user_one["entry_count"] == 1
    assert user_two["entry_count"] == 1
    assert user_one["entries"][0]["user_id"] == 1
    assert user_two["entries"][0]["user_id"] == 2


def test_qdrant_failure_does_not_break_sqlite_save(tmp_path):
    adapter = _RecordingQdrantAdapter(should_succeed=False)
    diary = HealBiteNutritionDiary(
        db_path=tmp_path / "healbite.db",
        qdrant_adapter=adapter,
        background_write=False,
    )
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "\u0411\u043e\u0443\u043b",
                "raw_summary": "\u0411\u043e\u0443\u043b \u0441 \u043a\u0443\u0440\u0438\u0446\u0435\u0439.",
                "confidence": 0.9,
                "totals": {"calories_kcal": 510, "protein_g": 35, "fat_g": 14, "carbs_g": 58},
            },
            ensure_ascii=False,
        )
    )

    sqlite_id, duplicate = diary.save_record(
        user_id=7,
        source="vision",
        record=record,
        image_ref="telegram:7:50",
        occurred_at=datetime.now(timezone.utc),
    )

    summary = diary.get_daily_summary(user_id=7)
    assert sqlite_id is not None
    assert duplicate is False
    assert summary["entry_count"] == 1
    assert adapter.calls[0]["user_id"] == 7


def test_qdrant_payload_contains_sqlite_id_and_user_id(tmp_path):
    adapter = _RecordingQdrantAdapter(should_succeed=True)
    diary = HealBiteNutritionDiary(
        db_path=tmp_path / "healbite.db",
        qdrant_adapter=adapter,
        background_write=False,
    )
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "\u0422\u0432\u043e\u0440\u043e\u0436\u043e\u043a",
                "raw_summary": "\u0422\u0432\u043e\u0440\u043e\u0436\u043e\u043a \u0441 \u044f\u0433\u043e\u0434\u0430\u043c\u0438.",
                "confidence": 0.84,
                "totals": {"calories_kcal": 190, "protein_g": 9, "fat_g": 4, "carbs_g": 28},
            },
            ensure_ascii=False,
        )
    )

    sqlite_id, _ = diary.save_record(
        user_id=12,
        source="vision",
        record=record,
        image_ref="telegram:12:77",
        occurred_at=datetime.now(timezone.utc),
    )

    call = adapter.calls[0]
    assert call["sqlite_id"] == sqlite_id
    assert call["user_id"] == 12
    assert call["payload"]["record_type"] == "nutrition_log"


def test_duplicate_photo_retry_does_not_double_save(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "\u041f\u0430\u0441\u0442\u0430",
                "raw_summary": "\u041f\u0430\u0441\u0442\u0430 \u0441 \u0442\u043e\u043c\u0430\u0442\u043d\u044b\u043c \u0441\u043e\u0443\u0441\u043e\u043c.",
                "confidence": 0.86,
                "totals": {"calories_kcal": 420, "protein_g": 31, "fat_g": 18, "carbs_g": 20},
            },
            ensure_ascii=False,
        )
    )

    first_id, first_duplicate = diary.save_record(
        user_id=3,
        source="vision",
        record=record,
        image_ref="telegram:3:91",
        occurred_at=datetime.now(timezone.utc),
    )
    second_id, second_duplicate = diary.save_record(
        user_id=3,
        source="vision",
        record=record,
        image_ref="telegram:3:91",
        occurred_at=datetime.now(timezone.utc) + timedelta(seconds=2),
    )

    summary = diary.get_daily_summary(user_id=3)
    assert first_duplicate is False
    assert second_duplicate is True
    assert first_id == second_id
    assert summary["entry_count"] == 1


@pytest.mark.asyncio
async def test_telegram_diary_command_routes_correctly(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    monkeypatch.setattr(
        "gateway.platforms.telegram.compute_nutrition_diary_summary",
        lambda *args, **kwargs: {
            "entries": [{"meal_name": "\u041f\u0430\u0441\u0442\u0430", "calories_kcal": 420, "protein_g": 25, "fat_g": 16, "carbs_g": 38}],
            "entry_count": 1,
            "calories_kcal": 420,
            "protein_g": 25,
            "fat_g": 16,
            "carbs_g": 38,
            "days": kwargs.get("days", 1),
        },
    )
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_nutrition_diary_report",
        lambda summary: f"\U0001f4ca <b>\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u0437\u0430 \u0441\u0435\u0433\u043e\u0434\u043d\u044f:</b>\n\U0001f525 \u041a\u0430\u043b\u043e\u0440\u0438\u0438: {summary['calories_kcal']:.0f} \u043a\u043a\u0430\u043b",
    )

    msg = SimpleNamespace(
        text="/diary",
        chat=SimpleNamespace(id=111, type="private"),
        from_user=SimpleNamespace(id=111),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a" in kwargs["text"]
    assert "\u041a\u0430\u043b\u043e\u0440\u0438\u0438" in kwargs["text"]
    assert "????" not in kwargs["text"]
    assert kwargs["parse_mode"] is not None
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_stats_7d_command_routes_correctly(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    captured_days: list[int] = []

    def _fake_summary(*args, **kwargs):
        captured_days.append(kwargs.get("days", 1))
        return {
            "entries": [{"meal_name": "\u0421\u0443\u043f", "calories_kcal": 300, "protein_g": 12, "fat_g": 8, "carbs_g": 40}],
            "entry_count": 1,
            "calories_kcal": 2100,
            "protein_g": 84,
            "fat_g": 56,
            "carbs_g": 280,
            "days": kwargs.get("days", 1),
        }

    monkeypatch.setattr("gateway.platforms.telegram.compute_nutrition_diary_summary", _fake_summary)
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_nutrition_diary_report",
        lambda summary: f"\U0001f4ca <b>\u0422\u0432\u043e\u044f \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0437\u0430 {summary['days']} \u0434\u043d\u0435\u0439:</b>\n\U0001f4c8 <b>\u0412 \u0441\u0440\u0435\u0434\u043d\u0435\u043c \u0437\u0430 \u0434\u0435\u043d\u044c:</b>",
    )

    msg = SimpleNamespace(
        text="/stats 7d",
        chat=SimpleNamespace(id=222, type="private"),
        from_user=SimpleNamespace(id=222),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=2, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "\u0412 \u0441\u0440\u0435\u0434\u043d\u0435\u043c \u0437\u0430 \u0434\u0435\u043d\u044c" in kwargs["text"]
    assert "????" not in kwargs["text"]
    assert captured_days == [7]
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_diary_7d_command_routes_correctly(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    captured_days: list[int] = []

    def _fake_summary(*args, **kwargs):
        captured_days.append(kwargs.get("days", 1))
        return {
            "entries": [{"meal_name": "\u041a\u0430\u0448\u0430", "calories_kcal": 250, "protein_g": 9, "fat_g": 4, "carbs_g": 44}],
            "entry_count": 2,
            "calories_kcal": 1750,
            "protein_g": 77,
            "fat_g": 49,
            "carbs_g": 210,
            "days": kwargs.get("days", 1),
        }

    monkeypatch.setattr("gateway.platforms.telegram.compute_nutrition_diary_summary", _fake_summary)
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_nutrition_diary_report",
        lambda summary: f"\U0001f4ca <b>\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u0437\u0430 {summary['days']} \u0434\u043d\u0435\u0439:</b>\n\U0001f4c8 <b>\u0412 \u0441\u0440\u0435\u0434\u043d\u0435\u043c \u0437\u0430 \u0434\u0435\u043d\u044c:</b>",
    )

    msg = SimpleNamespace(
        text="/diary 7d",
        chat=SimpleNamespace(id=333, type="private"),
        from_user=SimpleNamespace(id=333),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=3, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "\u0412 \u0441\u0440\u0435\u0434\u043d\u0435\u043c \u0437\u0430 \u0434\u0435\u043d\u044c" in kwargs["text"]
    assert "????" not in kwargs["text"]
    assert captured_days == [7]
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_menu_command_stays_local(monkeypatch):
    del monkeypatch
    adapter = object.__new__(TelegramAdapter)
    adapter._send_healbite_menu_message = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    msg = SimpleNamespace(
        text="/menu",
        chat=SimpleNamespace(id=444, type="private"),
        from_user=SimpleNamespace(id=444),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=4, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    adapter._send_healbite_menu_message.assert_awaited_once_with(
        msg,
        command="/menu",
    )
    adapter.handle_message.assert_not_awaited()


def test_compute_nutrition_diary_summary_uses_today_window(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "\u041a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430",
                "raw_summary": "\u0417\u0430\u043f\u0435\u0447\u0435\u043d\u043d\u0430\u044f \u043a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430.",
                "confidence": 0.9,
                "totals": {"calories_kcal": 350, "protein_g": 20, "fat_g": 10, "carbs_g": 42},
            },
            ensure_ascii=False,
        )
    )
    now = datetime.now(timezone.utc)
    diary.save_record(user_id=99, source="vision", record=record, image_ref="telegram:99:1", occurred_at=now)
    diary.save_record(user_id=99, source="vision", record=record, image_ref="telegram:99:2", occurred_at=now - timedelta(days=2))

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=99, now=now)

    assert summary["entry_count"] == 1
    assert summary["days"] == 1


def test_compute_nutrition_diary_summary_supports_7d_window(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "\u0411\u043e\u0443\u043b \u0441 \u0440\u0438\u0441\u043e\u043c",
                "raw_summary": "\u0420\u0438\u0441, \u043e\u0432\u043e\u0449\u0438 \u0438 \u043a\u0443\u0440\u0438\u0446\u0430.",
                "confidence": 0.93,
                "totals": {"calories_kcal": 600, "protein_g": 35, "fat_g": 18, "carbs_g": 70},
            },
            ensure_ascii=False,
        )
    )
    now = datetime.now(timezone.utc)
    diary.save_record(user_id=77, source="vision", record=record, image_ref="telegram:77:1", occurred_at=now - timedelta(days=1))
    diary.save_record(user_id=77, source="vision", record=record, image_ref="telegram:77:2", occurred_at=now - timedelta(days=6))
    diary.save_record(user_id=77, source="vision", record=record, image_ref="telegram:77:3", occurred_at=now - timedelta(days=8))

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=77, now=now, days=7)
    report = format_nutrition_diary_report(summary)

    assert summary["entry_count"] == 2
    assert summary["days"] == 7
    assert summary["calories_kcal"] == pytest.approx(1200.0)
    assert "\u0412 \u0441\u0440\u0435\u0434\u043d\u0435\u043c \u0437\u0430 \u0434\u0435\u043d\u044c" in report
    assert "????" not in report
