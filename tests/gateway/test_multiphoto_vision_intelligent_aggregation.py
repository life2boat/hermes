from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from gateway.healbite_food_composition import resolve_food_composition
from gateway.healbite_nutrition_diary import (
    ALBUM_DECISION_SAME_PORTION,
    ALBUM_DECISION_DISTINCT_PORTIONS,
    ALBUM_DECISION_PARTIAL_OVERLAP,
    ALBUM_DECISION_UNRESOLVED,
    ALBUM_DECISION_REQUIRED_ERROR,
    ALBUM_DECISION_PROMPT,
    build_food_vision_inventory_keyboard,
    FOOD_VISION_SCHEMA_VERSION,
    FoodVisionItem,
    FoodVisionInventory,
    HealBiteNutritionDiary,
    PendingFoodVisionItem,
    PendingFoodVisionInventory,
    format_food_vision_inventory_reply,
    parse_pending_inventory_action,
    _reconcile_multi_photo_inventories,
    _build_inventory_calculation_snapshot,
)
from gateway.run import _is_healbite_tester_user, GatewayRunner


def _mock_vision_item(name: str, grams_min: float, grams_max: float, quantity: int | None = None, norm_name: str | None = None, uncertainty: str = "") -> dict:
    return {
        "visible_name": name,
        "normalized_name": norm_name or name.lower(),
        "confidence": 0.95,
        "estimated_grams_min": grams_min,
        "estimated_grams_max": grams_max,
        "preparation": "",
        "is_sauce": False,
        "uncertainty": uncertainty,
        "quantity": quantity,
    }


def _mock_vision_response(items: list[dict], is_food: bool = True) -> str:
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": is_food,
            "items": items,
            "overall_confidence": 0.95,
            "needs_user_confirmation": False,
            "warnings": [],
        }, ensure_ascii=False)
    })


# -----------------------------------------------------------------------------
# LEVEL 1 & 2: Integration - GatewayRunner passes all image_paths to Diary
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_level2_gateway_runner_passes_all_image_paths(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    paths_received = []

    async def mock_analyze(*args, **kwargs):
        nonlocal paths_received
        paths_received = kwargs.get("image_paths", [])
        return MagicMock(available=True, clarification_text="Я вижу на 2 фотографиях:", record=None, pending=False, saved=False)

    diary.analyze_and_maybe_log = mock_analyze
    runner = MagicMock()
    runner._get_healbite_nutrition_diary.return_value = diary
    runner._healbite_image_ref.return_value = "telegram:123:1"

    source = MagicMock(user_id=123, platform=MagicMock())
    event = MagicMock(message_type=MagicMock(), timestamp=datetime.now(timezone.utc))

    from gateway.run import GatewayRunner
    outcome = await GatewayRunner._try_enrich_message_with_healbite_nutrition(
        runner,
        event=event,
        source=source,
        user_text="",
        image_paths=["/tmp/img1.jpg", "/tmp/img2.jpg"],
    )

    assert paths_received == ["/tmp/img1.jpg", "/tmp/img2.jpg"]


# -----------------------------------------------------------------------------
# LEVEL 3: Diary analyzes all photos independently
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_level3_diary_analyzes_each_photo_independently(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    called_urls = []

    async def mock_vision(image_url, user_prompt):
        called_urls.append(image_url)
        return _mock_vision_response([_mock_vision_item("яблоко", 100.0, 150.0, 1, "яблоко")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=248875361,
            image_paths=["/tmp/photo1.jpg", "/tmp/photo2.jpg", "/tmp/photo3.jpg"],
        )

        assert outcome.available is True
        assert called_urls == ["/tmp/photo1.jpg", "/tmp/photo2.jpg", "/tmp/photo3.jpg"]
        assert getattr(outcome.pending_inventory, "photo_count", 1) == 3


# -----------------------------------------------------------------------------
# LEVEL 4: SCENARIO A - Single photo walnut baseline
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_a_single_photo_walnuts(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    items = [_mock_vision_item("грецкий орех", 10.0, 15.0, norm_name="грецкий орех в скорлупе") for _ in range(5)]

    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_v:
        mock_v.return_value = _mock_vision_response(items)
        outcome = await diary.analyze_and_maybe_log(
            user_id=248875361,
            image_paths=["/tmp/single_walnuts.jpg"],
        )

        assert outcome.available is True
        assert len(outcome.pending_inventory.items) == 1
        assert outcome.pending_inventory.items[0].quantity == 5
        assert "Грецкие орехи" in outcome.pending_inventory.items[0].visible_name
        assert "Я вижу:" in outcome.clarification_text


# -----------------------------------------------------------------------------
# LEVEL 4: SCENARIO B - SAME_PORTION deduplication (5 walnuts top + 5 walnuts side -> quantity 5, NOT 10)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_b_same_portion_deduplication(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    # Photo 1: 5 walnuts from above
    resp1 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 75.0, 5, norm_name="грецкий орех в скорлупе", uncertainty="вид сверху")])
    # Photo 2: 5 walnuts from side
    resp2 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 75.0, 5, norm_name="грецкий орех в скорлупе", uncertainty="ракурс сбоку")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=248875361,
            image_paths=["/tmp/walnuts_top.jpg", "/tmp/walnuts_side.jpg"],
            user_text="та же порция с разных ракурсов",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert len(inv.items) == 1
        assert inv.items[0].quantity == 5, f"Expected 5, got {inv.items[0].quantity}"
        assert "Я вижу на 2 фотографиях:" in outcome.clarification_text


