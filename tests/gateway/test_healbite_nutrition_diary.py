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
            "meal_name": "?????????? ?? ????????",
            "raw_summary": "???????????? ???? ?????????? ?? ???????????????????????? ????????.",
            "confidence": 0.91,
            "items": [
                {"name": "??????????", "calories_kcal": 260, "protein_g": 18, "fat_g": 14, "carbs_g": 10},
                {"name": "???????????????????????? ????????", "calories_kcal": 180, "protein_g": 4, "fat_g": 6, "carbs_g": 28},
            ],
        },
        ensure_ascii=False,
    )

    record = normalize_nutrition_payload(payload)

    assert record is not None
    assert record.is_food is True
    assert record.meal_name == "?????????? ?? ????????"
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
        user_text="?????? ???????",
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
                "meal_name": "??????????",
                "raw_summary": "?????????????? ??????????.",
                "confidence": 0.88,
                "totals": {"calories_kcal": 120, "protein_g": 3, "fat_g": 7, "carbs_g": 11},
                "items": [{"name": "??????????", "estimated_weight_g": 180}],
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
    assert "??????????" in report


def test_diary_summary_is_isolated_by_user_id(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "??????????????",
                "raw_summary": "????????.",
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
                "meal_name": "????????",
                "raw_summary": "???????????? ?? ??????????.",
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
                "meal_name": "??????????????",
                "raw_summary": "???????????? ?? ??????????????.",
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
                "meal_name": "????????",
                "raw_summary": "???????? ?? ??????????????.",
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
            "entries": [{"meal_name": "????????", "calories_kcal": 420, "protein_g": 25, "fat_g": 16, "carbs_g": 38}],
            "entry_count": 1,
            "calories_kcal": 420,
            "protein_g": 25,
            "fat_g": 16,
            "carbs_g": 38,
        },
    )
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_nutrition_diary_report",
        lambda summary: f"?????????????? ?????????????? ???? ??????????????\n??????????????: {summary['calories_kcal']:.0f} ????????",
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
    assert "??????????????: 420 ????????" in kwargs["text"]
    adapter.handle_message.assert_not_awaited()


def test_compute_nutrition_diary_summary_uses_today_window(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "?????????????? ????????",
                "raw_summary": "???????????????? ????????????.",
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
