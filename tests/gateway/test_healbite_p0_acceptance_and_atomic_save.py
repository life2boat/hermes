"""Tests for HealBite P0 Production Acceptance, Atomic Save, and Memory Isolation.

Covers:
- P0-A: Atomic Save, Transactional Rollback, Concurrency, and Idempotency
- P0-B: Portion Estimation, Approximate Ranges, and Acceptance Scenarios A1-A10
- P0-C: Storage Hygiene and Cross-User Isolation
"""

from __future__ import annotations

import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from gateway.healbite_nutrition_diary import (
    ALBUM_DECISION_SAME_PORTION,
    ALBUM_DECISION_UNRESOLVED,
    ALBUM_OVERLAP_DISTINCT_PORTIONS,
    ALBUM_OVERLAP_SAME_PORTION,
    ALBUM_OVERLAP_UNRESOLVED,
    ALBUM_PROCESSING_COMPLETE,
    ALBUM_PROCESSING_PARTIAL,
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    PendingMealPayload,
    calculate_inventory_nutrition,
    format_food_vision_inventory_reply,
    NutritionRecord,
)
from scripts.hermes_runtime_attestation_v2 import (
    CheckStatus,
    audit_memory_isolation,
)


def _sample_item(name: str, grams: float | None = None, min_g: float | None = None, max_g: float | None = None) -> FoodVisionItem:
    if min_g is None and max_g is None and grams is not None:
        min_g = round(grams * 0.9, 1)
        max_g = round(grams * 1.1, 1)
    return FoodVisionItem(
        visible_name=name,
        normalized_name=name.casefold(),
        confidence=0.92,
        estimated_grams_min=min_g,
        estimated_grams_max=max_g,
    )


def _sample_inventory(*items: FoodVisionItem, photo_count: int = 1) -> FoodVisionInventory:
    return FoodVisionInventory(
        schema_version="2.0",
        items=list(items),
        overall_confidence=0.91,
        warnings=[],
        needs_user_confirmation=False,
    )


# ============================================================================
# P0-A: ATOMIC SAVE & IDEMPOTENCY
# ============================================================================

def test_atomic_save_success(tmp_path: Path):
    """Save succeeds: 1 log record inserted and pending state deleted atomically."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Гречневая каша", 200.0, 180.0, 220.0))
    staged = diary.stage_pending_inventory(
        user_id=101,
        source="photo",
        inventory=inv,
        image_ref="tg:101:1",
        occurred_at=datetime.now(timezone.utc),
    )

    res = diary.save_pending_inventory_directly(101, expected_inventory_id=staged.inventory_id)
    assert res.status == "saved"
    assert res.saved is True
    assert res.sqlite_id is not None

    # Verify pending state was cleared
    assert diary.get_pending_inventory(101) is None

    # Verify nutrition_log has exactly 1 record
    with diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 101").fetchall()
        assert len(rows) == 1
        assert "Гречневая каша" in rows[0]["meal_name"]


def test_atomic_save_error_before_write(tmp_path: Path):
    """Error before write (stale ID or expired): aborts with no change to nutrition_log."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Овсянка", 150.0, 130.0, 170.0))
    diary.stage_pending_inventory(
        user_id=102,
        source="photo",
        inventory=inv,
        image_ref="tg:102:1",
        occurred_at=datetime.now(timezone.utc),
    )

    # Attempt save with wrong inventory ID
    res = diary.save_pending_inventory_directly(102, expected_inventory_id="wrong_id")
    assert res.status == "stale"
    assert res.saved is False

    # Pending inventory is still preserved!
    assert diary.get_pending_inventory(102) is not None

    # Zero records in nutrition log
    with diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 102").fetchall()
        assert len(rows) == 0