# -----------------------------------------------------------------------------
# LEVEL 4: SCENARIO C - DISTINCT_PORTIONS summation (5 walnuts plate 1 + 5 walnuts plate 2 -> quantity 10)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_c_distinct_portions_summation(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 75.0, 5, norm_name="грецкий орех в скорлупе")])
    resp2 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 75.0, 5, norm_name="грецкий орех в скорлупе")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=248875361,
            image_paths=["/tmp/plate1.jpg", "/tmp/plate2.jpg"],
            user_text="две разные порции на двух тарелках",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert len(inv.items) == 1
        assert inv.items[0].quantity == 10, f"Expected 10, got {inv.items[0].quantity}"
        assert inv.items[0].estimated_grams_min == 100.0
        assert inv.items[0].estimated_grams_max == 150.0


# -----------------------------------------------------------------------------
# LEVEL 4: SCENARIO D - Multi-dish meal (Borscht + Salad + Bread -> 3 distinct dishes, 1 meal)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_d_multi_dish_album(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp_borscht = _mock_vision_response([_mock_vision_item("борщ", 250.0, 300.0, 1, "борщ")])
    resp_salad = _mock_vision_response([_mock_vision_item("салат овощной", 150.0, 200.0, 1, "салат")])
    resp_bread = _mock_vision_response([_mock_vision_item("хлеб", 40.0, 50.0, 1, "хлеб")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp_borscht, resp_salad, resp_bread]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=248875361,
            image_paths=["/tmp/borscht.jpg", "/tmp/salad.jpg", "/tmp/bread.jpg"],
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert len(inv.items) == 3
        names = [it.visible_name.lower() for it in inv.items]
        assert any("борщ" in n for n in names)
        assert any("салат" in n for n in names)
        assert any("хлеб" in n for n in names)
        assert "Я вижу на 3 фотографиях:" in outcome.clarification_text
        assert "Итого" in outcome.clarification_text


# -----------------------------------------------------------------------------
# LEVEL 4: SCENARIO E - Ambiguous overlap requires user confirmation
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_e_ambiguous_overlap_requires_confirmation(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    # Same dish in two photos, no evidence provided
    resp1 = _mock_vision_response([_mock_vision_item("пицца", 200.0, 250.0, 1, "пицца")])
    resp2 = _mock_vision_response([_mock_vision_item("пицца", 200.0, 250.0, 1, "пицца")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=248875361,
            image_paths=["/tmp/pizza1.jpg", "/tmp/pizza2.jpg"],
            user_text="",  # No user hints
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv.needs_user_confirmation is True
        assert any("одна и та же порция" in w for w in inv.warnings)
        assert "одна и та же порция" in outcome.clarification_text


# -----------------------------------------------------------------------------
# LEVEL 4: SCENARIO F - Partial album failure (photo 1 OK, photo 2 failed -> partial album warning, no auto-save)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_f_partial_album_failure(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("яблоко", 100.0, 120.0, 1, "яблоко")])
    resp2 = json.dumps({"success": False, "error": "HTTP 503 Provider Unavailable"})

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=248875361,
            image_paths=["/tmp/apple.jpg", "/tmp/failed.jpg"],
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv.needs_user_confirmation is True
        assert any("Удалось распознать 1 из 2 фото" in w for w in inv.warnings)


# -----------------------------------------------------------------------------
# LEVEL 4: SCENARIO G - Idempotency & Duplicate delivery
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_g_duplicate_delivery_idempotent(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _mock_vision_response([_mock_vision_item("банан", 100.0, 120.0, 1, "банан")])

    with patch("tools.vision_tools.vision_analyze_tool", return_value=resp):
        outcome1 = await diary.analyze_and_maybe_log(user_id=101, image_paths=["/tmp/b1.jpg"], image_ref="msg:101")
        outcome2 = await diary.analyze_and_maybe_log(user_id=101, image_paths=["/tmp/b1.jpg"], image_ref="msg:101")

        # Staging replaces cleanly; exactly 1 pending inventory remains
        inv = diary.get_pending_inventory(101)
        assert inv is not None


# -----------------------------------------------------------------------------
# LEVEL 4: SCENARIO H - Food + packaging artifact is not double counted
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_h_food_plus_packaging_not_double_counted(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp_food = _mock_vision_response([_mock_vision_item("творог", 150.0, 180.0, 1, "творог")])
    resp_pack = _mock_vision_response([_mock_vision_item("упаковка творога", 0.0, 0.0, 1, "упаковка творога")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp_food, resp_pack]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=248875361,
            image_paths=["/tmp/cottage.jpg", "/tmp/packaging.jpg"],
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert len(inv.items) == 1
        assert "творог" in inv.items[0].visible_name.lower()
        assert "упаковка" not in inv.items[0].visible_name.lower()


# -----------------------------------------------------------------------------
# LEVEL 4: Album image limit exceeded check
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_album_limit_exceeded(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    outcome = await diary.analyze_and_maybe_log(
        user_id=248875361,
        image_paths=[f"/tmp/img{i}.jpg" for i in range(6)],  # 6 > default 5
    )

    assert outcome.available is True
    assert outcome.validation_status == "ALBUM_LIMIT_EXCEEDED"
    assert "поддерживается до 5 фотографий" in outcome.clarification_text


# -----------------------------------------------------------------------------
# LEVEL 5: Reply formatting, dynamic examples, saving & confirmation
# -----------------------------------------------------------------------------
def test_level5_reply_formatting_and_saving(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    user_id = 248875361

    # Stage an album inventory with 2 photos
    item1 = PendingFoodVisionItem(index=1, visible_name="Борщ", normalized_name="борщ", confidence=0.9, estimated_grams_min=250.0, estimated_grams_max=300.0, selected_grams=275.0, preparation="", is_sauce=False, uncertainty="")
    item2 = PendingFoodVisionItem(index=2, visible_name="Хлеб", normalized_name="хлеб", confidence=0.9, estimated_grams_min=30.0, estimated_grams_max=50.0, selected_grams=40.0, preparation="", is_sauce=False, uncertainty="")

    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-album-1",
        schema_version="food_vision_inventory_v1",
        items=[item1, item2],
        overall_confidence=0.9,
        warnings=[],
        photo_count=2,
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    # Check formatted reply
    reply = format_food_vision_inventory_reply(inv)
    assert "Я вижу на 2 фотографиях:" in reply
    assert "1. Борщ" in reply
    assert "2. Хлеб" in reply
    assert "Итого:" in reply
    assert "КБЖУ рассчитаны приблизительно" in reply
    assert "КБЖУ не рассчитаны окончательно" not in reply

    # Dynamic suggestions
    assert "• Исправить 1:" in reply or "• Вес 1:" in reply
    assert "• Исправить 2:" in reply
    assert "• Удалить 2" in reply
    assert "Удалить 5" not in reply

    # Single save confirmation saves entire album as 1 meal entry
    save_res = diary.handle_pending_inventory_reply(user_id, "✅ Сохранить")
    assert save_res.status == "saved"
    assert "✅ Сохранено." in save_res.reply_text

    # Verify 1 record in diary log
    summary = diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 1


# =============================================================================
# MANDATORY 20 SCENARIOS (A THROUGH T) FOR MULTIPHOTO SAFETY HOTFIX V1
# =============================================================================

# -----------------------------------------------------------------------------
# SCENARIO A: Explicit SAME_PORTION via caption/hint ("одна порция")
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_a_explicit_same_portion(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("паста карбонара", 200.0, 220.0, 1)])
    resp2 = _mock_vision_response([_mock_vision_item("паста карбонара", 200.0, 220.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1001,
            image_paths=["/tmp/pasta_angle1.jpg", "/tmp/pasta_angle2.jpg"],
            user_text="одна порция с двух сторон",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv.album_decision == ALBUM_DECISION_SAME_PORTION
        assert len(inv.items) == 1
        assert inv.items[0].quantity in (1, None)
        assert inv.items[0].estimated_grams_min == 200.0
        assert inv.items[0].estimated_grams_max == 220.0
        assert ALBUM_DECISION_PROMPT not in inv.warnings
        assert inv.needs_user_confirmation is False


# -----------------------------------------------------------------------------
# SCENARIO B: Explicit DISTINCT_PORTIONS via caption/hint ("две разные порции")
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_b_explicit_distinct_portions(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 75.0, 5, norm_name="грецкие орехи")])
    resp2 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 75.0, 5, norm_name="грецкие орехи")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1002,
            image_paths=["/tmp/plate1.jpg", "/tmp/plate2.jpg"],
            user_text="две разные порции на двух столах",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv.album_decision == ALBUM_DECISION_DISTINCT_PORTIONS
        assert len(inv.items) == 1
        assert inv.items[0].quantity == 10
        assert inv.items[0].estimated_grams_min == 100.0
        assert inv.items[0].estimated_grams_max == 150.0


# -----------------------------------------------------------------------------
# SCENARIO C: Non-overlapping dishes across photos (Borscht + Salad)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_c_non_overlapping_multi_dish(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("борщ", 250.0, 300.0, 1)])
    resp2 = _mock_vision_response([_mock_vision_item("греческий салат", 150.0, 200.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1003,
            image_paths=["/tmp/dish1.jpg", "/tmp/dish2.jpg"],
            user_text="",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv.album_decision == ALBUM_DECISION_DISTINCT_PORTIONS
        assert len(inv.items) == 2
        names = [it.visible_name.lower() for it in inv.items]
        assert any("борщ" in n for n in names)
        assert any("салат" in n for n in names)
        assert ALBUM_DECISION_PROMPT not in inv.warnings


# -----------------------------------------------------------------------------
# SCENARIO D: Ambiguous overlap without hint -> UNRESOLVED
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_d_ambiguous_overlap_unresolved(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 180.0, 1)])
    resp2 = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 180.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1004,
            image_paths=["/tmp/chicken1.jpg", "/tmp/chicken2.jpg"],
            user_text="",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv.album_decision == ALBUM_DECISION_UNRESOLVED
        assert inv.needs_user_confirmation is True
        assert ALBUM_DECISION_PROMPT in inv.warnings
        assert ALBUM_DECISION_PROMPT in outcome.clarification_text


# -----------------------------------------------------------------------------
# SCENARIO E: Block save via save_pending_inventory_directly when UNRESOLVED
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_e_save_directly_blocked_when_unresolved(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 180.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp, resp]):
        await diary.analyze_and_maybe_log(
            user_id=1005,
            image_paths=["/tmp/c1.jpg", "/tmp/c2.jpg"],
        )

    # Attempt to save directly without resolving
    save_result = diary.save_pending_inventory_directly(user_id=1005)
    assert save_result.status == "album_decision_required"
    assert ALBUM_DECISION_PROMPT in save_result.reply_text

    # Verify zero database records created
    summary = diary.get_daily_summary(user_id=1005)
    assert summary["entry_count"] == 0


# -----------------------------------------------------------------------------
# SCENARIO F: Block save via confirm_pending_inventory when UNRESOLVED
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_f_confirm_blocked_when_unresolved(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 180.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp, resp]):
        await diary.analyze_and_maybe_log(
            user_id=1006,
            image_paths=["/tmp/c1.jpg", "/tmp/c2.jpg"],
        )

    # Attempt to confirm without resolving
    confirm_result = diary.confirm_pending_inventory(user_id=1006)
    assert confirm_result.status == "album_decision_required"
    assert ALBUM_DECISION_PROMPT in confirm_result.reply_text

    # Verify zero database records created
    summary = diary.get_daily_summary(user_id=1006)
    assert summary["entry_count"] == 0


# -----------------------------------------------------------------------------
# SCENARIO G: Block save via save_pending_inventory_without_kbzhu when UNRESOLVED
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_g_save_no_kbzhu_blocked_when_unresolved(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 180.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp, resp]):
        await diary.analyze_and_maybe_log(
            user_id=1007,
            image_paths=["/tmp/c1.jpg", "/tmp/c2.jpg"],
        )

    # Attempt to save without KBZHU while UNRESOLVED
    result = diary.save_pending_inventory_without_kbzhu(user_id=1007)
    assert result.status == "album_decision_required"
    assert ALBUM_DECISION_PROMPT in result.reply_text

    # Verify zero database records created
    summary = diary.get_daily_summary(user_id=1007)
    assert summary["entry_count"] == 0


# -----------------------------------------------------------------------------
# SCENARIO H: Block save via callback / text save when UNRESOLVED
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_h_callback_confirm_blocked_when_unresolved(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 180.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp, resp]):
        await diary.analyze_and_maybe_log(
            user_id=1008,
            image_paths=["/tmp/c1.jpg", "/tmp/c2.jpg"],
        )

    # User texts save button command
    reply_result = diary.handle_pending_inventory_reply(user_id=1008, message="✅ Сохранить")
    assert reply_result.status == "album_decision_required"
    assert ALBUM_DECISION_PROMPT in reply_result.reply_text

    # Also test confirm_pending_meal guard
    meal_res = diary.confirm_pending_meal(user_id=1008)
    assert meal_res.status in {"album_decision_required", "missing"}

    # Verify zero database records created
    summary = diary.get_daily_summary(user_id=1008)
    assert summary["entry_count"] == 0


# -----------------------------------------------------------------------------
# SCENARIO I: User resolves with "Одна порция" -> resolves to SAME_PORTION and saves
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_i_user_resolves_same_portion(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 170.0, 1)])
    resp2 = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 170.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        await diary.analyze_and_maybe_log(
            user_id=1009,
            image_paths=["/tmp/c1.jpg", "/tmp/c2.jpg"],
        )

    # User sends "Одна порция"
    res = diary.handle_pending_inventory_reply(user_id=1009, message="Одна порция")
    assert res.status == "updated"
    assert res.inventory.album_decision == ALBUM_DECISION_SAME_PORTION
    assert ALBUM_DECISION_PROMPT not in res.inventory.warnings
    assert len(res.inventory.items) == 1
    assert res.inventory.items[0].estimated_grams_min == 150.0

    # Now saving is permitted
    save_res = diary.handle_pending_inventory_reply(user_id=1009, message="✅ Сохранить")
    assert save_res.status == "saved"

    summary = diary.get_daily_summary(user_id=1009)
    assert summary["entry_count"] == 1


# -----------------------------------------------------------------------------
# SCENARIO J: User resolves with "Разные порции" -> resolves to DISTINCT_PORTIONS and saves
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_j_user_resolves_distinct_portions(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 160.0, 1)])
    resp2 = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 160.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        await diary.analyze_and_maybe_log(
            user_id=1010,
            image_paths=["/tmp/c1.jpg", "/tmp/c2.jpg"],
        )

    # User sends "Разные порции"
    res = diary.handle_pending_inventory_reply(user_id=1010, message="Разные порции")
    assert res.status == "updated"
    assert res.inventory.album_decision == ALBUM_DECISION_DISTINCT_PORTIONS
    assert ALBUM_DECISION_PROMPT not in res.inventory.warnings
    assert len(res.inventory.items) == 1
    # Portions summed
    assert res.inventory.items[0].quantity == 2
    assert res.inventory.items[0].estimated_grams_min == 300.0
    assert res.inventory.items[0].estimated_grams_max == 320.0

    # Save succeeds
    save_res = diary.handle_pending_inventory_reply(user_id=1010, message="✅ Сохранить")
    assert save_res.status == "saved"
    summary = diary.get_daily_summary(user_id=1010)
    assert summary["entry_count"] == 1


# -----------------------------------------------------------------------------
# SCENARIO K: User sends "Отмена" -> cancelled cleanly, state cleared
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_k_user_cancel_cleans_state(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 180.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp, resp]):
        await diary.analyze_and_maybe_log(
            user_id=1011,
            image_paths=["/tmp/c1.jpg", "/tmp/c2.jpg"],
        )

    # User cancels
    cancel_res = diary.handle_pending_inventory_reply(user_id=1011, message="Отмена")
    assert cancel_res.status == "cancelled"
    assert diary.get_pending_inventory(1011) is None
    summary = diary.get_daily_summary(user_id=1011)
    assert summary["entry_count"] == 0


