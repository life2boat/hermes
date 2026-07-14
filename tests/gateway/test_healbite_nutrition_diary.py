from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session_context import clear_session_vars, set_session_vars
from gateway.run import GatewayRunner, _classify_telegram_diary_turn, _exec_approval_policy_for_turn, _filter_user_facing_toolsets_for_turn
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, is_gateway_known_command, resolve_command
from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    HealBiteNutritionDiary,
    PENDING_MEALS_TABLE,
    UndoMealResult,
    compute_nutrition_diary_summary,
    format_food_vision_inventory_reply,
    format_nutrition_diary_report,
    format_pending_meal_prompt,
    format_undo_meal_report,
    localized_meal_display_name,
    NutritionRecord,
    normalize_nutrition_payload,
    validate_food_vision_inventory,
)
from gateway.platforms.telegram import TelegramAdapter
from tools.healbite_nutrition_diary_tool import (
    _build_update_last_meal_user_reply,
    update_last_meal_tool,
)


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


def _count_pending_rows(db_path: Path, *, user_id: int) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {PENDING_MEALS_TABLE} WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row[0] if row else 0)


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
    undo_def = resolve_command("/undo_meal")
    undo_alias_def = resolve_command("/diary_undo")

    assert diary_def is not None
    assert diary_def.name == "diary"
    assert diary_def.gateway_only is True

    assert stats_def is not None
    assert stats_def.name == "stats"
    assert stats_def.gateway_only is True

    assert undo_def is not None
    assert undo_def.name == "undo_meal"
    assert undo_def.gateway_only is True

    assert undo_alias_def is not None
    assert undo_alias_def.name == "undo_meal"
    assert undo_alias_def.gateway_only is True

    assert "diary" in GATEWAY_KNOWN_COMMANDS
    assert "stats" in GATEWAY_KNOWN_COMMANDS
    assert "undo_meal" in GATEWAY_KNOWN_COMMANDS
    assert "diary_undo" in GATEWAY_KNOWN_COMMANDS
    assert is_gateway_known_command("diary") is True
    assert is_gateway_known_command("stats") is True
    assert is_gateway_known_command("undo_meal") is True
    assert is_gateway_known_command("diary_undo") is True


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
async def test_malformed_vision_output_returns_unavailable_without_creating_log(tmp_path, monkeypatch, caplog):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    monkeypatch.setattr(
        "tools.vision_tools.vision_analyze_tool",
        AsyncMock(return_value=json.dumps({"success": True, "analysis": "looks tasty but no json"})),
    )

    with caplog.at_level("INFO", logger="gateway.healbite_nutrition_diary"):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1,
            image_path="/tmp/meal.jpg",
            user_text="\u043f\u043e\u0441\u0447\u0438\u0442\u0430\u0439 \u043a\u0431\u0436\u0443",
            image_ref="telegram:1:10",
        )

    summary = diary.get_daily_summary(user_id=1)
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert outcome.available is False
    assert outcome.record is None
    assert outcome.saved is False
    assert diary.get_pending_meal(1) is None
    assert summary["entry_count"] == 0
    assert "vision_parse_ok" in joined
    assert "invalid_json" in joined
    assert "vision_pending_staged" in joined


@pytest.mark.asyncio
async def test_vision_provider_failure_does_not_stage_pending_or_write_log(tmp_path, monkeypatch, caplog):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    monkeypatch.setattr(
        "tools.vision_tools.vision_analyze_tool",
        AsyncMock(
            return_value=json.dumps(
                {
                    "success": False,
                    "error": "Error analyzing image: provider_unavailable",
                    "analysis": "Vision service temporarily unavailable. Please try again later or describe the meal in text.",
                },
                ensure_ascii=False,
            )
        ),
    )

    with caplog.at_level("INFO", logger="gateway.healbite_nutrition_diary"):
        outcome = await diary.analyze_and_maybe_log(
            user_id=7,
            image_path="/tmp/meal.jpg",
            user_text="count calories",
            image_ref="telegram:7:404",
        )

    summary = diary.get_daily_summary(user_id=7)
    joined = "\n".join(record.getMessage() for record in caplog.records)

    assert outcome.available is False
    assert outcome.pending is False
    assert outcome.record is None
    assert diary.get_pending_meal(7) is None
    assert _count_pending_rows(tmp_path / "healbite.db", user_id=7) == 0
    assert summary["entry_count"] == 0
    assert "vision_parse_ok" in joined
    assert "provider_result_non_success" in joined
    assert "vision_pending_staged" in joined


