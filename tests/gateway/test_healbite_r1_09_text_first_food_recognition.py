"""Tests for HEALBITE R1-09: Text-First Food Recognition & Vision Bypass.

Validates:
1. Scenario A: Complete text + photo -> Vision API is NEVER called (call_count == 0),
   meal is calculated and staged directly from text, image_ref is None.
2. Scenario B1: Total weight constraint ("Это блюдо весит 350 г") -> Vision called,
   total weight scaled/adjusted to target.
3. Scenario B2: Exclusion constraint ("Без сметаны") -> Vision called,
   excluded ingredient removed from final staged items.
4. Scenario B3: Partial item weight ("Курицы здесь 120 г") -> Vision called,
   chicken weight locked to 120g with user_modified=True.
5. Scenario C1: Contradiction with complete text -> Full text wins unconditionally,
   Vision is bypassed (call_count == 0).
6. Scenario C2: Contradiction with incomplete text -> Vision runs,
   disagreement clarification requested.
7. Scenario D: Photo-only -> Vision called normally.
8. Scenario E: Unparseable chit-chat text + photo -> Vision called, fallback works.
9. Persistence: On confirmation of text-bypassed meal, image_ref is None in DB.
10. Multi-user isolation: Concurrent user sessions do not leak or overwrite state.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    HealBiteNutritionDiary,
)
from gateway.nutrition.text_parser import (
    is_full_food_description,
    parse_food_text_description,
    parse_text_meal_constraints,
)


def _make_mock_vision_response(items: list[dict], confidence: float = 0.9) -> str:
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": True,
            "items": items,
            "overall_confidence": confidence,
            "needs_user_confirmation": False,
            "warnings": [],
        }),
    })


# =============================================================================
# Unit tests: text_parser constraints & full description detection
# =============================================================================
def test_text_parser_full_description():
    # Scenario A: full ingredients with grams
    text_a = "Гречка отварная 150 г, куриная грудка запечённая 120 г, огурец 80 г"
    parsed_a = parse_food_text_description(text_a)
    assert parsed_a is not None
    assert is_full_food_description(parsed_a) is True
    assert len(parsed_a.ingredients) == 3
    assert parsed_a.total_grams == 350.0

    # Scenario B1: total weight phrase only
    text_b1 = "Это блюдо весит 350 г"
    c_b1 = parse_text_meal_constraints(text_b1)
    assert c_b1 is not None and c_b1.total_weight_g == 350.0
    parsed_b1 = parse_food_text_description(text_b1)
    assert is_full_food_description(parsed_b1) is False

    # Scenario B2: exclusion only
    text_b2 = "Без сметаны"
    c_b2 = parse_text_meal_constraints(text_b2)
    assert c_b2 is not None and "сметаны" in c_b2.excluded_ingredients
    parsed_b2 = parse_food_text_description(text_b2)
    assert is_full_food_description(parsed_b2) is False

    # Scenario B3: partial weight only
    text_b3 = "Курицы здесь 120 г"
    c_b3 = parse_text_meal_constraints(text_b3)
    assert c_b3 is not None and "курицы" in c_b3.partial_item_weights
    assert c_b3.partial_item_weights["курицы"] == 120.0
    parsed_b3 = parse_food_text_description(text_b3)
    assert is_full_food_description(parsed_b3) is False

    # Scenario C2: vague dish without weights
    text_c2 = "Это шоколадный торт с вишней"
    parsed_c2 = parse_food_text_description(text_c2)
    assert is_full_food_description(parsed_c2) is False


# =============================================================================
# Integration tests: HealBiteNutritionDiary Vision Bypass & Constraints
# =============================================================================
@pytest.mark.asyncio
async def test_scenario_a_full_text_bypasses_vision(tmp_path: Path):
    """Scenario A: Full text description + photo must NOT call Vision API at all."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    caption = "Гречка отварная 150 г, куриная грудка запечённая 120 г, огурец 80 г"

    mock_vision = AsyncMock()
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=201,
            image_path="/tmp/meal.jpg",
            user_text=caption,
            source="photo",
        )

    # 1. Vision API must NOT be called
    assert mock_vision.call_count == 0, f"Expected 0 vision calls, got {mock_vision.call_count}"

    # 2. Outcome must be pending and valid
    assert outcome.available is True
    assert outcome.pending is True
    assert outcome.validation_status == "VALID"
    assert outcome.clarification_text is not None
    assert "Гречка" in outcome.clarification_text and "куриная грудка" in outcome.clarification_text.lower()

    # 3. Items match parsed text
    inv = outcome.pending_inventory
    assert inv is not None
    assert len(inv.items) == 3
    names = [it.visible_name.lower() for it in inv.items]
    assert any("гречка" in n for n in names)
    assert any("грудка" in n or "курица" in n for n in names)
    assert any("огурец" in n for n in names)

    total_grams = sum(it.selected_grams for it in inv.items)
    assert round(total_grams, 1) == 350.0

    # 4. Image ref must be None (no photo saved in DB)
    assert inv.image_ref is None
    assert inv.photo_count == 0


