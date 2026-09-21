from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import pytest

from gateway.healbite_recipe_catalog_domain import MealType
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore
from gateway.healbite_recipe_fixtures import TEST_AUTHORS, build_test_recipe_catalog
from gateway.healbite_recipe_grounded_planner import (
    PlannedMealSlot,
    PlannerGroundingError,
    RecipeGroundedPlan,
    RecipeGroundedWeeklyPlanner,
)
from gateway.healbite_recipe_grounding_validator import RecipeGroundingValidator
from gateway.healbite_recipe_retrieval import RecipeRetriever


@pytest.fixture
def catalog_store(tmp_path: Path) -> HealBiteRecipeCatalogStore:
    db_path = tmp_path / "planner_test_catalog.db"
    build_test_recipe_catalog(db_path)
    return HealBiteRecipeCatalogStore(db_path, read_only=True)


def _make_dummy_response(meals: list[dict[str, object]], week_start: str = "2026-09-21") -> dict[str, object]:
    content = json.dumps({"week_start": week_start, "meals": meals})
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": f"```json\n{content}\n```",
                }
            }
        ]
    }


def test_planner_happy_path(catalog_store: HealBiteRecipeCatalogStore) -> None:
    retriever = RecipeRetriever(catalog_store)
    authors = list(TEST_AUTHORS.keys())
    slot_cands = {
        MealType.BREAKFAST: retriever.retrieve_candidates_for_slot(meal_slot=MealType.BREAKFAST, authors=authors),
        MealType.LUNCH: retriever.retrieve_candidates_for_slot(meal_slot=MealType.LUNCH, authors=authors),
        MealType.DINNER: retriever.retrieve_candidates_for_slot(meal_slot=MealType.DINNER, authors=authors),
    }

    # Generate 21 distinct meals
    dates = [
        "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
        "2026-09-25", "2026-09-26", "2026-09-27"
    ]
    mock_meals = []
    for day_idx, dt in enumerate(dates):
        mock_meals.append({
            "date": dt,
            "slot": "breakfast",
            "recipe_id": slot_cands[MealType.BREAKFAST][day_idx].recipe_id,
            "servings": 2,
        })
        mock_meals.append({
            "date": dt,
            "slot": "lunch",
            "recipe_id": slot_cands[MealType.LUNCH][day_idx].recipe_id,
            "servings": 2,
        })
        mock_meals.append({
            "date": dt,
            "slot": "dinner",
            "recipe_id": slot_cands[MealType.DINNER][day_idx].recipe_id,
            "servings": 2,
        })

    def mock_llm_caller(*args, **kwargs):
        return _make_dummy_response(mock_meals)

    planner = RecipeGroundedWeeklyPlanner(llm_caller=mock_llm_caller)
    plan = planner.plan(
        week_start="2026-09-21",
        dates=dates,
        slot_candidates=slot_cands,
        target_servings=Decimal("2"),
    )

    assert len(plan.meals) == 21

    # Validate plan with RecipeGroundingValidator
    validator = RecipeGroundingValidator(catalog_store)
    res = validator.validate_plan(plan, expected_dates=dates, selected_authors=authors)

    assert res.valid is True
    assert res.total_meals == 21
    assert res.source_coverage == "21/21"
    assert res.grounding_ratio == 1.0
    assert res.unknown_recipe_ids == 0
    assert len(res.resolved_meals or ()) == 21


def test_planner_repair_on_syntax_error(catalog_store: HealBiteRecipeCatalogStore) -> None:
    retriever = RecipeRetriever(catalog_store)
    authors = list(TEST_AUTHORS.keys())
    slot_cands = {
        MealType.BREAKFAST: retriever.retrieve_candidates_for_slot(meal_slot=MealType.BREAKFAST, authors=authors),
        MealType.LUNCH: retriever.retrieve_candidates_for_slot(meal_slot=MealType.LUNCH, authors=authors),
        MealType.DINNER: retriever.retrieve_candidates_for_slot(meal_slot=MealType.DINNER, authors=authors),
    }
    dates = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]

    call_count = 0

    def mock_flaky_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call returns invalid JSON
            return {"choices": [{"message": {"role": "assistant", "content": "I cannot help you with that JSON"}}]}
        # Second call returns valid 21 meals
        mock_meals = []
        for day_idx, dt in enumerate(dates):
            mock_meals.append({"date": dt, "slot": "breakfast", "recipe_id": slot_cands[MealType.BREAKFAST][day_idx].recipe_id, "servings": 2})
            mock_meals.append({"date": dt, "slot": "lunch", "recipe_id": slot_cands[MealType.LUNCH][day_idx].recipe_id, "servings": 2})
            mock_meals.append({"date": dt, "slot": "dinner", "recipe_id": slot_cands[MealType.DINNER][day_idx].recipe_id, "servings": 2})
        return _make_dummy_response(mock_meals)

    planner = RecipeGroundedWeeklyPlanner(llm_caller=mock_flaky_llm)
    plan = planner.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)

    assert call_count == 2
    assert len(plan.meals) == 21


def test_validator_rejects_insufficient_meals(catalog_store: HealBiteRecipeCatalogStore) -> None:
    validator = RecipeGroundingValidator(catalog_store)
    # Only 3 meals instead of 21
    partial_plan = RecipeGroundedPlan(
        week_start="2026-09-21",
        meals=(
            PlannedMealSlot(date="2026-09-21", slot=MealType.BREAKFAST, recipe_id="some_id", recipe_version=1, target_servings=Decimal("2")),
            PlannedMealSlot(date="2026-09-21", slot=MealType.LUNCH, recipe_id="some_id", recipe_version=1, target_servings=Decimal("2")),
            PlannedMealSlot(date="2026-09-21", slot=MealType.DINNER, recipe_id="some_id", recipe_version=1, target_servings=Decimal("2")),
        ),
    )
    res = validator.validate_plan(partial_plan, expected_dates=["2026-09-21"])
    assert res.valid is False
    assert "INSUFFICIENT_GROUNDED_RECIPES" in (res.error_message or "")
    assert res.total_meals == 3