@pytest.mark.asyncio
async def test_analyze_and_maybe_log_returns_stage1_clarification_without_pending(tmp_path, monkeypatch, caplog):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    monkeypatch.setattr(
        "tools.vision_tools.vision_analyze_tool",
        AsyncMock(
            return_value=json.dumps(
                {
                    "success": True,
                    "analysis": json.dumps(
                        {
                            "schema_version": FOOD_VISION_SCHEMA_VERSION,
                            "items": [
                                {
                                    "visible_name": "Борщ",
                                    "normalized_name": "борщ",
                                    "confidence": 0.92,
                                    "estimated_grams_min": 250,
                                    "estimated_grams_max": 260,
                                    "preparation": "горячий суп",
                                    "is_sauce": False,
                                    "uncertainty": "",
                                }
                            ],
                            "overall_confidence": 0.92,
                            "needs_user_confirmation": False,
                            "warnings": [],
                        },
                        ensure_ascii=False,
                    ),
                },
                ensure_ascii=False,
            )
        ),
    )

    with caplog.at_level("INFO", logger="gateway.healbite_nutrition_diary"):
        outcome = await diary.analyze_and_maybe_log(
            user_id=11,
            image_path="/tmp/borscht.jpg",
            user_text="посчитай кбжу",
            image_ref="telegram:11:77",
        )

    summary = diary.get_daily_summary(user_id=11)
    joined = "\n".join(record.getMessage() for record in caplog.records)

    assert outcome.available is True
    assert outcome.pending is False
    assert outcome.saved is False
    assert outcome.record is None
    assert outcome.validation_status == "VALID"
    assert "Я вижу:" in outcome.clarification_text
    assert "Борщ" in outcome.clarification_text
    assert "КБЖУ не рассчитаны" in outcome.clarification_text
    pending_inventory = diary.get_pending_inventory(11)
    assert pending_inventory is not None
    assert pending_inventory.inventory_id
    assert diary.get_pending_meal(11) is None
    assert _count_pending_rows(tmp_path / "healbite.db", user_id=11) == 1
    assert summary["entry_count"] == 0
    assert "vision_parse_ok" in joined
    assert "validation_status=VALID" in joined
    assert "vision_pending_staged" in joined


def test_validate_food_vision_inventory_rejects_aggregate_nutrition_injection():
    payload = json.dumps(
        {
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "items": [
                {
                    "visible_name": "Паста",
                    "normalized_name": "паста",
                    "confidence": 0.9,
                    "estimated_grams_min": 60,
                    "estimated_grams_max": 90,
                    "preparation": "",
                    "is_sauce": False,
                    "uncertainty": "",
                }
            ],
            "overall_confidence": 0.9,
            "needs_user_confirmation": False,
            "warnings": [],
            "calories_kcal": 500,
        },
        ensure_ascii=False,
    )

    result = validate_food_vision_inventory(payload)

    assert result.status == "INVALID_PROVIDER_OUTPUT"
    assert result.inventory is None
    assert result.reason == "aggregate_nutrition_present"


def test_format_food_vision_inventory_reply_never_exposes_raw_json():
    result = validate_food_vision_inventory(
        json.dumps(
            {
                "schema_version": FOOD_VISION_SCHEMA_VERSION,
                "items": [
                    {
                        "visible_name": "Огурцы",
                        "normalized_name": "огурцы",
                        "confidence": 0.81,
                        "estimated_grams_min": 60,
                        "estimated_grams_max": 100,
                        "preparation": "соломкой",
                        "is_sauce": False,
                        "uncertainty": "точность порции ограничена",
                    }
                ],
                "overall_confidence": 0.81,
                "needs_user_confirmation": True,
                "warnings": [],
            },
            ensure_ascii=False,
        )
    )

    assert result.inventory is not None
    reply = format_food_vision_inventory_reply(result.inventory)
    assert "Я вижу:" in reply
    assert "Огурцы" in reply
    assert "{" not in reply
    assert "calories" not in reply
    assert "КБЖУ не рассчитаны" in reply


