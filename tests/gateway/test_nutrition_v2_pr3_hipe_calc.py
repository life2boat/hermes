"""
tests.gateway.test_nutrition_v2_pr3_hipe_calc — Test suite for Nutrition Intelligence v2.2 PR-3.

Validates:
1. HIPE (HealBite Intelligent Portion Estimator) density tables and visual portion priors.
2. Visual anchor scale calibration and conservative widening when reference anchors are absent.
3. Component resolution with HFKB & legacy fallback.
4. Recipe mass constraints & cooking yield consistency solver.
5. Shared visual scale uncertainty propagation across plate components.
6. Full meal aggregation and Telegram range formatting.
"""

from __future__ import annotations

import pytest

from gateway.nutrition.contracts import (
    MassBounds,
    NutrientInterval,
    RecipeMassConstraint,
    SharedVisualScaleUncertainty,
    UncertaintyModelType,
)
from gateway.nutrition.engine import (
    apply_shared_visual_scale_uncertainty,
    calculate_meal_nutrition,
    format_meal_summary_text,
    format_nutrient_range,
    resolve_and_calculate_component,
    solve_recipe_mass_constraints,
)
from gateway.nutrition.hipe import (
    VisualAnchorType,
    estimate_portion_mass,
    get_anchor_spec,
    get_food_density,
    get_portion_prior,
)


# -----------------------------------------------------------------------------
# 1. HIPE Food Density and Portion Prior Lookups
# -----------------------------------------------------------------------------

def test_food_density_lookup_and_invariants():
    soup_density = get_food_density("борщ украинский")
    assert soup_density.category == "soup"
    assert 0.0 < soup_density.density_min <= soup_density.density_center <= soup_density.density_max
    assert soup_density.density_center == pytest.approx(1.00)

    porridge_density = get_food_density("гречневая каша")
    assert porridge_density.category == "porridge"
    assert 0.70 <= porridge_density.density_center <= 0.85

    bread_density = get_food_density("ржаной хлеб")
    assert bread_density.category == "bread"
    assert bread_density.density_center < 0.50  # bread is airy and light

    default_density = get_food_density("какой-то неизвестный продукт")
    assert default_density.category == "default"
    assert default_density.density_min <= default_density.density_center <= default_density.density_max


def test_portion_priors_lookup_and_invariants():
    soup_prior = get_portion_prior("суп куриный с лапшой")
    assert soup_prior.category == "soup"
    assert soup_prior.grams_min >= 150.0
    assert soup_prior.grams_max <= 500.0

    cutlet_prior = get_portion_prior("котлета домашняя")
    assert cutlet_prior.category == "meat_cutlet"
    assert cutlet_prior.grams_min <= 100.0 <= cutlet_prior.grams_max

    bread_prior = get_portion_prior("кусок черного хлеба")
    assert bread_prior.category == "bread_slice"
    assert bread_prior.grams_center == pytest.approx(35.0)

    default_prior = get_portion_prior("неведомое блюдо")
    assert default_prior.category == "default"
    assert default_prior.grams_min <= default_prior.grams_center <= default_prior.grams_max


# -----------------------------------------------------------------------------
# 2. HIPE Portion Mass Estimation
# -----------------------------------------------------------------------------

def test_hipe_estimate_portion_mass_user_confirmed():
    # User confirmed mass: exact point bounds, not approximate
    mass = estimate_portion_mass("гречка", user_confirmed_grams=180.0)
    assert mass.grams_min == pytest.approx(180.0)
    assert mass.grams_center == pytest.approx(180.0)
    assert mass.grams_max == pytest.approx(180.0)
    assert mass.is_user_confirmed is True
    assert mass.is_approximate_plausible_range is False
    assert mass.span == pytest.approx(0.0)


def test_hipe_estimate_portion_mass_visual_volume():
    # Estimating 300 cm³ of soup with a soup bowl anchor
    mass = estimate_portion_mass(
        "борщ",
        visual_volume_cm3=300.0,
        visual_anchor=VisualAnchorType.SOUP_BOWL,
    )
    assert mass.is_user_confirmed is False
    assert mass.is_approximate_plausible_range is True
    # ~300 cm³ * 1.0 g/cm³ = ~300 g center
    assert 280.0 <= mass.grams_center <= 320.0
    assert mass.grams_min < mass.grams_center < mass.grams_max