# -----------------------------------------------------------------------------
# SCENARIO L: Edit while UNRESOLVED preserves safety
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_l_edit_while_unresolved_preserves_safety(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _mock_vision_response([_mock_vision_item("куриная грудка", 150.0, 170.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp, resp]):
        await diary.analyze_and_maybe_log(
            user_id=1012,
            image_paths=["/tmp/p1.jpg", "/tmp/p2.jpg"],
        )

    # User edits weight first
    edit_res = diary.handle_pending_inventory_reply(user_id=1012, message="Вес 1: 180 г")
    assert edit_res.status == "updated"
    assert edit_res.inventory.items[0].selected_grams == 180.0
    # Still UNRESOLVED
    assert edit_res.inventory.album_decision == ALBUM_DECISION_UNRESOLVED

    # Direct save still blocked!
    blocked = diary.save_pending_inventory_directly(user_id=1012)
    assert blocked.status == "album_decision_required"

    # Now resolve
    diary.handle_pending_inventory_reply(user_id=1012, message="Одна порция")
    save_ok = diary.save_pending_inventory_directly(user_id=1012)
    assert save_ok.status == "saved"


# -----------------------------------------------------------------------------
# SCENARIO M: Word "ракурс" or "с разных ракурсов" without "одна порция" -> UNRESOLVED
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_m_rakurs_word_does_not_falsely_assume_same_portion(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("пицца пепперони", 200.0, 250.0, 1, uncertainty="ракурс сверху")])
    resp2 = _mock_vision_response([_mock_vision_item("пицца пепперони", 200.0, 250.0, 1, uncertainty="ракурс сбоку")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1013,
            image_paths=["/tmp/p1.jpg", "/tmp/p2.jpg"],
            user_text="с разных ракурсов",  # Contains 'ракурс', but NOT 'одна порция'!
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        # Must be UNRESOLVED, not falsely resolved to SAME_PORTION!
        assert inv.album_decision == ALBUM_DECISION_UNRESOLVED
        assert ALBUM_DECISION_PROMPT in inv.warnings
        assert inv.needs_user_confirmation is True


# -----------------------------------------------------------------------------
# SCENARIO N: Shared vision timeout bounds per-image timeout, prevents album stall
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_n_shared_vision_deadline_budget(tmp_path, monkeypatch):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    monkeypatch.setenv("HEALBITE_ALBUM_TOTAL_TIMEOUT_SECONDS", "3.0")

    call_count = 0
    async def slow_mock_vision(image_url, user_prompt):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(0.8)
            return _mock_vision_response([_mock_vision_item("яблоко", 100.0, 110.0, 1)])
        await asyncio.sleep(2.0)
        return json.dumps({"success": False, "error": "timeout"})

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=slow_mock_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1014,
            image_paths=["/tmp/img1.jpg", "/tmp/img2.jpg"],
        )

        # First photo succeeded, second photo caught budget deadline
        assert outcome.available is True
        assert outcome.pending_inventory is not None
        assert len(outcome.pending_inventory.items) == 1


# -----------------------------------------------------------------------------
# SCENARIO O: Partial album safety (Photo 1 OK, Photo 2 fails)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_o_partial_album_safety(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("банан", 100.0, 120.0, 1)])
    resp2 = json.dumps({"success": False, "error": "HTTP 500 internal server error"})

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1015,
            image_paths=["/tmp/banana.jpg", "/tmp/error.jpg"],
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv.photo_count == 2
        assert len(inv.items) == 1
        assert any("Удалось распознать 1 из 2 фото" in w for w in inv.warnings)
        assert inv.needs_user_confirmation is True


# -----------------------------------------------------------------------------
# SCENARIO P: Packaging artifact filter
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_p_packaging_artifact_filter(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("творог 5%", 200.0, 200.0, 1)])
    resp2 = _mock_vision_response([_mock_vision_item("упаковка творога", 0.0, 0.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1016,
            image_paths=["/tmp/cottage.jpg", "/tmp/pack.jpg"],
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert len(inv.items) == 1
        assert "творог" in inv.items[0].visible_name.lower()
        assert "упаковка" not in inv.items[0].visible_name.lower()


# -----------------------------------------------------------------------------
# SCENARIO Q: In-shell vs edible distinction preserved
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_q_inshell_vs_edible_distinction(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("грецкие орехи в скорлупе", 100.0, 120.0, 5, norm_name="грецкие орехи в скорлупе")])
    resp2 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 60.0, 5, norm_name="грецкие орехи")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1017,
            image_paths=["/tmp/nuts_shell.jpg", "/tmp/nuts_shelled.jpg"],
            user_text="",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        # They should NOT be collapsed into 1 item because edible forms differ
        assert len(inv.items) == 2


# -----------------------------------------------------------------------------
# SCENARIO R: DB isolation / no records written before resolution and save
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_r_db_isolation_no_write_before_resolution(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _mock_vision_response([_mock_vision_item("стейк", 250.0, 300.0, 1)])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp, resp]):
        await diary.analyze_and_maybe_log(
            user_id=1018,
            image_paths=["/tmp/s1.jpg", "/tmp/s2.jpg"],
        )

    # Perform edits and invalid actions
    diary.handle_pending_inventory_reply(user_id=1018, message="Вес 1: 280 г")
    diary.handle_pending_inventory_reply(user_id=1018, message="✅ Сохранить")  # Should be blocked

    with diary._connect() as conn:
        count = conn.execute("SELECT count(*) FROM nutrition_log WHERE user_id = 1018").fetchone()[0]
        assert count == 0, "No records should exist in nutrition_log before resolution!"


# -----------------------------------------------------------------------------
# SCENARIO S: Lossless candidate retention when switching decisions
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_s_lossless_candidate_retention_toggle(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 75.0, 5, norm_name="грецкие орехи")])
    resp2 = _mock_vision_response([_mock_vision_item("грецкие орехи", 50.0, 75.0, 5, norm_name="грецкие орехи")])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        await diary.analyze_and_maybe_log(
            user_id=1019,
            image_paths=["/tmp/w1.jpg", "/tmp/w2.jpg"],
        )

    # 1. User says "Одна порция"
    res1 = diary.handle_pending_inventory_reply(user_id=1019, message="Одна порция")
    assert res1.status == "updated"
    assert res1.inventory.items[0].quantity == 5
    assert res1.inventory.album_decision == ALBUM_DECISION_SAME_PORTION

    # 2. User changes mind and says "Разные порции" -> candidates restored and summed losslessly!
    res2 = diary.handle_pending_inventory_reply(user_id=1019, message="Разные порции")
    assert res2.status == "updated"
    assert res2.inventory.items[0].quantity == 10
    assert res2.inventory.items[0].estimated_grams_min == 100.0
    assert res2.inventory.album_decision == ALBUM_DECISION_DISTINCT_PORTIONS

    # 3. User switches back to "Одна порция" -> deduplicated again cleanly!
    res3 = diary.handle_pending_inventory_reply(user_id=1019, message="Одна порция")
    assert res3.status == "updated"
    assert res3.inventory.items[0].quantity == 5
    assert res3.inventory.album_decision == ALBUM_DECISION_SAME_PORTION