def test_atomic_save_error_during_write_preserves_pending(tmp_path: Path):
    """Critical invariant: Exception during log insertion rolls back; pending state is NOT lost!"""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Куриное филе", 180.0, 160.0, 200.0))
    staged = diary.stage_pending_inventory(
        user_id=103,
        source="photo",
        inventory=inv,
        image_ref="tg:103:1",
        occurred_at=datetime.now(timezone.utc),
    )

    # Trigger failure during INSERT INTO nutrition_log via SQLite trigger
    with diary._connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_nutrition_log_insert BEFORE INSERT ON nutrition_log "
            "BEGIN SELECT RAISE(FAIL, 'Simulated disk failure during insert'); END;"
        )

    raised = False
    try:
        diary.save_pending_inventory_directly(103, expected_inventory_id=staged.inventory_id)
    except sqlite3.DatabaseError as e:
        raised = True
        assert "Simulated disk failure" in str(e)
    assert raised, "Expected sqlite3.DatabaseError was not raised"

    # CRITICAL: Pending state was NOT lost! It was rolled back!
    preserved = diary.get_pending_inventory(103)
    assert preserved is not None
    assert preserved.inventory_id == staged.inventory_id

    # 0 records in nutrition log
    with diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 103").fetchall()
        assert len(rows) == 0

    # Clean up trigger and confirm save now works
    with diary._connect() as conn:
        conn.execute("DROP TRIGGER fail_nutrition_log_insert")
    res_retry = diary.save_pending_inventory_directly(103, expected_inventory_id=staged.inventory_id)
    assert res_retry.status == "saved"
    assert diary.get_pending_inventory(103) is None


def test_atomic_save_error_after_commit_telegram_retry(tmp_path: Path):
    """If Telegram callback retries after DB commit, idempotency returns duplicate/missing without duplicate records."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Яблоко", 120.0, 100.0, 140.0))
    staged = diary.stage_pending_inventory(
        user_id=104,
        source="photo",
        inventory=inv,
        image_ref="tg:104:1",
        occurred_at=datetime.now(timezone.utc),
    )

    # First attempt succeeds
    res1 = diary.save_pending_inventory_directly(104, expected_inventory_id=staged.inventory_id)
    assert res1.status == "saved"

    # Simulated retry by Telegram / user
    res2 = diary.save_pending_inventory_directly(104, expected_inventory_id=staged.inventory_id)
    assert res2.status == "missing"

    # Exactly 1 record in nutrition log
    with diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 104").fetchall()
        assert len(rows) == 1


def test_atomic_save_concurrent_callbacks_deduplicated(tmp_path: Path):
    """Two concurrent threads calling save_pending_inventory_directly result in exactly 1 record."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Сырники", 160.0, 140.0, 180.0))
    staged = diary.stage_pending_inventory(
        user_id=105,
        source="photo",
        inventory=inv,
        image_ref="tg:105:1",
        occurred_at=datetime.now(timezone.utc),
    )

    results: list[Any] = []
    errors: list[Exception] = []

    def worker():
        try:
            r = diary.save_pending_inventory_directly(105, expected_inventory_id=staged.inventory_id)
            results.append(r)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    assert len(results) == 2
    # Exactly 1 log entry in DB (no double KBZHU)
    with diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 105").fetchall()
        assert len(rows) == 1

    # Exactly one successfully performed the initial save
    saved_initial = [r for r in results if r.status == "saved" and "уже есть в дневнике" not in r.reply_text and "ℹ️" not in r.reply_text]
    assert len(saved_initial) == 1
    assert saved_initial[0].sqlite_id == rows[0]["id"]


def test_atomic_save_recovery_after_process_restart(tmp_path: Path):
    """Pending operation stored in SQLite is preserved across diary instance recreation (simulating process restart)."""
    db_path = tmp_path / "test.db"
    diary1 = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Куриная грудка", 200.0, 180.0, 220.0))
    staged = diary1.stage_pending_inventory(
        user_id=106,
        source="photo",
        inventory=inv,
        image_ref="tg:106:1",
        occurred_at=datetime.now(timezone.utc),
    )

    # Simulate restart: create new diary instance from same DB file
    diary2 = HealBiteNutritionDiary(db_path=db_path)
    recovered = diary2.get_pending_inventory(106)
    assert recovered is not None
    assert recovered.inventory_id == staged.inventory_id

    # Complete save with the recovered instance
    res = diary2.save_pending_inventory_directly(106, expected_inventory_id=staged.inventory_id)
    assert res.status == "saved"

    with diary2._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 106").fetchall()
        assert len(rows) == 1
        assert "Куриная грудка" in rows[0]["meal_name"]


