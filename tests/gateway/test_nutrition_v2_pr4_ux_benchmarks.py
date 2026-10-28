"""
tests.gateway.test_nutrition_v2_pr4_ux_benchmarks — PR-4 Vision, Telegram UX and Benchmark Harnesses.

Validates:
1. Telegram UX formatting and vision inventory helper (calculate_food_vision_meal_nutrition).
2. Weight labels distinguishing calculated midpoint (~150-200g) from confirmed weight (180g).
3. Incomplete diary totals labeled as "Итого (частично)".
4. One-action inline buttons (Save, Edit, Cancel).
5. Tier 1 Offline Golden Benchmark suite (>= 200 test cases, 100% pass rate).
6. Tier 2 Real-Photo Benchmark manifest & evaluation harness (>= 200 items, calibration/held-out split,
   zero split leakage, fail-closed live provider request blocking).
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest

from gateway.healbite_nutrition_diary import (
    FoodVisionInventory,
    FoodVisionItem,
    PendingFoodVisionInventory,
    PendingFoodVisionItem,
    build_food_vision_inventory_keyboard,
    calculate_food_vision_meal_nutrition,
    format_food_vision_inventory_reply,
)
from gateway.nutrition.contracts import RecipeMassConstraint, SharedVisualScaleUncertainty
from scripts.evaluate_photo_benchmark import run_photo_benchmark
from tests.benchmarks.golden_kbzhu_v1.runner import run_golden_benchmark


def _make_pending_inventory(
    user_id: int = 1001,
    inv_id: str = "inv-pr4-001",
    items: list[PendingFoodVisionItem] | None = None,
) -> PendingFoodVisionInventory:
    if items is None:
        items = [
            PendingFoodVisionItem(
                index=1,
                visible_name="Куриная грудка отварная",
                normalized_name="куриная грудка отварная",
                confidence=0.92,
                estimated_grams_min=120.0,
                estimated_grams_max=160.0,
                selected_grams=140.0,
                preparation="boiled",
                is_sauce=False,
                uncertainty="",
                user_modified=False,
            ),
            PendingFoodVisionItem(
                index=2,
                visible_name="Гречка отварная",
                normalized_name="гречка отварная",
                confidence=0.95,
                estimated_grams_min=140.0,
                estimated_grams_max=180.0,
                selected_grams=160.0,
                preparation="boiled",
                is_sauce=False,
                uncertainty="",
                user_modified=True,
            ),
        ]
    now = datetime.now(timezone.utc)
    return PendingFoodVisionInventory(
        kind="pending_inventory",
        state="pending",
        inventory_id=inv_id,
        schema_version="food_vision_inventory_v1",
        user_id=user_id,
        source="vision",
        items=items,
        overall_confidence=0.93,
        needs_user_confirmation=True,
        warnings=[],
        image_ref=None,
        occurred_at=now,
        created_at=now,
        expires_at=now,
    )


# -----------------------------------------------------------------------------
# 1. Telegram UX & Vision Inventory Helper
# -----------------------------------------------------------------------------

def test_calculate_food_vision_meal_nutrition():
    inv = _make_pending_inventory()
    meal_est = calculate_food_vision_meal_nutrition(inv, meal_name="Обед атлета")

    assert meal_est.meal_name == "Обед атлета"
    assert len(meal_est.components) == 2

    comp1 = meal_est.components[0]
    assert comp1.name == "Куриная грудка отварная"
    # User did not modify comp1: marked as approximate
    assert comp1.mass_bounds.is_approximate_plausible_range is True
    assert comp1.mass_bounds.is_user_confirmed is False
    assert comp1.intervals.calories_kcal is not None
    assert comp1.intervals.calories_kcal.min_value > 0

    comp2 = meal_est.components[1]
    assert comp2.name == "Гречневая каша"
    # User confirmed comp2: exact point mass, not approximate
    assert comp2.mass_bounds.is_user_confirmed is True
    assert comp2.mass_bounds.is_approximate_plausible_range is False
    assert comp2.mass_bounds.grams_min == comp2.mass_bounds.grams_max == 160.0

    # Meal total intervals
    assert meal_est.total_intervals.calories_kcal is not None
    assert meal_est.total_intervals.calories_kcal.center_value > 300.0


def test_calculate_food_vision_meal_nutrition_with_constraints_and_scale():
    inv = _make_pending_inventory()
    scale = SharedVisualScaleUncertainty(0.90, 1.00, 1.10)
    constraint = RecipeMassConstraint(
        total_dish_mass_min_g=250.0,
        total_dish_mass_max_g=350.0,
    )
    meal_est = calculate_food_vision_meal_nutrition(
        inv,
        recipe_constraint=constraint,
        shared_scale=scale,
    )
    assert len(meal_est.components) == 2
    # Confirmed item (comp2) should not have been scaled
    assert meal_est.components[1].mass_bounds.is_user_confirmed is True
    assert meal_est.components[1].mass_bounds.grams_center == 160.0


def test_format_food_vision_reply_partial_totals_on_unmapped():
    mapped_item = PendingFoodVisionItem(
        index=1,
        visible_name="Куриная грудка отварная",
        normalized_name="куриная грудка отварная",
        confidence=0.9,
        estimated_grams_min=120.0,
        estimated_grams_max=160.0,
        selected_grams=140.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        user_modified=False,
    )
    unmapped_item = PendingFoodVisionItem(
        index=2,
        visible_name="Экзотический фрукт дуриан",
        normalized_name="экзотический фрукт дуриан",
        confidence=0.7,
        estimated_grams_min=100.0,
        estimated_grams_max=150.0,
        selected_grams=120.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        user_modified=False,
    )
    inv = _make_pending_inventory(items=[mapped_item, unmapped_item])
    reply = format_food_vision_inventory_reply(inv)

    # Incomplete totals must be explicitly labeled as "Итого (частично)"
    assert "Итого (частично):" in reply
    assert "Итого:" not in reply.replace("Итого (частично):", "")
    assert "⚠️ Для некоторых компонентов справочные данные не найдены." in reply
    assert "Можно исправить название или сохранить без КБЖУ." in reply


def test_format_food_vision_reply_complete_totals_when_all_mapped():
    inv = _make_pending_inventory()
    reply = format_food_vision_inventory_reply(inv)

    assert "Итого:" in reply
    assert "Итого (частично):" not in reply
    assert "⚠️ Для некоторых компонентов справочные данные не найдены." not in reply


def test_format_food_vision_reply_weight_labels():
    inv = _make_pending_inventory()
    reply = format_food_vision_inventory_reply(inv)

    # Item 1 is midpoint: must say "расчётный вес: ~140 г", NOT "вес подтверждён: 140 г"
    assert "расчётный вес: ~140 г" in reply
    assert "1. Куриная грудка отварная" in reply

    # Item 2 is user confirmed: must say "вес подтверждён: 160 г"
    assert "вес подтверждён: 160 г" in reply


def test_build_food_vision_inventory_keyboard_structure():
    inv = _make_pending_inventory(user_id=888, inv_id="inv-buttons-test")
    kb = build_food_vision_inventory_keyboard(inv)

    assert len(kb) == 2
    # Row 1: Save
    assert kb[0][0]["text"] == "✅ Сохранить"
    assert kb[0][0]["callback_data"] == "food_inv:save:888:inv-buttons-test"

    # Row 2: Edit and Cancel
    assert kb[1][0]["text"] in ("✏️ Исправить", "✏️ Уточнить состав")
    assert kb[1][0]["callback_data"] == "food_inv:edit:888:inv-buttons-test"
    assert kb[1][1]["text"] == "❌ Отмена"
    assert kb[1][1]["callback_data"] == "food_inv:cancel:888:inv-buttons-test"


# -----------------------------------------------------------------------------
# 2. Tier 1 — Offline Golden Benchmark Suite
# -----------------------------------------------------------------------------

def test_tier1_offline_golden_benchmark_execution():
    """
    Executes the full Tier 1 Golden Benchmark (>= 200 cases).
    Requires 100% pass rate without external requests.
    """
    cases_file = Path(__file__).resolve().parents[1] / "benchmarks" / "golden_kbzhu_v1" / "cases.jsonl"
    assert cases_file.exists(), f"Cases file missing: {cases_file}"

    report = run_golden_benchmark(cases_file)

    assert report.total_cases >= 200, f"Expected >= 200 cases, got {report.total_cases}"
    assert report.failed_cases == 0, f"Benchmark had {report.failed_cases} failures: {[r for r in report.results if not r.passed]}"
    assert report.passed_cases == report.total_cases
    assert report.status == "PASS"

    # Verify coverage across all required categories
    expected_categories = {
        "hfkb_lookup",
        "russian_synonyms",
        "composite_dishes",
        "portion_prior",
        "wide_weight_interval",
        "density_conversion",
        "recipe_yield",
        "shared_scale",
        "interval_aggregation",
        "missing_reference",
        "error_handling",
        "diary_persistence",
        "callback_protection",
    }
    present_categories = set(report.category_counts.keys())
    missing = expected_categories - present_categories
    assert not missing, f"Missing required categories in golden benchmark: {missing}"


# -----------------------------------------------------------------------------
# 3. Tier 2 — Real-Photo Benchmark Manifest & Evaluation Harness
# -----------------------------------------------------------------------------

def test_tier2_real_photo_benchmark_manifest_integrity():
    """
    Validates the Tier 2 real-photo benchmark manifest contract:
    - >= 200 items.
    - ~80 calibration split.
    - >= 120 held-out split.
    - Zero session leakage between splits.
    - Required metadata present.
    """
    manifest_file = Path(__file__).resolve().parents[1] / "benchmarks" / "photo_kbzhu_v1" / "manifest.json"
    assert manifest_file.exists(), f"Manifest file missing: {manifest_file}"

    report = run_photo_benchmark(manifest_file, split="all")

    assert report.total_items_in_manifest >= 200
    assert 70 <= report.calibration_count <= 90
    assert report.held_out_count >= 120
    assert report.session_leakage_detected is False
    assert report.live_provider_requests_used == 0

    # Offline algorithm evaluation metrics
    assert report.overall_metrics.dishes_f1 >= 0.90
    assert report.overall_metrics.mass_interval_coverage_pct >= 90.0
    assert report.overall_metrics.calorie_interval_coverage_pct >= 85.0

    # Acceptance statuses
    assert report.engineering_ready == "PASS"
    assert "PENDING" in report.real_photo_acceptance


def test_tier2_real_photo_benchmark_fail_closed_on_unauthorized_provider():
    """
    Verifies that requesting --execute-provider with max_calls > 0 halts fail-closed
    because LIVE_PROVIDER_REQUESTS_ALLOWED=0 in this phase.
    """
    manifest_file = Path(__file__).resolve().parents[1] / "benchmarks" / "photo_kbzhu_v1" / "manifest.json"
    report = run_photo_benchmark(
        manifest_file,
        execute_provider=True,
        max_calls=5,
    )
    assert report.engineering_ready == "FAIL"
    assert "BLOCKED" in report.real_photo_acceptance
    assert report.live_provider_requests_used == 0
