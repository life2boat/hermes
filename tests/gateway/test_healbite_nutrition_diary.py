from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