def test_atomic_save_unauthorized_user_blocked(tmp_path: Path):
    """User B cannot commit User A's pending inventory."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Салат Цезарь", 180.0, 160.0, 200.0))
    staged = diary.stage_pending_inventory(
        user_id=107,
        source="photo",
        inventory=inv,
        image_ref="tg:107:1",
        occurred_at=datetime.now(timezone.utc),
    )

    # User 999 attempts to save User 107's pending inventory
    res = diary.save_pending_inventory_directly(999, expected_inventory_id=staged.inventory_id)
    assert res.status == "missing"

    # User 999 attempts partial save
    res_partial = diary.save_partial_pending_inventory(999, expected_inventory_id=staged.inventory_id)
    assert res_partial.status == "missing"

    # User 107's inventory is completely unaffected
    assert diary.get_pending_inventory(107) is not None


# ============================================================================
# P0-B: SCENARIOS A1 TO A10
# ============================================================================

def test_scenario_a1_single_photo_simple_dish(tmp_path: Path):
    """A1: Single photo simple dish -> KBZHU calculated and saved."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Гречневая каша", 200.0, 180.0, 220.0))
    staged = diary.stage_pending_inventory(
        user_id=201,
        source="photo",
        inventory=inv,
        image_ref="tg:201:1",
        occurred_at=datetime.now(timezone.utc),
    )
    calc = calculate_inventory_nutrition(staged)
    assert calc.record is not None
    assert calc.record.calories_kcal is not None and calc.record.calories_kcal > 0

    res = diary.save_pending_inventory_directly(201)
    assert res.status == "saved"
    assert res.record is not None
    assert res.record.calories_kcal is not None
    assert res.record.calories_kcal > 0


def test_scenario_a2_single_photo_composite_dish(tmp_path: Path):
    """A2: Single photo composite dish -> main ingredients identified and calculated."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(
        _sample_item("Куриная грудка", 150.0, 130.0, 170.0),
        _sample_item("Рис", 150.0, 130.0, 170.0),
        _sample_item("Огурцы", 100.0, 80.0, 120.0),
    )
    staged = diary.stage_pending_inventory(
        user_id=202,
        source="photo",
        inventory=inv,
        image_ref="tg:202:1",
        occurred_at=datetime.now(timezone.utc),
    )
    assert len(staged.items) == 3
    calc = calculate_inventory_nutrition(staged)
    assert calc.record is not None
    assert len(calc.record.items) == 3

    res = diary.save_pending_inventory_directly(202)
    assert res.status == "saved"


def test_scenario_a3_two_photos_one_portion(tmp_path: Path):
    """A3: Two photos of same portion -> overlap resolved, single unified portion saved."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(
        _sample_item("Сырники", 200.0, 180.0, 220.0),
        photo_count=2,
    )
    staged = diary.stage_pending_inventory(
        user_id=203,
        source="photo",
        inventory=inv,
        image_ref="tg:203:1",
        occurred_at=datetime.now(timezone.utc),
        photo_count=2,
        album_overlap_decision=ALBUM_OVERLAP_UNRESOLVED,
    )
    # Direct save is blocked before resolution
    res_blocked = diary.save_pending_inventory_directly(203)
    assert res_blocked.status == "album_decision_required"

    # User resolves to SAME_PORTION
    diary.resolve_album_decision(203, decision="SAME_PORTION")
    updated = diary.get_pending_inventory(203)
    assert updated is not None
    assert updated.album_overlap_decision == ALBUM_OVERLAP_SAME_PORTION

    # Now save succeeds with 1 unified record
    res = diary.save_pending_inventory_directly(203)
    assert res.status == "saved"
    with diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 203").fetchall()
        assert len(rows) == 1


