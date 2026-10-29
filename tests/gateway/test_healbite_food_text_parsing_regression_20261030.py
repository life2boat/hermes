"""
Regression tests for HealBite natural Russian food text parsing, multi-item clause splitting,
and duplicate Telegram response prevention.

TASK_ID: HEALBITE_FOOD_TEXT_PARSING_REGRESSION_20261030
Issue: Operator reported:
"На завтрак съел 2 варёных яйца, 100 г хлеба и выпил 200 мл молока."
- 1 egg (~50g) counted instead of 2 eggs (~100g).
- Bread and milk incorrectly merged into single item "хлеба и выпил 200 мл молока".
- Bread and milk nutrition not found.
- Partial meal total was only ~78 kcal.
- Duplicate identical bot response in Telegram.
"""

from __future__ import annotations

import asyncio
from unittest import mock
import pytest

from gateway.config import Platform
from gateway.healbite_nutrition_diary import (
    format_food_vision_inventory_reply,
    get_default_nutrition_diary,
)
from gateway.nutrition.text_parser import (
    is_full_food_description,
    parse_food_text_description,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_operator_reported_meal_parsing():
    """Verify that the operator phrase extracts 3 distinct items with proper quantities and weights."""
    text = "На завтрак съел 2 варёных яйца, 100 г хлеба и выпил 200 мл молока."
    parsed = parse_food_text_description(text)

    assert parsed is not None, "Failed to parse operator food description"
    assert parsed.dish_title == "Завтрак", f"Expected title 'Завтрак', got {parsed.dish_title!r}"
    assert parsed.has_food_intent is True
    assert parsed.is_structured is True
    assert is_full_food_description(parsed) is True, "Expected full food description bypassing vision"
    assert len(parsed.ingredients) == 3, f"Expected 3 ingredients, got {len(parsed.ingredients)}"

    # Item 1: 2 boiled eggs (~100g)
    egg = parsed.ingredients[0]
    assert egg.quantity == 2.0, f"Expected 2 eggs, got {egg.quantity}"
    assert egg.grams == 100.0, f"Expected 100g for 2 eggs, got {egg.grams}"
    assert egg.unit == "шт"
    assert egg.preparation == "отварной"
    assert "яйц" in egg.name.lower() or "яйц" in egg.normalized_name.lower()

    # Item 2: Bread (100g)
    bread = parsed.ingredients[1]
    assert bread.quantity == 1.0
    assert bread.grams == 100.0
    assert bread.unit == "грамм"
    assert "хлеб" in bread.name.lower() or "хлеб" in bread.normalized_name.lower()

    # Item 3: Milk (200ml)
    milk = parsed.ingredients[2]
    assert milk.quantity == 1.0
    assert milk.grams == 200.0
    assert milk.unit == "мл"
    assert "молок" in milk.name.lower() or "молок" in milk.normalized_name.lower()


def test_operator_reported_meal_nutrition_resolution():
    """Verify that all three products are independently resolved in the nutrition catalog with ~524 kcal total."""
    text = "На завтрак съел 2 варёных яйца, 100 г хлеба и выпил 200 мл молока."
    diary = get_default_nutrition_diary()

    pending = diary.stage_pending_inventory_from_text(user_id=12345001, text=text)
    assert pending is not None, "Failed to stage pending inventory from text"
    assert len(pending.items) == 3, f"Expected 3 pending items, got {len(pending.items)}"

    reply = format_food_vision_inventory_reply(pending)

    # Calculation snapshot must contain all 3 components with valid nutrition
    snapshot = pending.calculation_snapshot
    assert snapshot is not None, "Calculation snapshot was not generated"
    components = snapshot.get("components", [])
    assert len(components) == 3, f"Expected 3 components in snapshot, got {len(components)}"

    for comp in components:
        assert comp.get("nutrition_available") is True, f"Component {comp.get('name')} missing nutrition"
        assert (comp.get("calories_kcal") or 0) > 0, f"Component {comp.get('name')} has zero calories"

    total_cals = snapshot.get("calories_kcal", 0)
    assert 500 <= total_cals <= 560, f"Expected total meal calories ~524 kcal, got {total_cals}"

    total_prot = snapshot.get("protein_g", 0)
    assert total_prot >= 20.0, f"Expected >= 20g protein, got {total_prot}"

    total_carbs = snapshot.get("carbs_g", 0)
    assert total_carbs >= 40.0, f"Expected >= 40g carbs, got {total_carbs}"

    # Verify reply formatting
    assert "Варёные яйца" in reply or "Яйцо" in reply
    assert "Хлеб" in reply
    assert "Молоко" in reply
    assert "Завтрак" in reply
    assert "ккал" in reply


@pytest.mark.asyncio
async def test_duplicate_telegram_response_prevention():
    """Verify that handling a text_meal turn returns already_sent=True and dispatches exactly once."""
    runner = GatewayRunner()
    mock_adapter = mock.AsyncMock()
    mock_adapter.send = mock.AsyncMock()
    runner.adapters[Platform.TELEGRAM] = mock_adapter

    test_uid = 999111222
    diary = runner._get_healbite_nutrition_diary()
    diary.clear_pending_state(test_uid)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="test_chat_999",
        user_id=str(test_uid),
        chat_type="private",
    )
    event = mock.MagicMock()
    event.text = "На завтрак съел 2 варёных яйца, 100 г хлеба и выпил 200 мл молока."
    event.message_id = "msg_001"
    event.platform_update_id = "upd_001"
    event.source = source
    event.media = []

    try:
        res = await runner._maybe_handle_healbite_nutrition_diary_turn(
            message=event.text,
            history=[],
            source=source,
            session_id="test-session-id",
            event=event,
        )
        assert res is not None, "Expected diary turn handler to claim the turn"
        assert res.get("already_sent") is True, "already_sent must be True to prevent duplicate Telegram send"
        assert res.get("completed") is True

        # Give scheduled asyncio tasks a tick to execute
        await asyncio.sleep(0.05)

        # Exactly 1 send call with inline keyboard
        assert mock_adapter.send.call_count == 1, f"Expected exactly 1 adapter.send call, got {mock_adapter.send.call_count}"
        send_args, send_kwargs = mock_adapter.send.call_args
        assert send_args[0] == "test_chat_999"
        meta = send_kwargs.get("metadata", {})
        assert meta.get("parse_mode") == "HTML"
        assert meta.get("reply_markup") is not None, "Reply must attach inline keyboard"
    finally:
        diary.clear_pending_state(test_uid)