def test_confirm_pending_meal_saves_to_db_and_clears_state(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    record = _build_record(
        meal_name="Омлет",
        calories_kcal=320,
        protein_g=18,
        fat_g=22,
        carbs_g=4,
    )
    diary.stage_pending_meal(
        user_id=15,
        source="vision",
        record=record,
        image_ref="telegram:15:12",
        occurred_at=datetime.now(timezone.utc),
    )

    outcome = HealBiteNutritionDiary(db_path=db_path, background_write=False).confirm_pending_meal(15)
    summary = diary.get_daily_summary(user_id=15)

    assert outcome.status == "saved"
    assert outcome.duplicate is False
    assert diary.get_pending_meal(15) is None
    assert _count_pending_rows(db_path, user_id=15) == 0
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == pytest.approx(320.0)


def test_clear_pending_meal_discards_state_without_writing_to_db(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    record = _build_record(
        meal_name="Салат",
        calories_kcal=150,
        protein_g=4,
        fat_g=8,
        carbs_g=12,
    )
    diary.stage_pending_meal(
        user_id=18,
        source="vision",
        record=record,
        image_ref="telegram:18:44",
        occurred_at=datetime.now(timezone.utc),
    )

    assert diary.clear_pending_meal(18) is True
    assert diary.get_pending_meal(18) is None
    assert _count_pending_rows(db_path, user_id=18) == 0
    assert diary.get_daily_summary(user_id=18)["entry_count"] == 0


def test_confirm_pending_meal_expired_rejects_and_clears_state(tmp_path):
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    base_now = datetime(2026, 6, 18, 5, 0, tzinfo=timezone.utc)
    diary.stage_pending_meal(
        user_id=19,
        source="vision",
        record=_build_record(
            meal_name="Суп",
            calories_kcal=180,
            protein_g=6,
            fat_g=7,
            carbs_g=24,
        ),
        image_ref="telegram:19:50",
        occurred_at=base_now,
        now=base_now,
    )

    result = diary.confirm_pending_meal(
        19,
        now=base_now + timedelta(hours=2, minutes=1),
    )

    assert result.status == "expired"
    assert diary.get_pending_meal(19) is None
    assert _count_pending_rows(db_path, user_id=19) == 0
    assert diary.get_daily_summary(user_id=19)["entry_count"] == 0


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


def test_localized_meal_display_name_uses_legacy_alias_map():
    assert localized_meal_display_name("Borscht with Sour Cream and Dill") == "\u0411\u043e\u0440\u0449 \u0441\u043e \u0441\u043c\u0435\u0442\u0430\u043d\u043e\u0439 \u0438 \u0443\u043a\u0440\u043e\u043f\u043e\u043c"
    assert localized_meal_display_name("  Buckwheat with Tomatoes and Herbs  ") == "\u0413\u0440\u0435\u0447\u043a\u0430 \u0441 \u043f\u043e\u043c\u0438\u0434\u043e\u0440\u0430\u043c\u0438 \u0438 \u0437\u0435\u043b\u0435\u043d\u044c\u044e"
    assert localized_meal_display_name("Buckwheat Kasha with Tomatoes and Herbs") == "\u0413\u0440\u0435\u0447\u043d\u0435\u0432\u0430\u044f \u043a\u0430\u0448\u0430 \u0441 \u043f\u043e\u043c\u0438\u0434\u043e\u0440\u0430\u043c\u0438 \u0438 \u0437\u0435\u043b\u0435\u043d\u044c\u044e"
    assert localized_meal_display_name("Traditional Yeast (100g)") == "\u0422\u0440\u0430\u0434\u0438\u0446\u0438\u043e\u043d\u043d\u044b\u0435 \u0434\u0440\u043e\u0436\u0436\u0438 (100 \u0433)"
    assert localized_meal_display_name("Asian Beef Salad") == "\u0410\u0437\u0438\u0430\u0442\u0441\u043a\u0438\u0439 \u0441\u0430\u043b\u0430\u0442 \u0441 \u0433\u043e\u0432\u044f\u0434\u0438\u043d\u043e\u0439"


def test_localized_meal_display_name_prefers_explicit_user_facing_fields():
    assert localized_meal_display_name(
        {
            "display_name": "\u0411\u043e\u0440\u0449 \u0434\u043e\u043c\u0430\u0448\u043d\u0438\u0439",
            "meal_name_user": "\u0411\u043e\u0440\u0449 \u043f\u043e \u0440\u0435\u0446\u0435\u043f\u0442\u0443 \u043c\u0430\u043c\u044b",
            "meal_name_ru": "\u0411\u043e\u0440\u0449",
            "meal_name": "Borscht with Sour Cream and Dill",
        }
    ) == "\u0411\u043e\u0440\u0449 \u0434\u043e\u043c\u0430\u0448\u043d\u0438\u0439"
    assert localized_meal_display_name(
        {
            "meal_name_user": "\u0413\u0440\u0435\u0447\u043a\u0430 \u0441 \u0437\u0435\u043b\u0435\u043d\u044c\u044e",
            "meal_name_ru": "\u0413\u0440\u0435\u0447\u043a\u0430",
            "meal_name": "Buckwheat with Tomatoes and Herbs",
        }
    ) == "\u0413\u0440\u0435\u0447\u043a\u0430 \u0441 \u0437\u0435\u043b\u0435\u043d\u044c\u044e"
    assert localized_meal_display_name({"meal_name_ru": "\u0411\u043e\u0440\u0449", "meal_name": "Borscht with Sour Cream and Dill"}) == "\u0411\u043e\u0440\u0449"


def test_localized_meal_display_name_keeps_unknown_name_and_fallback():
    assert localized_meal_display_name("Unknown Bowl Deluxe") == "Unknown Bowl Deluxe"
    assert localized_meal_display_name("   ") == "\u0411\u043b\u044e\u0434\u043e"


def test_diary_report_localizes_legacy_names_without_touching_unknown_english():
    summary = {
        "entries": [
            {"meal_name": "Borscht with Sour Cream and Dill", "calories_kcal": 251},
            {"meal_name": "Buckwheat with Tomatoes and Herbs", "calories_kcal": 236},
            {"meal_name": "Traditional Yeast (100g)", "calories_kcal": 110},
            {"meal_name": "Asian Beef Salad", "calories_kcal": 320},
            {"meal_name": "Unknown Bowl Deluxe", "calories_kcal": 199},
        ],
        "entry_count": 5,
        "calories_kcal": 1116,
        "protein_g": 0,
        "fat_g": 0,
        "carbs_g": 0,
        "days": 1,
    }

    report = format_nutrition_diary_report(summary)

    assert "\u0411\u043e\u0440\u0449 \u0441\u043e \u0441\u043c\u0435\u0442\u0430\u043d\u043e\u0439 \u0438 \u0443\u043a\u0440\u043e\u043f\u043e\u043c" in report
    assert "\u0413\u0440\u0435\u0447\u043a\u0430 \u0441 \u043f\u043e\u043c\u0438\u0434\u043e\u0440\u0430\u043c\u0438 \u0438 \u0437\u0435\u043b\u0435\u043d\u044c\u044e" in report
    assert "\u0422\u0440\u0430\u0434\u0438\u0446\u0438\u043e\u043d\u043d\u044b\u0435 \u0434\u0440\u043e\u0436\u0436\u0438 (100 \u0433)" in report
    assert "\u0410\u0437\u0438\u0430\u0442\u0441\u043a\u0438\u0439 \u0441\u0430\u043b\u0430\u0442 \u0441 \u0433\u043e\u0432\u044f\u0434\u0438\u043d\u043e\u0439" in report
    assert "Unknown Bowl Deluxe" in report
    assert "Borscht with Sour Cream and Dill" not in report
    assert "Buckwheat with Tomatoes and Herbs" not in report
    assert "Traditional Yeast (100g)" not in report
    assert "Asian Beef Salad" not in report
    assert "????" not in report


def test_pending_and_undo_reports_localize_legacy_name_without_display_name():
    record = NutritionRecord(
        is_food=True,
        meal_name="Assorted Dried Meat and Fish Platter",
        items=[],
        calories_kcal=480,
        protein_g=35,
        fat_g=30,
        carbs_g=8,
        confidence=0.87,
        raw_summary="Snack plate",
        display_name="",
    )

    prompt = format_pending_meal_prompt(record)
    undo_report = format_undo_meal_report(
        UndoMealResult(
            deleted=True,
            sqlite_id=1,
            meal_name="Vegetable Crudités Platter with Crackers and Olives",
            calories_kcal=320,
            protein_g=8,
            fat_g=18,
            carbs_g=29,
            occurred_at="2026-06-19 09:00:00",
        )
    )

    assert "\u0410\u0441\u0441\u043e\u0440\u0442\u0438 \u0438\u0437 \u0432\u044f\u043b\u0435\u043d\u043e\u0433\u043e \u043c\u044f\u0441\u0430 \u0438 \u0440\u044b\u0431\u044b" in prompt
    assert "Assorted Dried Meat and Fish Platter" not in prompt
    assert "\u041e\u0432\u043e\u0449\u043d\u0430\u044f \u0442\u0430\u0440\u0435\u043b\u043a\u0430 \u0441 \u043a\u0440\u0435\u043a\u0435\u0440\u0430\u043c\u0438 \u0438 \u043e\u043b\u0438\u0432\u043a\u0430\u043c\u0438" in undo_report
    assert "Vegetable Crudit" not in undo_report
    assert "????" not in prompt
    assert "????" not in undo_report



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


def test_delete_last_meal_removes_only_latest_record_for_user(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    older = _build_record(
        meal_name="Овсянка",
        calories_kcal=200,
        protein_g=8,
        fat_g=4,
        carbs_g=32,
    )
    newer = _build_record(
        meal_name="Запеченная семга",
        calories_kcal=520,
        protein_g=35,
        fat_g=20,
        carbs_g=30,
    )

    older_id, _ = diary.save_record(
        user_id=70,
        source="text",
        record=older,
        image_ref="telegram:70:1",
        occurred_at=now - timedelta(hours=2),
    )
    newer_id, _ = diary.save_record(
        user_id=70,
        source="text",
        record=newer,
        image_ref="telegram:70:2",
        occurred_at=now - timedelta(minutes=5),
    )

    deleted = diary.delete_last_meal(70, now=now)
    summary = diary.get_daily_summary(user_id=70, now=now)
    report = format_nutrition_diary_report(summary)

    assert older_id is not None
    assert newer_id is not None
    assert deleted.deleted is True
    assert deleted.sqlite_id == newer_id
    assert deleted.meal_name == "Запеченная семга"
    assert summary["entry_count"] == 1
    assert summary["entries"][0]["id"] == older_id
    assert summary["calories_kcal"] == pytest.approx(200.0)
    assert "Овсянка" in report
    assert "Запеченная семга" not in report


def test_delete_last_meal_does_not_touch_other_users(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    user_one = _build_record(
        meal_name="Суп",
        calories_kcal=180,
        protein_g=10,
        fat_g=6,
        carbs_g=22,
    )
    user_two = _build_record(
        meal_name="Паста",
        calories_kcal=430,
        protein_g=16,
        fat_g=12,
        carbs_g=58,
    )

    diary.save_record(
        user_id=80,
        source="text",
        record=user_one,
        image_ref="telegram:80:1",
        occurred_at=now - timedelta(minutes=30),
    )
    other_id, _ = diary.save_record(
        user_id=81,
        source="text",
        record=user_two,
        image_ref="telegram:81:1",
        occurred_at=now - timedelta(minutes=10),
    )

    deleted = diary.delete_last_meal(80, now=now)
    other_summary = diary.get_daily_summary(user_id=81, now=now)

    assert deleted.deleted is True
    assert other_id is not None
    assert other_summary["entry_count"] == 1
    assert other_summary["entries"][0]["id"] == other_id
    assert other_summary["entries"][0]["meal_name"] == "Паста"


def test_delete_last_meal_empty_diary_returns_empty_result(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    deleted = diary.delete_last_meal(91)
    report = format_undo_meal_report(deleted)

    assert deleted.deleted is False
    assert "Удалять нечего" in report
    assert "????" not in report


def test_format_undo_meal_report_is_html_safe_and_unicode_clean():
    result = UndoMealResult(
        deleted=True,
        sqlite_id=55,
        meal_name='Семга & <сыр>',
        calories_kcal=520,
        protein_g=35,
        fat_g=20,
        carbs_g=30,
        occurred_at="2026-06-17 08:30:00",
    )

    report = format_undo_meal_report(result)

    assert "&lt;сыр&gt;" in report
    assert "<b>Семга &amp; &lt;сыр&gt;</b>" in report
    assert "Было: 520 ккал · Б 35 г · Ж 20 г · У 30 г" in report
    assert "????" not in report



def test_update_last_meal_tool_updates_only_latest_entry(tmp_path, monkeypatch):
    adapter = _RecordingQdrantAdapter(should_succeed=True)
    diary = HealBiteNutritionDiary(
        db_path=tmp_path / "healbite.db",
        qdrant_adapter=adapter,
        background_write=False,
    )
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    older = _build_record(
        meal_name="\u0413\u0440\u0435\u0447\u043a\u0430",
        calories_kcal=310,
        protein_g=11,
        fat_g=7,
        carbs_g=49,
    )
    newer = _build_record(
        meal_name="\u041a\u0443\u0440\u0438\u0446\u0430",
        calories_kcal=520,
        protein_g=42,
        fat_g=18,
        carbs_g=14,
    )
    older_id, _ = diary.save_record(
        user_id=92,
        source="text",
        record=older,
        image_ref="telegram:92:1",
        occurred_at=now - timedelta(hours=2),
    )
    newer_id, _ = diary.save_record(
        user_id=92,
        source="text",
        record=newer,
        image_ref="telegram:92:2",
        occurred_at=now - timedelta(minutes=5),
    )
    monkeypatch.setattr("tools.healbite_nutrition_diary_tool.get_default_nutrition_diary", lambda: diary)
    tokens = set_session_vars(platform="telegram", chat_id="chat-92", user_id="92", session_key="s-92", session_id="session-92")
    try:
        payload = json.loads(update_last_meal_tool(new_calories=400))
    finally:
        clear_session_vars(tokens)

    summary = diary.get_daily_summary(user_id=92, now=now)
    assert payload["success"] is True
    assert payload["sqlite_id"] == newer_id
    assert "ОБЯЗАТЕЛЬНО ответь пользователю" in payload["assistant_directive"]
    assert payload["user_facing_reply"] == (
        "✅ Исправил последнюю запись:\n"
        "Курица — 400 ккал\n"
        "Б 42 г · Ж 18 г · У 14 г\n\n"
        "Вызови /diary, чтобы посмотреть итог за день."
    )
    assert older_id is not None
    assert newer_id is not None
    entries_by_id = {entry["id"]: entry for entry in summary["entries"]}
    assert entries_by_id[older_id]["calories_kcal"] == pytest.approx(310.0)
    assert entries_by_id[newer_id]["calories_kcal"] == pytest.approx(400.0)


def test_update_last_meal_tool_ignores_none_fields(tmp_path, monkeypatch):
    adapter = _RecordingQdrantAdapter(should_succeed=True)
    diary = HealBiteNutritionDiary(
        db_path=tmp_path / "healbite.db",
        qdrant_adapter=adapter,
        background_write=False,
    )
    now = datetime.now(timezone.utc)
    original = _build_record(
        meal_name="\u0411\u043e\u0440\u0449",
        calories_kcal=280,
        protein_g=12,
        fat_g=9,
        carbs_g=31,
    )
    sqlite_id, _ = diary.save_record(
        user_id=93,
        source="text",
        record=original,
        image_ref="telegram:93:1",
        occurred_at=now,
    )
    monkeypatch.setattr("tools.healbite_nutrition_diary_tool.get_default_nutrition_diary", lambda: diary)
    tokens = set_session_vars(platform="telegram", chat_id="chat-93", user_id="93", session_key="s-93", session_id="session-93")
    try:
        payload = json.loads(update_last_meal_tool(new_calories=400))
    finally:
        clear_session_vars(tokens)

    summary = diary.get_daily_summary(user_id=93, now=now)
    row = summary["entries"][0]
    assert payload["success"] is True
    assert payload["sqlite_id"] == sqlite_id
    assert "Б 12 г · Ж 9 г · У 31 г" in payload["user_facing_reply"]
    assert row["meal_name"] == "\u0411\u043e\u0440\u0449"
    assert row["calories_kcal"] == pytest.approx(400.0)
    assert row["protein_g"] == pytest.approx(12.0)
    assert row["fat_g"] == pytest.approx(9.0)
    assert row["carbs_g"] == pytest.approx(31.0)


def test_update_last_meal_tool_returns_error_for_empty_diary(tmp_path, monkeypatch):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    monkeypatch.setattr("tools.healbite_nutrition_diary_tool.get_default_nutrition_diary", lambda: diary)
    tokens = set_session_vars(platform="telegram", chat_id="chat-94", user_id="94", session_key="s-94", session_id="session-94")
    try:
        payload = json.loads(update_last_meal_tool(new_calories=400))
    finally:
        clear_session_vars(tokens)

    assert payload["success"] is False
    assert payload["code"] == "diary_empty"
    assert "\u043d\u0435\u0442 \u0437\u0430\u043f\u0438\u0441\u0435\u0439" in payload["error"]


def test_update_last_meal_tool_isolated_by_user_id(tmp_path, monkeypatch):
    adapter = _RecordingQdrantAdapter(should_succeed=True)
    diary = HealBiteNutritionDiary(
        db_path=tmp_path / "healbite.db",
        qdrant_adapter=adapter,
        background_write=False,
    )
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    user_one = _build_record(
        meal_name="\u041f\u043b\u043e\u0432",
        calories_kcal=640,
        protein_g=22,
        fat_g=24,
        carbs_g=68,
    )
    user_two = _build_record(
        meal_name="\u0421\u0430\u043b\u0430\u0442",
        calories_kcal=180,
        protein_g=8,
        fat_g=11,
        carbs_g=16,
    )
    diary.save_record(
        user_id=95,
        source="text",
        record=user_one,
        image_ref="telegram:95:1",
        occurred_at=now - timedelta(minutes=10),
    )
    second_id, _ = diary.save_record(
        user_id=96,
        source="text",
        record=user_two,
        image_ref="telegram:96:1",
        occurred_at=now - timedelta(minutes=5),
    )
    monkeypatch.setattr("tools.healbite_nutrition_diary_tool.get_default_nutrition_diary", lambda: diary)
    tokens = set_session_vars(platform="telegram", chat_id="chat-96", user_id="96", session_key="s-96", session_id="session-96")
    try:
        payload = json.loads(update_last_meal_tool(new_meal_name="\u041e\u0431\u043d\u043e\u0432\u043b\u0451\u043d\u043d\u044b\u0439 \u0441\u0430\u043b\u0430\u0442", new_calories=210))
    finally:
        clear_session_vars(tokens)

    first_summary = diary.get_daily_summary(user_id=95, now=now)
    second_summary = diary.get_daily_summary(user_id=96, now=now)
    assert payload["success"] is True
    assert payload["sqlite_id"] == second_id
    assert "210 ккал" in payload["user_facing_reply"]
    assert first_summary["entries"][0]["meal_name"] == "\u041f\u043b\u043e\u0432"
    assert first_summary["entries"][0]["calories_kcal"] == pytest.approx(640.0)
    assert second_summary["entries"][0]["meal_name"] == "\u041e\u0431\u043d\u043e\u0432\u043b\u0451\u043d\u043d\u044b\u0439 \u0441\u0430\u043b\u0430\u0442"
    assert second_summary["entries"][0]["calories_kcal"] == pytest.approx(210.0)


def test_build_update_last_meal_user_reply_omits_missing_macros_and_flattens_name():
    reply = _build_update_last_meal_user_reply(
        meal_name="Суп\nдня",
        calories_kcal=315.0,
        protein_g=None,
        fat_g=11,
        carbs_g=None,
    )

    assert reply == (
        "✅ Исправил последнюю запись:\n"
        "Суп дня — 315 ккал\n"
        "Ж 11 г\n\n"
        "Вызови /diary, чтобы посмотреть итог за день."
    )


def test_build_update_last_meal_user_reply_localizes_legacy_meal_name():
    reply = _build_update_last_meal_user_reply(
        meal_name="Borscht with Sour Cream and Dill",
        calories_kcal=251,
        protein_g=9,
        fat_g=14,
        carbs_g=18,
    )

    assert "\u0411\u043e\u0440\u0449 \u0441\u043e \u0441\u043c\u0435\u0442\u0430\u043d\u043e\u0439 \u0438 \u0443\u043a\u0440\u043e\u043f\u043e\u043c" in reply
    assert "Borscht with Sour Cream and Dill" not in reply
    assert "????" not in reply

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
async def test_telegram_stats_command_defaults_to_7d(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    captured_days: list[int] = []

    def _fake_summary(*args, **kwargs):
        captured_days.append(kwargs.get("days", 1))
        return {
            "entries": [{"meal_name": "Суп", "calories_kcal": 300, "protein_g": 12, "fat_g": 8, "carbs_g": 40}],
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
        lambda summary: f"📊 <b>Твоя статистика за {summary['days']} дней:</b>\n📈 <b>В среднем за день:</b>",
    )

    msg = SimpleNamespace(
        text="/stats",
        chat=SimpleNamespace(id=221, type="private"),
        from_user=SimpleNamespace(id=221),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=21, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "В среднем за день" in kwargs["text"]
    assert captured_days == [7]
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_stats_invalid_argument_returns_usage(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    monkeypatch.setattr(
        "gateway.platforms.telegram.compute_nutrition_diary_summary",
        lambda *args, **kwargs: pytest.fail("stats summary should not run for invalid args"),
    )

    msg = SimpleNamespace(
        text="/stats abc",
        chat=SimpleNamespace(id=223, type="private"),
        from_user=SimpleNamespace(id=223),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=22, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Использование: /stats [7d]" == kwargs["text"]
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
async def test_telegram_undo_meal_command_routes_correctly(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    class _FakeDiary:
        def delete_last_meal(self, user_id):
            assert user_id == 555
            return UndoMealResult(
                deleted=True,
                sqlite_id=101,
                meal_name="Запеченная семга",
                calories_kcal=520,
                protein_g=35,
                fat_g=20,
                carbs_g=30,
            )

    monkeypatch.setattr("gateway.platforms.telegram.get_default_nutrition_diary", lambda: _FakeDiary())
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_undo_meal_report",
        lambda result: (
            f"\U0001f5d1 Запись <b>{result.meal_name}</b> удалена.\n"
            "Было: 520 ккал · Б 35 г · Ж 20 г · У 30 г"
        ),
    )

    msg = SimpleNamespace(
        text="/undo_meal",
        chat=SimpleNamespace(id=555, type="private"),
        from_user=SimpleNamespace(id=555),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=5, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "удалена" in kwargs["text"]
    assert "520 ккал" in kwargs["text"]
    assert kwargs["parse_mode"] is not None
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_diary_undo_alias_routes_correctly(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True

    class _FakeDiary:
        def delete_last_meal(self, user_id):
            assert user_id == 556
            return UndoMealResult(deleted=False)

    monkeypatch.setattr("gateway.platforms.telegram.get_default_nutrition_diary", lambda: _FakeDiary())
    monkeypatch.setattr(
        "gateway.platforms.telegram.format_undo_meal_report",
        lambda result: "Удалять нечего — сегодня дневник пуст." if not result.deleted else "unexpected",
    )

    msg = SimpleNamespace(
        text="/diary_undo",
        chat=SimpleNamespace(id=556, type="private"),
        from_user=SimpleNamespace(id=556),
        message_thread_id=None,
    )
    update = SimpleNamespace(update_id=6, message=msg, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    adapter._send_message_with_thread_fallback.assert_awaited_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Удалять нечего" in kwargs["text"]
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


def _telegram_source(user_id: int = 111):
    return SimpleNamespace(platform=Platform.TELEGRAM, user_id=str(user_id), chat_id=f"chat-{user_id}")


def _telegram_text_event(text: str):
    return SimpleNamespace(text=text, message_type=None, media_types=[])


@pytest.mark.parametrize(
    "text",
    [
        "исправь последнюю запись на 400 ккал",
        "добавь к последней записи 100 ккал",
        "переименуй последнюю запись в борщ",
    ],
)
def test_explicit_diary_correction_turn_blocks_general_tools(text):
    source = _telegram_source()
    event = _telegram_text_event(text)

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=source,
        event=event,
        message=text,
        history=[],
        enabled_toolsets=["hermes-telegram", "terminal", "file", "code_execution", "delegation"],
        disabled_toolsets=[],
    )

    assert _classify_telegram_diary_turn(
        source=source,
        event=event,
        message=text,
        history=[],
    ) == "correction"
    assert enabled == ["nutrition_diary"]
    assert {"terminal", "file", "code_execution", "delegation"} <= set(disabled)
    assert _exec_approval_policy_for_turn(
        source=source,
        event=event,
        message=text,
        history=[],
    ) == "auto_deny"


@pytest.mark.asyncio
async def test_natural_language_diary_summary_short_circuits_without_tools(monkeypatch):
    runner = object.__new__(GatewayRunner)
    source = _telegram_source(201)
    event = _telegram_text_event("что у меня сегодня в дневнике?")

    monkeypatch.setattr(
        "gateway.healbite_nutrition_diary.compute_nutrition_diary_summary",
        lambda *args, **kwargs: {
            "entries": [{"meal_name": "Суп", "calories_kcal": 300, "protein_g": 12, "fat_g": 8, "carbs_g": 40}],
            "entry_count": 1,
            "calories_kcal": 300,
            "protein_g": 12,
            "fat_g": 8,
            "carbs_g": 40,
            "days": kwargs.get("days", 1),
        },
    )
    monkeypatch.setattr(
        "gateway.healbite_nutrition_diary.format_nutrition_diary_report",
        lambda summary: "<b>Твой дневник за сегодня:</b> Калории: 300 ккал",
    )

    result = await runner._maybe_handle_healbite_nutrition_diary_turn(
        message=event.text,
        history=[],
        source=source,
        session_id="session-201",
        event=event,
    )

    assert result is not None
    assert "Твой дневник" in result["final_response"]
    assert result["tools"] == []
    assert result["api_calls"] == 0


@pytest.mark.asyncio
async def test_ambiguous_diary_turn_returns_clarification_without_tools(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = HealBiteNutritionDiary(
        db_path=tmp_path / "ambiguous-healbite.db",
        background_write=False,
    )
    source = _telegram_source(202)
    event = _telegram_text_event("наверное тут ошибка")

    result = await runner._maybe_handle_healbite_nutrition_diary_turn(
        message=event.text,
        history=[
            {"role": "assistant", "content": "Твой дневник за сегодня: 400 ккал"},
        ],
        source=source,
        session_id="session-202",
        event=event,
    )

    assert result is not None
    assert "исправить запись в дневнике" in result["final_response"]
    assert "400 ккал" in result["messages"][0]["content"] or result["messages"][0]["content"] == event.text
    assert result["tools"] == []
    assert result["api_calls"] == 0


def test_unrelated_code_fix_request_is_not_classified_as_diary_turn():
    source = _telegram_source()
    event = _telegram_text_event("исправь ошибку в коде")

    assert _classify_telegram_diary_turn(
        source=source,
        event=event,
        message=event.text,
        history=[],
    ) == "none"


def test_photo_tool_guard_still_blocks_general_toolsets():
    source = _telegram_source()
    event = SimpleNamespace(text="посчитай кбжу", message_type=None, media_types=["image/jpeg"])

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=source,
        event=event,
        message=event.text,
        history=[],
        enabled_toolsets=["terminal", "file", "code_execution", "vision"],
        disabled_toolsets=[],
    )

    assert "vision" in enabled
    assert "terminal" not in enabled
    assert {"terminal", "file", "code_execution"} <= set(disabled)


@pytest.mark.asyncio
async def test_pending_meal_yes_reply_saves_and_clears_state(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    diary.stage_pending_meal(
        user_id=203,
        source="vision",
        record=_build_record(
            meal_name="Борщ",
            calories_kcal=300,
            protein_g=10,
            fat_g=12,
            carbs_g=28,
        ),
        image_ref="telegram:203:1",
        occurred_at=datetime.now(timezone.utc),
    )

    runner = object.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary
    source = _telegram_source(203)
    event = _telegram_text_event("Да")

    result = await runner._maybe_handle_healbite_nutrition_diary_turn(
        message=event.text,
        history=[],
        source=source,
        session_id="session-203",
        event=event,
    )

    assert result is not None
    assert "Сохранено" in result["final_response"]
    assert diary.get_pending_meal(203) is None
    assert diary.get_daily_summary(user_id=203)["entry_count"] == 1
    assert result["tools"] == []
    assert result["api_calls"] == 0


@pytest.mark.asyncio
async def test_pending_meal_no_reply_clears_state_without_writing(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    diary.stage_pending_meal(
        user_id=204,
        source="vision",
        record=_build_record(
            meal_name="Каша",
            calories_kcal=220,
            protein_g=7,
            fat_g=5,
            carbs_g=40,
        ),
        image_ref="telegram:204:2",
        occurred_at=datetime.now(timezone.utc),
    )

    runner = object.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary
    source = _telegram_source(204)
    event = _telegram_text_event("Нет")

    result = await runner._maybe_handle_healbite_nutrition_diary_turn(
        message=event.text,
        history=[],
        source=source,
        session_id="session-204",
        event=event,
    )

    assert result is not None
    assert "Отменено" in result["final_response"]
    assert diary.get_pending_meal(204) is None
    assert diary.get_daily_summary(user_id=204)["entry_count"] == 0
    assert result["tools"] == []
    assert result["api_calls"] == 0


@pytest.mark.asyncio
async def test_pending_meal_does_not_block_diary_command(monkeypatch, tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    diary.stage_pending_meal(
        user_id=205,
        source="vision",
        record=_build_record(
            meal_name="Суп",
            calories_kcal=250,
            protein_g=9,
            fat_g=7,
            carbs_g=31,
        ),
        image_ref="telegram:205:3",
        occurred_at=datetime.now(timezone.utc),
    )

    runner = object.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary

    monkeypatch.setattr(
        "gateway.healbite_nutrition_diary.compute_nutrition_diary_summary",
        lambda *args, **kwargs: {
            "entries": [],
            "entry_count": 0,
            "calories_kcal": 0,
            "protein_g": 0,
            "fat_g": 0,
            "carbs_g": 0,
            "days": kwargs.get("days", 1),
            "targets_available": False,
            "progress": {},
            "comparison_values": {},
            "hints": [],
        },
    )
    monkeypatch.setattr(
        "gateway.healbite_nutrition_diary.format_nutrition_diary_report",
        lambda summary: "<b>Твой дневник за сегодня пока пуст.</b>",
    )

    event = SimpleNamespace(
        text="/diary",
        source=_telegram_source(205),
    )
    response = await runner._handle_healbite_nutrition_diary_command(event)

    assert "Твой дневник" in response
    assert diary.get_pending_meal(205) is None


def test_format_pending_meal_prompt_localized_and_html_safe():
    record = NutritionRecord(
        is_food=True,
        meal_name="Dried Fish and Meat Jerky Platter",
        items=[],
        calories_kcal=1170,
        protein_g=60,
        fat_g=85,
        carbs_g=12,
        confidence=0.9,
        raw_summary="Snack platter",
        display_name="Вяленая & рыба <снеки>",
    )

    prompt = format_pending_meal_prompt(record)

    assert "🍽 Я вижу: Вяленая &amp; рыба &lt;снеки&gt;." in prompt
    assert "Оценка: примерно 1170 ккал" in prompt
    assert "Б 60 г · Ж 85 г · У 12 г" in prompt
    assert "Сохранить в дневник?" in prompt
    assert "Ответьте: Да или Нет." in prompt
    assert "Распознано приблизительно" not in prompt
    assert "????" not in prompt


def test_format_pending_meal_prompt_handles_missing_macros():
    record = NutritionRecord(
        is_food=True,
        meal_name="Meal",
        items=[],
        calories_kcal=315,
        protein_g=None,
        fat_g=11,
        carbs_g=None,
        confidence=0.8,
        raw_summary="Meal summary",
        display_name="Суп дня",
    )

    prompt = format_pending_meal_prompt(record)

    assert "🍽 Я вижу: Суп дня." in prompt
    assert "Оценка: примерно 315 ккал" in prompt
    assert "Ж 11 г" in prompt
    assert "Б " not in prompt
    assert "У " not in prompt


def test_normalize_nutrition_payload_prefers_russian_display_name():
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "Dried Fish and Meat Jerky Platter",
                "display_name": "Вяленая рыба и мясные снеки",
                "raw_summary": "Russian-friendly title should win",
                "totals": {"calories_kcal": 1170, "protein_g": 60, "fat_g": 85, "carbs_g": 12},
                "items": [
                    {"name": "Dried fish", "display_name": "Вяленая рыба"},
                    {"name": "Meat jerky", "display_name": "Мясные снеки"},
                ],
            },
            ensure_ascii=False,
        )
    )

    assert record is not None
    assert record.meal_name == "Dried Fish and Meat Jerky Platter"
    assert record.display_name == "Вяленая рыба и мясные снеки"
    assert record.items[0]["name"] == "Вяленая рыба"
    assert "Dried Fish and Meat Jerky Platter" not in format_pending_meal_prompt(record)


def test_diary_report_uses_display_name_for_new_vision_records(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "Dried Fish and Meat Jerky Platter",
                "display_name": "Вяленая рыба и мясные снеки",
                "raw_summary": "Snack platter",
                "totals": {"calories_kcal": 1170, "protein_g": 60, "fat_g": 85, "carbs_g": 12},
                "items": [{"name": "Dried fish", "display_name": "Вяленая рыба"}],
            },
            ensure_ascii=False,
        )
    )

    diary.save_record(
        user_id=501,
        source="vision",
        record=record,
        image_ref="telegram:501:1",
        occurred_at=datetime.now(timezone.utc),
    )

    report = format_nutrition_diary_report(diary.get_daily_summary(user_id=501))

    assert "Вяленая рыба и мясные снеки" in report
    assert "Dried Fish and Meat Jerky Platter" not in report
    assert "????" not in report