def test_hipe_estimate_portion_mass_anchor_widening_when_missing():
    # When anchor is NONE vs when a dinner plate is present
    mass_none = estimate_portion_mass("куриное филе", visual_anchor=VisualAnchorType.NONE)
    mass_plate = estimate_portion_mass("куриное филе", visual_anchor=VisualAnchorType.STANDARD_DINNER_PLATE)

    assert mass_none.is_approximate_plausible_range is True
    assert mass_plate.is_approximate_plausible_range is True
    # The span without an anchor must be strictly wider than with a calibrated anchor
    assert mass_none.span > mass_plate.span
    # Lower bound without anchor should be lower, upper bound should be higher
    assert mass_none.grams_min < mass_plate.grams_min
    assert mass_none.grams_max > mass_plate.grams_max


def test_hipe_count_multiplier():
    single = estimate_portion_mass("котлета", detected_count=1.0)
    double = estimate_portion_mass("котлета", detected_count=2.0)
    assert double.grams_center == pytest.approx(single.grams_center * 2.0, rel=1e-2)
    assert double.grams_min == pytest.approx(single.grams_min * 2.0, rel=1e-2)
    assert double.grams_max == pytest.approx(single.grams_max * 2.0, rel=1e-2)


# -----------------------------------------------------------------------------
# 3. Component Resolution & Nutrition Calculation
# -----------------------------------------------------------------------------

def test_component_resolution_known_food():
    mass = MassBounds(grams_min=120.0, grams_center=150.0, grams_max=180.0)
    comp = resolve_and_calculate_component("гречка", mass)

    assert comp.nutrition_available is True
    assert comp.uncertainty_model_type == UncertaintyModelType.CONSERVATIVE_INTERVAL
    assert comp.is_approximate_plausible_range is True
    assert comp.per_100g_calories_kcal is not None and comp.per_100g_calories_kcal > 0

    cal = comp.intervals.calories_kcal
    assert cal is not None
    assert 0.0 < cal.min_value < cal.center_value < cal.max_value
    # 150g should scale 100g calories by 1.5
    assert cal.center_value == pytest.approx(comp.per_100g_calories_kcal * 1.5, rel=1e-2)


def test_component_resolution_unresolved_food():
    mass = MassBounds(grams_min=80.0, grams_center=100.0, grams_max=120.0)
    comp = resolve_and_calculate_component("абсолютно_вымышленное_блюдо_xyz", mass)

    assert comp.nutrition_available is False
    assert comp.uncertainty_model_type == UncertaintyModelType.UNRESOLVED
    assert comp.is_approximate_plausible_range is True
    assert comp.intervals.is_empty() is True
    assert comp.candidate_suggestions is not None


# -----------------------------------------------------------------------------
# 4. Recipe Mass Constraints & Cooking Yield Solver
# -----------------------------------------------------------------------------

def test_recipe_mass_constraints_scaling_dish():
    # Stir fry recipe with chicken fillet and rice
    chicken_mass = MassBounds(grams_min=150.0, grams_center=200.0, grams_max=250.0)
    rice_mass = MassBounds(grams_min=150.0, grams_center=200.0, grams_max=250.0)
    sour_cream_mass = MassBounds(grams_min=20.0, grams_center=30.0, grams_max=40.0)

    comp_chicken = resolve_and_calculate_component("куриное филе", chicken_mass)
    comp_rice = resolve_and_calculate_component("рис", rice_mass)
    comp_sour_cream = resolve_and_calculate_component("сметана", sour_cream_mass)

    assert comp_chicken.nutrition_available is True
    assert comp_rice.nutrition_available is True

    # Raw chicken + rice = 400g raw center. Yield factor 0.8 -> implied cooked = 320g.
    # Total dish limit is max 240g cooked. Sour cream is external topping.
    constraint = RecipeMassConstraint(
        total_dish_mass_max_g=240.0,
        cooking_yield_factor=0.8,
        recipe_ingredient_names=frozenset({"куриное филе", "рис"}),
        external_ingredient_names=frozenset({"сметана"}),
    )

    scaled_components = solve_recipe_mass_constraints(
        [comp_chicken, comp_rice, comp_sour_cream],
        constraint,
    )

    # Implied cooked = 400 * 0.8 = 320g. Target max = 240g. Scale factor = 240 / 320 = 0.75
    # Chicken center: 200 * 0.75 = 150g
    # Rice center: 200 * 0.75 = 150g
    # Sour cream center: untouched (30g)
    assert scaled_components[0].mass_bounds.grams_center == pytest.approx(150.0, abs=0.5)
    assert scaled_components[1].mass_bounds.grams_center == pytest.approx(150.0, abs=0.5)
    assert scaled_components[2].mass_bounds.grams_center == pytest.approx(30.0, abs=0.1)

    # Calories should also be scaled by 0.75
    assert scaled_components[0].intervals.calories_kcal is not None
    assert scaled_components[0].intervals.calories_kcal.center_value == pytest.approx(
        comp_chicken.intervals.calories_kcal.center_value * 0.75,
        rel=1e-2,
    )