def test_russian_word_numerals_and_clause_connectors():
    """Test Russian numeral words, connective verbs, and multi-item sentence clauses."""
    # 1. Russian numeral words for pieces
    p1 = parse_food_text_description("два варёных яйца")
    assert p1 is not None and len(p1.ingredients) == 1
    assert p1.ingredients[0].quantity == 2.0
    assert p1.ingredients[0].grams == 100.0

    p2 = parse_food_text_description("2 банана и 3 яйца")
    assert p2 is not None and len(p2.ingredients) == 2
    assert p2.ingredients[0].quantity == 2.0
    assert p2.ingredients[0].grams == 240.0
    assert p2.ingredients[1].quantity == 3.0
    assert p2.ingredients[1].grams == 150.0

    p3 = parse_food_text_description("два банана и три яйца")
    assert p3 is not None and len(p3.ingredients) == 2
    assert p3.ingredients[0].quantity == 2.0
    assert p3.ingredients[0].grams == 240.0
    assert p3.ingredients[1].quantity == 3.0
    assert p3.ingredients[1].grams == 150.0

    # 2. Connective verbs without comma
    p4 = parse_food_text_description("съел 1 яблоко и выпил 250 мл кефира")
    assert p4 is not None and len(p4.ingredients) == 2
    assert p4.ingredients[0].quantity == 1.0
    assert p4.ingredients[0].grams == 150.0
    assert p4.ingredients[1].grams == 250.0
    assert p4.ingredients[1].unit == "мл"

    # 3. Preamble + compound items with household measure
    p5 = parse_food_text_description("на обед поел 200 г гречки, 150 г куриной грудки и выпил стакан воды")
    assert p5 is not None and len(p5.ingredients) == 3
    assert p5.dish_title == "Обед"
    assert p5.ingredients[0].grams == 200.0
    assert p5.ingredients[1].grams == 150.0
    assert p5.ingredients[2].grams == 200.0  # glass of water is 200g
    assert p5.ingredients[2].household_measure == "glass"


def test_cyrillic_yo_handling():
    """Verify that Russian letter 'ё' in foods and preparations matches identically to 'е'."""
    p_yo = parse_food_text_description("2 варёных яйца")
    p_e = parse_food_text_description("2 вареных яйца")

    assert p_yo is not None and p_e is not None
    assert p_yo.ingredients[0].quantity == p_e.ingredients[0].quantity == 2.0
    assert p_yo.ingredients[0].grams == p_e.ingredients[0].grams == 100.0
    assert p_yo.ingredients[0].preparation == p_e.ingredients[0].preparation == "отварной"

    p_baked = parse_food_text_description("куриная грудка запечённая 150 г")
    assert p_baked is not None
    assert p_baked.ingredients[0].grams == 150.0
    assert p_baked.ingredients[0].preparation == "запечённый"