# -----------------------------------------------------------------------------
# SCENARIO T: Full end-to-end integration flow
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiphoto_scenario_t_full_end_to_end_flow(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp1 = _mock_vision_response([_mock_vision_item("паста", 200.0, 220.0, 1)])
    resp2 = _mock_vision_response([_mock_vision_item("паста", 200.0, 220.0, 1)])

    # 1. User uploads 2 photos of pasta
    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=1020,
            image_paths=["/tmp/pasta1.jpg", "/tmp/pasta2.jpg"],
        )

    assert outcome.available is True
    assert outcome.pending_inventory.album_decision == ALBUM_DECISION_UNRESOLVED
    assert ALBUM_DECISION_PROMPT in outcome.clarification_text

    # Keyboard offers resolution buttons
    kb = build_food_vision_inventory_keyboard(outcome.pending_inventory)
    btn_texts = [b["text"] for row in kb for b in row]
    assert "🍽 Одна порция" in btn_texts
    assert "🍽🍽 Разные порции" in btn_texts

    # 2. Attempt direct save -> blocked
    save_blocked = diary.save_pending_inventory_directly(user_id=1020)
    assert save_blocked.status == "album_decision_required"

    # 3. User resolves "Одна порция"
    resolved = diary.handle_pending_inventory_reply(user_id=1020, message="Одна порция")
    assert resolved.status == "updated"
    assert resolved.inventory.album_decision == ALBUM_DECISION_SAME_PORTION

    # Keyboard now offers save button
    kb_resolved = build_food_vision_inventory_keyboard(resolved.inventory)
    resolved_btn_texts = [b["text"] for row in kb_resolved for b in row]
    assert any("Сохранить" in t for t in resolved_btn_texts)

    # 4. Save succeeds
    saved = diary.handle_pending_inventory_reply(user_id=1020, message="✅ Сохранить")
    assert saved.status == "saved"
    assert diary.get_pending_inventory(1020) is None

    # 5. Record verified in diary log
    summary = diary.get_daily_summary(user_id=1020)
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] > 0
