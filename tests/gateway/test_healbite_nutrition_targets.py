from __future__ import annotations

import pytest

from gateway.healbite_nutrition_targets import (
    ACTIVITY_FACTORS,
    GOAL_GAIN,
    GOAL_LOSE,
    GOAL_MAINTAIN,
    NUTRITION_CALCULATION_VERSION,
    NutritionProfileInputs,
    NutritionTargetValidationError,
    calculate_nutrition_targets,
)


def test_calculate_targets_for_male_moderate_maintain():
    result = calculate_nutrition_targets(
        NutritionProfileInputs(
            sex="male",
            age=35,
            height_cm=180,
            weight_kg=85,
            activity_level="moderate",
            goal=GOAL_MAINTAIN,
        )
    )

    assert result.calculation_version == NUTRITION_CALCULATION_VERSION
    assert result.target_source == "calculated"
    assert result.bmr_kcal == 1805
    assert result.tdee_kcal == 2798
    assert result.daily_kcal_target == 2798
    assert result.daily_protein_g == 136
    assert result.daily_fat_g == 68
    assert result.daily_carbs_g == 410
    assert result.calorie_delta_kcal <= 15


def test_calculate_targets_for_female_light_lose():
    result = calculate_nutrition_targets(
        NutritionProfileInputs(
            sex="female",
            age=30,
            height_cm=165,
            weight_kg=60,
            activity_level="light",
            goal=GOAL_LOSE,
        )
    )

    assert result.bmr_kcal == 1320
    assert result.tdee_kcal == 1815
    assert result.daily_kcal_target == 1543
    assert result.daily_protein_g == 96
    assert result.daily_fat_g == 48
    assert result.daily_carbs_g == 182
    assert result.calorie_delta_kcal <= 15


def test_gain_goal_increases_target_above_maintain():
    maintain = calculate_nutrition_targets(
        NutritionProfileInputs(
            sex="male",
            age=35,
            height_cm=180,
            weight_kg=85,
            activity_level="moderate",
            goal=GOAL_MAINTAIN,
        )
    )
    gain = calculate_nutrition_targets(
        NutritionProfileInputs(
            sex="male",
            age=35,
            height_cm=180,
            weight_kg=85,
            activity_level="moderate",
            goal=GOAL_GAIN,
        )
    )

    assert gain.daily_kcal_target > maintain.daily_kcal_target


@pytest.mark.parametrize(
    ("activity_level", "expected_factor"),
    [
        ("sedentary", 1.20),
        ("light", 1.375),
        ("moderate", 1.55),
        ("high", 1.725),
        ("very_high", 1.90),
    ],
)
def test_activity_factors_are_applied(activity_level: str, expected_factor: float):
    result = calculate_nutrition_targets(
        NutritionProfileInputs(
            sex="female",
            age=30,
            height_cm=165,
            weight_kg=60,
            activity_level=activity_level,
            goal=GOAL_MAINTAIN,
        )
    )

    assert ACTIVITY_FACTORS[activity_level] == expected_factor
    assert result.tdee_kcal == round(result.bmr_kcal * expected_factor)


def test_manual_target_recalculates_macros_against_manual_kcal():
    result = calculate_nutrition_targets(
        NutritionProfileInputs(
            sex="male",
            age=35,
            height_cm=180,
            weight_kg=85,
            activity_level="moderate",
            goal=GOAL_MAINTAIN,
        ),
        manual_kcal_target=2000,
        target_source="manual",
    )

    assert result.target_source == "manual"
    assert result.daily_kcal_target == 2000
    assert result.daily_protein_g == 136
    assert result.daily_fat_g == 68
    assert result.daily_carbs_g == 211
    assert result.calorie_delta_kcal <= 15


@pytest.mark.parametrize(
    "inputs",
    [
        NutritionProfileInputs(sex="male", age=17, height_cm=180, weight_kg=85, activity_level="moderate", goal=GOAL_MAINTAIN),
        NutritionProfileInputs(sex="female", age=30, height_cm=110, weight_kg=60, activity_level="light", goal=GOAL_LOSE),
        NutritionProfileInputs(sex="female", age=30, height_cm=165, weight_kg=20, activity_level="light", goal=GOAL_LOSE),
        NutritionProfileInputs(sex="other", age=30, height_cm=165, weight_kg=60, activity_level="light", goal=GOAL_LOSE),
        NutritionProfileInputs(sex="female", age=30, height_cm=165, weight_kg=60, activity_level="extreme", goal=GOAL_LOSE),
        NutritionProfileInputs(sex="female", age=30, height_cm=165, weight_kg=60, activity_level="light", goal="bulk"),
    ],
)
def test_invalid_inputs_raise_validation_error(inputs: NutritionProfileInputs):
    with pytest.raises(NutritionTargetValidationError):
        calculate_nutrition_targets(inputs)


def test_impossible_carbs_budget_raises_validation_error():
    with pytest.raises(NutritionTargetValidationError):
        calculate_nutrition_targets(
            NutritionProfileInputs(
                sex="male",
                age=35,
                height_cm=180,
                weight_kg=85,
                activity_level="moderate",
                goal=GOAL_MAINTAIN,
            ),
            manual_kcal_target=1000,
            target_source="manual",
        )


def test_result_is_deterministic():
    inputs = NutritionProfileInputs(
        sex="female",
        age=30,
        height_cm=165,
        weight_kg=60,
        activity_level="light",
        goal=GOAL_LOSE,
    )

    first = calculate_nutrition_targets(inputs)
    second = calculate_nutrition_targets(inputs)

    assert first == second