@pytest.mark.asyncio
async def test_scenario_b1_total_weight_constraint(tmp_path: Path):
    """Scenario B1: User specifies total weight constraint -> Vision called, total weight adjusted."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    caption = "Это блюдо весит 350 г"

    mock_items = [
        {"visible_name": "Плов с говядиной", "normalized_name": "плов", "confidence": 0.9, "estimated_grams_min": 200.0, "estimated_grams_max": 250.0, "preparation": "", "is_sauce": False, "uncertainty": ""},
    ]
    resp = _make_mock_vision_response(mock_items)

    mock_vision = AsyncMock(return_value=resp)
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=202,
            image_path="/tmp/plov.jpg",
            user_text=caption,
            source="photo",
        )

    # Vision was called
    assert mock_vision.call_count == 1
    assert outcome.available is True
    assert outcome.pending_inventory is not None

    inv = outcome.pending_inventory
    assert inv is not None
    total_grams = sum(it.selected_grams for it in inv.items)
    assert round(total_grams, 1) == 350.0


@pytest.mark.asyncio
async def test_scenario_b2_excluded_ingredients_constraint(tmp_path: Path):
    """Scenario B2: User specifies 'Без сметаны' -> Vision called, sour cream removed."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    caption = "Без сметаны"

    mock_items = [
        {"visible_name": "Пельмени отварные", "normalized_name": "пельмени", "confidence": 0.92, "estimated_grams_min": 180.0, "estimated_grams_max": 220.0, "preparation": "", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Сметана 20%", "normalized_name": "сметана", "confidence": 0.88, "estimated_grams_min": 25.0, "estimated_grams_max": 35.0, "preparation": "соус", "is_sauce": True, "uncertainty": ""},
    ]
    resp = _make_mock_vision_response(mock_items)

    mock_vision = AsyncMock(return_value=resp)
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=203,
            image_path="/tmp/pelmeni.jpg",
            user_text=caption,
            source="photo",
        )

    assert mock_vision.call_count == 1
    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None

    # Sour cream must be filtered out
    names = [it.visible_name.lower() for it in inv.items]
    assert not any("сметан" in n for n in names)
    assert any("пельмен" in n for n in names)
    assert len(inv.items) == 1


@pytest.mark.asyncio
async def test_scenario_b3_partial_item_weight_constraint(tmp_path: Path):
    """Scenario B3: User specifies 'Курицы здесь 120 г' -> chicken weight locked to 120g."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    caption = "Курицы здесь 120 г"

    mock_items = [
        {"visible_name": "Куриное филе", "normalized_name": "курица", "confidence": 0.91, "estimated_grams_min": 80.0, "estimated_grams_max": 100.0, "preparation": "", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Рис басмати", "normalized_name": "рис", "confidence": 0.89, "estimated_grams_min": 140.0, "estimated_grams_max": 160.0, "preparation": "", "is_sauce": False, "uncertainty": ""},
    ]
    resp = _make_mock_vision_response(mock_items)

    mock_vision = AsyncMock(return_value=resp)
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=204,
            image_path="/tmp/chicken_rice.jpg",
            user_text=caption,
            source="photo",
        )

    assert mock_vision.call_count == 1
    inv = outcome.pending_inventory
    assert inv is not None

    chicken_item = next(it for it in inv.items if "куриц" in it.visible_name.lower() or "курин" in it.visible_name.lower())
    assert chicken_item.selected_grams == 120.0
    assert chicken_item.user_modified is True


@pytest.mark.asyncio
async def test_scenario_c1_contradiction_full_text_wins(tmp_path: Path):
    """Scenario C1: Complete recipe text contradicts image -> Text wins, Vision bypassed."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    # Complete recipe with grams
    caption = "Шоколадный торт 200 г, вишня свежая 50 г"

    mock_vision = AsyncMock()
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=205,
            image_path="/tmp/soup_photo.jpg",
            user_text=caption,
            source="photo",
        )

    assert mock_vision.call_count == 0
    assert outcome.available is True
    assert outcome.validation_status == "VALID"
    inv = outcome.pending_inventory
    assert inv is not None
    names = [it.visible_name.lower() for it in inv.items]
    assert any("торт" in n for n in names)
    assert any("вишн" in n for n in names)