def test_scenario_a4_two_distinct_dishes(tmp_path: Path):
    """A4: Two distinct dishes in album -> both accounted for."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(
        _sample_item("Сырники", 200.0, 180.0, 220.0),
        _sample_item("Яблоко", 150.0, 130.0, 170.0),
        photo_count=2,
    )
    diary.stage_pending_inventory(
        user_id=204,
        source="photo",
        inventory=inv,
        image_ref="tg:204:1",
        occurred_at=datetime.now(timezone.utc),
        photo_count=2,
        album_processing_status=ALBUM_PROCESSING_COMPLETE,
        album_overlap_decision=ALBUM_OVERLAP_DISTINCT_PORTIONS,
    )
    res = diary.save_pending_inventory_directly(204)
    assert res.status == "saved"
    with diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 204").fetchall()
        assert len(rows) == 1
        items = rows[0]["items_json"]
        assert "Сырники" in items
        assert "Яблоко" in items


def test_scenario_a5_partial_vision_failure(tmp_path: Path):
    """A5: Partial vision failure -> direct save blocked, only partial save allowed."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(
        _sample_item("Вафли", 100.0, 80.0, 120.0),
        photo_count=3,
    )
    diary.stage_pending_inventory(
        user_id=205,
        source="photo",
        inventory=inv,
        image_ref="tg:205:1",
        occurred_at=datetime.now(timezone.utc),
        photo_count=3,
        album_processing_status=ALBUM_PROCESSING_PARTIAL,
        album_overlap_decision=ALBUM_OVERLAP_DISTINCT_PORTIONS,
    )
    # Direct save is blocked
    res_direct = diary.save_pending_inventory_directly(205)
    assert res_direct.status == "album_partial_blocked"

    # Explicit partial save succeeds
    res_partial = diary.save_partial_pending_inventory(205)
    assert res_partial.status == "saved"
    assert "💾 Сохранена распознанная часть блюда" in res_partial.reply_text


def test_scenario_a6_ambiguous_overlap(tmp_path: Path):
    """A6: Ambiguous overlap -> confirmation requested before any save is unlocked."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(
        _sample_item("Яблоко", 150.0, 130.0, 170.0),
        photo_count=2,
    )
    diary.stage_pending_inventory(
        user_id=206,
        source="photo",
        inventory=inv,
        image_ref="tg:206:1",
        occurred_at=datetime.now(timezone.utc),
        photo_count=2,
        album_processing_status=ALBUM_PROCESSING_PARTIAL,
        album_overlap_decision=ALBUM_OVERLAP_UNRESOLVED,
    )
    # Both direct and partial save are blocked until overlap is resolved
    assert diary.save_pending_inventory_directly(206).status == "album_decision_required"
    assert diary.save_partial_pending_inventory(206).status == "album_decision_required"

    # Resolve overlap
    diary.resolve_album_decision(206, decision="DISTINCT_PORTIONS")
    # Partial save is now unlocked
    res = diary.save_partial_pending_inventory(206)
    assert res.status == "saved"


def test_scenario_a7_unknown_grammage_approximate_estimate(tmp_path: Path):
    """A7: Unknown grammage from Vision -> HIPE generates plausible mass range and approximate KBZHU without blocking."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)

    # Item with no grams from vision (None, None)
    item_no_grams = FoodVisionItem(
        visible_name="Гречневая каша",
        normalized_name="гречневая каша",
        confidence=0.88,
        estimated_grams_min=None,
        estimated_grams_max=None,
    )
    inv = FoodVisionInventory(
        schema_version="2.0",
        items=[item_no_grams],
        overall_confidence=0.88,
        warnings=[],
        needs_user_confirmation=False,
    )
    staged = diary.stage_pending_inventory(
        user_id=207,
        source="photo",
        inventory=inv,
        image_ref="tg:207:1",
        occurred_at=datetime.now(timezone.utc),
    )
    # Verify HIPE populated approximate grams and plausible range in calculation snapshot
    staged_item = staged.items[0]
    assert staged_item.selected_grams is None
    assert staged_item.user_modified is False
    assert staged.calculation_snapshot is not None
    assert staged.calculation_snapshot["is_approximate_range"] is True
    assert math.isclose(float(staged.calculation_snapshot["components"][0]["estimated_weight_g"]), 200.0, rel_tol=0.05)

    # Verify formatted reply shows approximate mass and KBZHU
    text = format_food_vision_inventory_reply(staged)
    assert "расчётный вес: ~200 г" in text or "вес не подтверждён" in text
    assert "Итого:" in text

    # Direct save succeeds without demanding manual input
    res = diary.save_pending_inventory_directly(207)
    assert res.status == "saved"
    assert res.record is not None
    assert res.record.is_approximate_range is True