# -----------------------------------------------------------------------------
# 5. Shared Visual Scale Uncertainty
# -----------------------------------------------------------------------------

def test_shared_visual_scale_uncertainty_propagation():
    mass1 = MassBounds(grams_min=100.0, grams_center=150.0, grams_max=200.0, is_user_confirmed=False)
    mass2_confirmed = MassBounds(grams_min=100.0, grams_center=100.0, grams_max=100.0, is_user_confirmed=True)

    c1 = resolve_and_calculate_component("гречка", mass1)
    c2 = resolve_and_calculate_component("куриное филе", mass2_confirmed)

    shared_scale = SharedVisualScaleUncertainty(
        scale_factor_min=0.85,
        scale_factor_center=1.00,
        scale_factor_max=1.15,
    )

    scaled = apply_shared_visual_scale_uncertainty([c1, c2], shared_scale)

    # c1 is estimated: bounds widened by scale factor
    assert scaled[0].mass_bounds.grams_min == pytest.approx(100.0 * 0.85, abs=0.1)
    assert scaled[0].mass_bounds.grams_max == pytest.approx(200.0 * 1.15, abs=0.1)
    # c2 is user-confirmed: untouched
    assert scaled[1].mass_bounds.grams_min == pytest.approx(100.0)
    assert scaled[1].mass_bounds.grams_max == pytest.approx(100.0)
    assert scaled[1].mass_bounds.is_user_confirmed is True


# -----------------------------------------------------------------------------
# 6. Meal Nutrition Aggregator & Range Formatting
# -----------------------------------------------------------------------------

def test_calculate_meal_nutrition_full_plate():
    m1 = estimate_portion_mass("куриное филе", visual_anchor=VisualAnchorType.STANDARD_DINNER_PLATE)
    m2 = estimate_portion_mass("гречка", visual_anchor=VisualAnchorType.STANDARD_DINNER_PLATE)
    m3 = estimate_portion_mass("огурец", visual_anchor=VisualAnchorType.STANDARD_DINNER_PLATE)

    c1 = resolve_and_calculate_component("куриное филе", m1)
    c2 = resolve_and_calculate_component("гречка", m2)
    c3 = resolve_and_calculate_component("огурец", m3)

    assert c1.nutrition_available is True
    assert c2.nutrition_available is True
    assert c3.nutrition_available is True

    anchor_spec = get_anchor_spec(VisualAnchorType.STANDARD_DINNER_PLATE)

    meal = calculate_meal_nutrition(
        [c1, c2, c3],
        meal_name="Обед атлета",
        shared_scale=anchor_spec.shared_scale,
    )

    assert meal.meal_name == "Обед атлета"
    assert meal.has_unresolved_components is False
    assert meal.is_approximate_plausible_range is True
    assert meal.uncertainty_model_type == UncertaintyModelType.CONSERVATIVE_INTERVAL

    cal = meal.total_intervals.calories_kcal
    assert cal is not None
    assert 0.0 < cal.min_value < cal.center_value < cal.max_value

    summary_text = format_meal_summary_text(meal)
    assert "Обед атлета (оценка диапазона)" in summary_text
    assert "Итого:" in summary_text
    assert "ккал" in summary_text
    assert "Б " in summary_text


def test_calculate_meal_nutrition_with_unresolved_component():
    m1 = MassBounds(100, 150, 200)
    m2 = MassBounds(50, 70, 90)

    c1 = resolve_and_calculate_component("гречка", m1)
    c2 = resolve_and_calculate_component("неизвестный_инопланетный_мох", m2)

    meal = calculate_meal_nutrition([c1, c2], meal_name="Странный завтрак")

    assert meal.has_unresolved_components is True
    summary_text = format_meal_summary_text(meal)
    assert "Итого (частично):" in summary_text
    assert "КБЖУ не определено" in summary_text
    assert "итог частичный" in summary_text


def test_format_nutrient_range_variants():
    # Point
    point = NutrientInterval(150.0, 150.0, 150.0)
    assert format_nutrient_range(point, "ккал") == "150 ккал"

    # Interval
    interval = NutrientInterval(120.4, 150.0, 180.8)
    assert format_nutrient_range(interval, "ккал") == "120–181 ккал"

    # None
    assert format_nutrient_range(None) == "—"