@pytest.mark.asyncio
async def test_scenario_c2_contradiction_ambiguous_text_asks_clarification(tmp_path: Path):
    """Scenario C2: Ambiguous text without weights contradicts image -> Vision runs, asks clarification."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    caption = "Это шоколадный торт с вишней"  # no weights!

    soup_items = [
        {"visible_name": "Борщ со сметаной", "normalized_name": "борщ", "confidence": 0.95, "estimated_grams_min": 300.0, "estimated_grams_max": 350.0, "preparation": "суп", "is_sauce": False, "uncertainty": ""},
    ]
    resp = _make_mock_vision_response(soup_items)

    mock_vision = AsyncMock(return_value=resp)
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=206,
            image_path="/tmp/borscht.jpg",
            user_text=caption,
            source="photo",
        )

    assert mock_vision.call_count == 1
    assert outcome.available is True
    assert outcome.validation_status == "DISAGREEMENT_CLARIFICATION"
    assert "Борщ" in outcome.clarification_text
    assert "торт" in outcome.clarification_text.lower()


@pytest.mark.asyncio
async def test_scenario_d_photo_only_regular_flow(tmp_path: Path):
    """Scenario D: Photo without text -> Vision called, standard flow works."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")

    salad_items = [
        {"visible_name": "Салат овощной", "normalized_name": "салат", "confidence": 0.92, "estimated_grams_min": 150.0, "estimated_grams_max": 180.0, "preparation": "", "is_sauce": False, "uncertainty": ""},
    ]
    resp = _make_mock_vision_response(salad_items)

    mock_vision = AsyncMock(return_value=resp)
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=207,
            image_path="/tmp/salad.jpg",
            user_text="",
            source="photo",
        )

    assert mock_vision.call_count == 1
    assert outcome.available is True
    assert outcome.validation_status == "VALID"
    assert outcome.pending_inventory is not None


@pytest.mark.asyncio
async def test_scenario_e_unparseable_text_graceful_vision(tmp_path: Path):
    """Scenario E: Chit-chat text -> Vision called, standard flow works."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    caption = "Привет, вот мой обед сегодня в кафе"

    steak_items = [
        {"visible_name": "Стейк говяжий", "normalized_name": "стейк", "confidence": 0.94, "estimated_grams_min": 200.0, "estimated_grams_max": 250.0, "preparation": "гриль", "is_sauce": False, "uncertainty": ""},
    ]
    resp = _make_mock_vision_response(steak_items)

    mock_vision = AsyncMock(return_value=resp)
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=208,
            image_path="/tmp/steak.jpg",
            user_text=caption,
            source="photo",
        )

    assert mock_vision.call_count == 1
    assert outcome.available is True
    assert outcome.validation_status == "VALID"


@pytest.mark.asyncio
async def test_persistence_no_photo_in_db_for_text_first(tmp_path: Path):
    """Persistence: When text bypasses vision, image_ref in DB must be NULL."""
    db_path = tmp_path / "test.db"
    diary = HealBiteNutritionDiary(db_path=db_path)
    caption = "Гречка отварная 150 г, куриная грудка запечённая 120 г, огурец 80 г"

    mock_vision = AsyncMock()
    with patch("tools.vision_tools.vision_analyze_tool", mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=209,
            image_path="/tmp/photo_to_ignore.jpg",
            user_text=caption,
            source="photo",
        )

    assert mock_vision.call_count == 0
    assert outcome.pending_inventory is not None

    # Step 1: confirm inventory
    step1 = diary.confirm_pending_inventory(user_id=209)
    assert step1 is not None
    assert step1.status == "awaiting_save_confirmation"

    # Step 2: confirm meal save
    step2 = diary.confirm_pending_meal(user_id=209)
    assert step2.status == "saved"
    assert step2.record is not None

    # Verify directly in SQLite table nutrition_log
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, meal_name, image_ref FROM nutrition_log WHERE user_id = 209")
    rows = cur.fetchall()
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    # row[3] is image_ref, must be None / NULL
    assert row[3] is None, f"Expected image_ref to be None, got {row[3]!r}"


@pytest.mark.asyncio
async def test_multi_user_isolation(tmp_path: Path):
    """Multi-user isolation: User A (text-first) and User B (photo) do not interfere."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")

    # User A: text bypass
    caption_a = "Овсянка 50 г, банан 100 г"
    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock()) as mock_v_a:
        outcome_a = await diary.analyze_and_maybe_log(
            user_id=301,
            image_path="/tmp/oats.jpg",
            user_text=caption_a,
            source="photo",
        )
        assert mock_v_a.call_count == 0

    # User B: photo vision
    photo_b_items = [
        {"visible_name": "Яблоко зеленое", "normalized_name": "яблоко", "confidence": 0.95, "estimated_grams_min": 150.0, "estimated_grams_max": 180.0, "preparation": "", "is_sauce": False, "uncertainty": ""},
    ]
    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(return_value=_make_mock_vision_response(photo_b_items))) as mock_v_b:
        outcome_b = await diary.analyze_and_maybe_log(
            user_id=302,
            image_path="/tmp/apple.jpg",
            user_text="",
            source="photo",
        )
        assert mock_v_b.call_count == 1

    # Verify pending state isolation
    inv_a = diary.get_pending_inventory(user_id=301)
    inv_b = diary.get_pending_inventory(user_id=302)

    assert inv_a is not None and len(inv_a.items) == 2
    assert inv_b is not None and len(inv_b.items) == 1

    # User A confirms, User B cancels
    save_a1 = diary.confirm_pending_inventory(user_id=301)
    save_a2 = diary.confirm_pending_meal(user_id=301)
    assert save_a2.status == "saved"

    cancel_b = diary.clear_pending_state(user_id=302)
    assert cancel_b is True

    # User B has no pending state
    assert diary.get_pending_inventory(user_id=302) is None
    # User A has record in diary
    summary_a = diary.get_summary(user_id=301)
    assert summary_a is not None
    assert summary_a["calories_kcal"] > 0