def test_scenario_a8_repeated_save_no_duplicate(tmp_path: Path):
    """A8: Repeated save -> returns missing or duplicate, zero duplicate DB records."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Банан", 120.0, 100.0, 140.0))
    diary.stage_pending_inventory(
        user_id=208,
        source="photo",
        inventory=inv,
        image_ref="tg:208:1",
        occurred_at=datetime.now(timezone.utc),
    )
    res1 = diary.save_pending_inventory_directly(208)
    assert res1.status == "saved"

    res2 = diary.save_pending_inventory_directly(208)
    assert res2.status == "missing"

    with diary._connect() as conn:
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = 208").fetchall()
        assert len(rows) == 1


def test_scenario_a9_save_failure_no_data_loss(tmp_path: Path):
    """A9: Save failure -> pending state is not wiped; user data preserved."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Творог 5%", 150.0, 130.0, 170.0))
    staged = diary.stage_pending_inventory(
        user_id=209,
        source="photo",
        inventory=inv,
        image_ref="tg:209:1",
        occurred_at=datetime.now(timezone.utc),
    )

    with patch.object(diary, "_has_v2_columns", side_effect=sqlite3.DatabaseError("Disk full")):
        raised = False
        try:
            diary.save_pending_inventory_directly(209)
        except sqlite3.DatabaseError:
            raised = True
        assert raised, "Expected sqlite3.DatabaseError was not raised"

    # Pending inventory is STILL present!
    assert diary.get_pending_inventory(209) is not None


def test_scenario_a10_container_restart_recovery(tmp_path: Path):
    """A10: Container restart -> pending state in SQLite is intact and can be saved subsequently."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Сырники", 140.0, 120.0, 160.0))
    staged = diary.stage_pending_inventory(
        user_id=210,
        source="photo",
        inventory=inv,
        image_ref="tg:210:1",
        occurred_at=datetime.now(timezone.utc),
    )

    # Simulate container restart with fresh diary object
    restarted_diary = HealBiteNutritionDiary(db_path=db_path)
    recovered = restarted_diary.get_pending_inventory(210)
    assert recovered is not None
    assert recovered.items[0].visible_name == "Сырники"

    res = restarted_diary.save_pending_inventory_directly(210)
    assert res.status == "saved"


# ============================================================================
# P0-C: MEMORY ISOLATION & STORAGE HYGIENE
# ============================================================================

def test_memory_isolation_storage_hygiene(tmp_path: Path):
    """Storage Hygiene: SQLite and Qdrant payloads contain no base64, data URLs, or screenshot paths."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    inv = _sample_inventory(_sample_item("Яблоко", 100.0, 90.0, 110.0))
    diary.stage_pending_inventory(
        user_id=301,
        source="photo",
        inventory=inv,
        image_ref="tg:301:1",
        occurred_at=datetime.now(timezone.utc),
    )
    diary.save_pending_inventory_directly(301)

    clean_qdrant_payloads = [
        {"user_id": 301, "record_type": "nutrition_log", "meal_name": "Яблоко", "calories": 52.0}
    ]
    with diary._connect() as conn:
        res = audit_memory_isolation(
            sqlite_connection=conn,
            qdrant_payloads=clean_qdrant_payloads,
            trajectories=None,
        )
        assert res.raw_image_bytes_found is False
        assert res.base64_payload_found is False
        assert res.data_url_found is False
        assert res.screenshot_path_found is False
        assert res.private_cache_path_found is False


def test_memory_isolation_cross_user_separation(tmp_path: Path):
    """Cross-User Isolation: User A cannot read or mutate User B's pending inventory or daily logs."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)

    # User 401 stages inventory
    inv_a = _sample_inventory(_sample_item("Вафли", 200.0, 180.0, 220.0))
    diary.stage_pending_inventory(user_id=401, source="photo", inventory=inv_a, image_ref="tg:401:1", occurred_at=datetime.now(timezone.utc))

    # User 402 checks pending inventory -> sees None
    assert diary.get_pending_inventory(402) is None

    # User 401 saves
    diary.save_pending_inventory_directly(401)

    # User 402 checks daily summary -> sees 0 entries
    summary_b = diary.get_daily_summary(user_id=402)
    assert summary_b["entry_count"] == 0

    # User 401 has 1 entry
    summary_a = diary.get_daily_summary(user_id=401)
    assert summary_a["entry_count"] == 1


def test_memory_isolation_truthful_attestation_status():
    """Fail-closed: With 0 observed trajectory turns, status is truthfully NOT_VERIFIED, preventing false PASS."""
    res = audit_memory_isolation(
        sqlite_connection=None,
        qdrant_payloads=None,
        trajectories=None,
    )
    assert res.status == CheckStatus.NOT_VERIFIED.value
    assert res.coverage == "NONE"
    assert res.surfaces_checked == ()
