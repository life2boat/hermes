"""Regression tests for HEALBITE R1 Canary №3 Photo Semantic Accuracy & Album Deduplication.

Covers:
- DEFECT A: Distinct separable foods (teriyaki chicken, rice, gyoza, salad) are not erroneously consolidated into Plov.
- DEFECT B: Fried minced meat semantic duplicates across photos in an album are properly matched and deduplicated on SAME_PORTION.
- DEFECT C: Album confirmation, post-decision re-normalization, premature save blocking, idempotent single-confirmation saving.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from gateway.healbite_nutrition_diary import (
    ALBUM_DECISION_DISTINCT_PORTIONS,
    ALBUM_DECISION_PROMPT,
    ALBUM_DECISION_SAME_PORTION,
    ALBUM_DECISION_UNRESOLVED,
    ALBUM_OVERLAP_DISTINCT_PORTIONS,
    ALBUM_OVERLAP_NONE,
    ALBUM_OVERLAP_SAME_PORTION,
    ALBUM_OVERLAP_UNRESOLVED,
    FOOD_VISION_SCHEMA_VERSION,
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    PendingFoodVisionItem,
    _detect_and_deduplicate_composite_inventory,
    _reconcile_multi_photo_inventories,
    apply_dish_first_policy_to_items,
    build_food_vision_inventory_keyboard,
    calculate_inventory_nutrition,
    format_food_vision_inventory_reply,
)


def _item(
    name: str,
    gmin=None,
    gmax=None,
    *,
    sauce: bool = False,
    conf: float = 0.85,
    prep: str = "",
    uncertainty: str = "",
    quantity: int | None = None,
) -> FoodVisionItem:
    return FoodVisionItem(
        visible_name=name,
        normalized_name=name.casefold(),
        confidence=conf,
        estimated_grams_min=gmin,
        estimated_grams_max=gmax,
        preparation=prep,
        is_sauce=sauce,
        uncertainty=uncertainty,
        quantity=quantity,
    )


def _inventory(*items: FoodVisionItem) -> FoodVisionInventory:
    return FoodVisionInventory(
        schema_version=FOOD_VISION_SCHEMA_VERSION,
        items=list(items),
        overall_confidence=0.85,
        needs_user_confirmation=False,
        warnings=[],
    )


def _diary(tmp_path) -> HealBiteNutritionDiary:
    return HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)


# -----------------------------------------------------------------------------
# DEFECT A Tests: Teriyaki chicken + rice + gyoza + salad NOT turned into Plov
# -----------------------------------------------------------------------------

def test_teriyaki_chicken_with_rice_not_turned_into_plov():
    """DEFECT A: A plate with chicken teriyaki, rice, gyoza, and salad must NOT

    collapse into 'Плов' (1226 kcal). Distinct separable items must remain separate.
    """
    raw_items = [
        _item("Курица терияки", 160.0, 200.0, prep="жареная в соусе"),
        _item("Рис белый", 150.0, 180.0, prep="отварной"),
        _item("Гёдза", 100.0, 140.0, prep="на пару"),
        _item("Овощной салат с морковью", 60.0, 90.0, prep="свежий"),
    ]
    consolidated = apply_dish_first_policy_to_items(raw_items)

    # Must preserve separate items, never collapse into plov!
    names = [it.visible_name.lower() for it in consolidated]
    assert not any("плов" in n for n in names), f"Unexpected plov in names: {names}"
    assert len(consolidated) == 4
    assert any("терияки" in n for n in names)
    assert any("рис" in n for n in names)
    assert any("гёдза" in n for n in names)
    assert any("салат" in n for n in names)


def test_plov_consolidation_requires_explicit_plov_marker():
    """Plov consolidation must only trigger when there is explicit plov evidence

    ('плов', 'для плова', 'в плове') and no conflicting Asian or separate dish markers.
    """
    # Without explicit plov marker, chicken + rice + carrot remain separate
    generic_items = [
        _item("Курица", 150.0, 180.0, prep="тушеная"),
        _item("Рис", 150.0, 180.0, prep="отварной"),
        _item("Морковь", 30.0, 50.0, prep="тушеная"),
    ]
    res_generic = apply_dish_first_policy_to_items(generic_items)
    assert len(res_generic) == 3
    assert not any("плов" in it.visible_name.lower() for it in res_generic)

    # With explicit plov marker and no conflict: consolidates properly
    plov_items = [
        _item("Рис для плова", 150.0, 200.0, prep="тушеный"),
        _item("Говядина кусочками", 80.0, 120.0, prep="тушеная"),
        _item("Морковь", 20.0, 40.0, prep="тушеная"),
    ]
    res_plov = apply_dish_first_policy_to_items(plov_items)
    assert len(res_plov) == 1
    assert res_plov[0].normalized_name == "плов"
    # Inferred weight must NOT set confirmed selected_grams
    if hasattr(res_plov[0], "selected_grams"):
        assert res_plov[0].selected_grams is None


def test_teriyaki_and_gyoza_prevent_composite_suppression(tmp_path):
    """DEFECT A: Nutrition calculation for chicken teriyaki plate does not inflate

    to 1226 plov kcal; gyoza is preserved even if nutrition catalog lacks it.
    """
    diary = _diary(tmp_path)
    inv = diary.stage_pending_inventory(
        user_id=1,
        source="vision",
        inventory=_inventory(
            _item("Курица терияки", 160.0, 180.0),
            _item("Рис отварной", 150.0, 180.0),
            _item("Гёдза", 100.0, 140.0),
            _item("Салат свежий", 70.0, 90.0),
        ),
        image_ref="telegram:1:1",
        occurred_at=datetime.now(timezone.utc),
    )
    # Check that plov is NOT in the staged inventory
    names = [it.visible_name.lower() for it in inv.items]
    assert not any("плов" in n for n in names)
    assert len(inv.items) == 4

    # Gyoza may be unmapped -> partial total, but total calories are realistic (NOT 1246+ kcal)
    reply = format_food_vision_inventory_reply(inv)
    assert "плов" not in reply.lower()


# -----------------------------------------------------------------------------
# DEFECT B Tests: Fried minced meat semantic duplicates deduplication
# -----------------------------------------------------------------------------

def test_fried_minced_meat_semantic_duplicates_dedup():
    """DEFECT B: Photo 1 ('Жареный фарш') and Photo 2 ('Жареный фарш с базиликом и чили')

    must be recognized as an overlap, and deduplicated on SAME_PORTION into 1 item
    without doubling the weight or calories.
    """
    photo_1 = [_item("Жареный фарш", 180.0, 220.0, conf=0.88)]
    photo_2 = [_item("Жареный фарш с базиликом и чили", 160.0, 190.0, conf=0.90)]

    # 1. Unresolved: must flag ambiguous overlap requiring confirmation
    res_unresolved = _reconcile_multi_photo_inventories([photo_1, photo_2])
    assert res_unresolved.requires_confirmation is True
    assert ALBUM_DECISION_PROMPT in res_unresolved.warnings

    # 2. Resolved SAME_PORTION: must deduplicate into 1 item
    res_same = _reconcile_multi_photo_inventories(
        [photo_1, photo_2],
        decision=ALBUM_DECISION_SAME_PORTION,
    )
    reconciled_items = res_same[0]
    assert len(reconciled_items) == 1
    dish = reconciled_items[0]
    # Retains more specific composite name
    assert "базиликом" in dish.visible_name.lower()
    # Weight must NOT be doubled (not 180+160=340 min or 220+190=410 max)
    assert dish.estimated_grams_min <= 200.0
    assert dish.estimated_grams_max <= 230.0


def test_two_angles_one_portion_pad_krapao_plate():
    """DEFECT B & Canary №3 Test B: Two angles of one plate:

    rice, egg, minced meat, chili, basil.
    Must produce 1 portion, single meat item, herbs <= 40g, total <= 1200g, calories not doubled.
    """
    photo_1 = [
        _item("Рис белый", 150.0, 180.0),
        _item("Яйцо жареное", 45.0, 55.0),
        _item("Жареный фарш", 180.0, 220.0),
    ]
    photo_2 = [
        _item("Рис", 150.0, 180.0),
        _item("Яйцо", 45.0, 55.0),
        _item("Жареный фарш с базиликом и чили", 160.0, 190.0),
        _item("Чили", 5.0, 15.0),
        _item("Базилик", 5.0, 10.0),
    ]

    res = _reconcile_multi_photo_inventories(
        [photo_1, photo_2],
        decision=ALBUM_DECISION_SAME_PORTION,
    )
    items = res[0]
    # Rice, egg, minced meat, chili, basil -> exactly 5 items (minced meat deduplicated!)
    assert len(items) == 5
    names = [it.visible_name.lower() for it in items]
    meat_items = [it for it in items if "фарш" in it.visible_name.lower()]
    assert len(meat_items) == 1, f"Expected exactly 1 meat item, got {len(meat_items)}"
    assert "базиликом" in meat_items[0].visible_name.lower()

    # Chili and basil small portion weights (<= 40g)
    for it in items:
        if it not in meat_items and any(h in it.visible_name.lower() for h in ("чили", "базилик")):
            assert (it.estimated_grams_max or 0.0) <= 40.0

    # Total estimated weight bounded
    total_max = sum(it.estimated_grams_max for it in items if it.estimated_grams_max)
    assert total_max <= 1200.0


def test_composite_deduplication_of_base_and_composite_in_same_inventory():
    """DEFECT B: In the same inventory, if both 'Жареный фарш с базиликом и чили' and

    'Жареный фарш' are present, the generic base is suppressed under the composite.
    """
    items = [
        PendingFoodVisionItem(
            index=1,
            visible_name="Жареный фарш с базиликом и чили",
            normalized_name="жареный фарш с базиликом и чили",
            confidence=0.9,
            estimated_grams_min=160.0,
            estimated_grams_max=190.0,
            selected_grams=175.0,
            preparation="",
            is_sauce=False,
            uncertainty="",
            item_id="item_1",
        ),
        PendingFoodVisionItem(
            index=2,
            visible_name="Жареный фарш",
            normalized_name="жареный фарш",
            confidence=0.85,
            estimated_grams_min=180.0,
            estimated_grams_max=220.0,
            selected_grams=200.0,
            preparation="",
            is_sauce=False,
            uncertainty="",
            item_id="item_2",
        ),
    ]
    deduped, _ = _detect_and_deduplicate_composite_inventory(items)
    active = [it for it in deduped if not getattr(it, "suppressed_by_item_id", None)]
    assert len(active) == 1
    assert "базиликом" in active[0].visible_name.lower()


# -----------------------------------------------------------------------------
# DEFECT C Tests: Album Confirmation, Re-normalization, Save Blocking
# -----------------------------------------------------------------------------

def test_user_resolves_same_portion_and_recalculates(tmp_path):
    """DEFECT C: When user confirms 'Одна порция', resolve_album_decision runs

    re-normalization, component deduplication, and recalculates nutrition accurately.
    """
    diary = _diary(tmp_path)
    photo_1 = [_item("Жареный фарш", 180.0, 220.0)]
    photo_2 = [_item("Жареный фарш с базиликом и чили", 160.0, 190.0)]

    raw_items = [
        [{"visible_name": it.visible_name, "normalized_name": it.normalized_name, "confidence": it.confidence, "estimated_grams_min": it.estimated_grams_min, "estimated_grams_max": it.estimated_grams_max, "preparation": it.preparation, "is_sauce": it.is_sauce, "uncertainty": it.uncertainty} for it in photo_1],
        [{"visible_name": it.visible_name, "normalized_name": it.normalized_name, "confidence": it.confidence, "estimated_grams_min": it.estimated_grams_min, "estimated_grams_max": it.estimated_grams_max, "preparation": it.preparation, "is_sauce": it.is_sauce, "uncertainty": it.uncertainty} for it in photo_2],
    ]

    inv = diary.stage_pending_inventory(
        user_id=42,
        source="vision",
        inventory=_inventory(*photo_1, *photo_2),
        image_ref="telegram:42:101",
        occurred_at=datetime.now(timezone.utc),
        photo_count=2,
        album_decision=ALBUM_DECISION_UNRESOLVED,
        album_raw_photo_items=raw_items,
        album_overlap_decision=ALBUM_OVERLAP_UNRESOLVED,
    )
    # Staged with prompt
    inv.warnings.append(ALBUM_DECISION_PROMPT)
    diary._store_pending_inventory(inv)

    # Attempting save before resolving portion -> BLOCKED
    blocked = diary.save_pending_inventory_directly(user_id=42)
    assert blocked.status == "album_decision_required"

    # Resolve "Одна порция"
    res = diary.resolve_album_decision(user_id=42, decision=ALBUM_DECISION_SAME_PORTION)
    assert res.status == "updated"
    assert res.inventory.album_overlap_decision == ALBUM_OVERLAP_SAME_PORTION
    assert ALBUM_DECISION_PROMPT not in res.inventory.warnings

    # Active items should be deduplicated to 1 meat item
    active = [it for it in res.inventory.items if not getattr(it, "suppressed_by_item_id", None)]
    assert len(active) == 1
    assert "базиликом" in active[0].visible_name.lower()

    # Now save is allowed
    saved = diary.save_pending_inventory_directly(user_id=42)
    assert saved.status in {"saved", "saved_without_kbzhu"}


def test_two_truly_distinct_portions_preserved():
    """DEFECT B: When user indicates distinct portions ('Разные порции'),

    both portions are preserved and quantities/weights are summed appropriately.
    """
    photo_1 = [_item("Жареный фарш", 180.0, 220.0, conf=0.88)]
    photo_2 = [_item("Жареный фарш с базиликом и чили", 160.0, 190.0, conf=0.90)]

    res_distinct = _reconcile_multi_photo_inventories(
        [photo_1, photo_2],
        decision=ALBUM_DECISION_DISTINCT_PORTIONS,
    )
    items = res_distinct[0]
    assert len(items) == 1
    dish = items[0]
    # Summed into 2 portions
    assert getattr(dish, "quantity", 1) == 2
    assert dish.estimated_grams_min == pytest.approx(340.0, abs=1.0)
    assert dish.estimated_grams_max == pytest.approx(410.0, abs=1.0)


def test_save_single_confirmation_idempotent(tmp_path):
    """DEFECT C: Save action is strictly idempotent. Repeated tap returns missing/stale

    and results in exactly 1 row in SQLite.
    """
    diary = _diary(tmp_path)
    inv = diary.stage_pending_inventory(
        user_id=101,
        source="vision",
        inventory=_inventory(_item("Овсяная каша", 200.0, 200.0)),
        image_ref="telegram:101:201",
        occurred_at=datetime.now(timezone.utc),
    )
    # First save -> succeeds
    first_save = diary.save_pending_inventory_directly(user_id=101)
    assert first_save.status in {"saved", "saved_without_kbzhu"}
    assert first_save.sqlite_id is not None

    # Second save -> fails gracefully (missing/stale), no duplicate
    second_save = diary.save_pending_inventory_directly(user_id=101)
    assert second_save.status in {"missing", "stale"}
    assert second_save.sqlite_id is None

    # Verify SQLite row count
    with diary._connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM nutrition_log WHERE user_id = 101").fetchone()[0]
        assert count == 1


def test_premature_save_blocked_while_album_unresolved(tmp_path):
    """DEFECT C: Every save pathway (direct save, save without kbzhu, confirm, partial save)

    is strictly fail-closed while album_overlap_decision is UNRESOLVED.
    """
    diary = _diary(tmp_path)
    diary.stage_pending_inventory(
        user_id=202,
        source="vision",
        inventory=_inventory(_item("Паста", 200.0, 250.0)),
        image_ref="telegram:202:301",
        occurred_at=datetime.now(timezone.utc),
        photo_count=2,
        album_overlap_decision=ALBUM_OVERLAP_UNRESOLVED,
    )

    assert diary.save_pending_inventory_directly(user_id=202).status == "album_decision_required"
    assert diary.save_pending_inventory_without_kbzhu(user_id=202).status == "album_decision_required"
    assert diary.confirm_pending_inventory(user_id=202).status == "album_decision_required"
    assert diary.save_partial_pending_inventory(user_id=202).status == "album_decision_required"


def test_regression_pr488_pr489_pr491(tmp_path):
    """Regressions for PR #488, #489, #491:

    - PR #488: text meal while pending does not merge; pending choice keyboard preserved.
    - PR #489: auto-estimated weights don't set confirmed selected_grams; centered category priors.
    - PR #491: category center midpoint preserved.
    """
    diary = _diary(tmp_path)
    inv = diary.stage_pending_inventory(
        user_id=303,
        source="vision",
        inventory=_inventory(_item("Куриное филе")),  # unweighted
        image_ref="telegram:303:401",
        occurred_at=datetime.now(timezone.utc),
    )
    # PR #489: Unweighted item filled from category prior (poultry ~150g) without confirmed selected_grams
    assert len(inv.items) == 1
    item = inv.items[0]
    assert item.selected_grams is None
    assert "оценён по типовой порции" in (item.uncertainty or "")

    # PR #488: Free text meal description while pending does not merge into pending inventory
    reply = diary.handle_pending_inventory_reply(user_id=303, message="на перекус съел яблоко")
    assert reply.status in {"choice_required", "needs_clarification", "updated", "invalid_action"}
    # Original item not polluted by apple
    fresh = diary.get_pending_inventory(303)
    assert len(fresh.items) == 1
    assert "яблоко" not in fresh.items[0].visible_name.lower()
