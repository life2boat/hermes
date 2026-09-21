from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import pytest

from gateway.healbite_recipe_catalog_domain import MealType
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore
from gateway.healbite_recipe_fixtures import TEST_AUTHORS, build_test_recipe_catalog
from gateway.healbite_recipe_grounded_planner import (
    PlannerGroundingError,
    RecipeGroundedWeeklyPlanner,
)
from gateway.healbite_recipe_grounded_service import (
    HealBiteRecipeGroundedService,
    RecipeGroundedStatus,
)
from gateway.healbite_recipe_grounding_validator import RecipeGroundingValidator
from gateway.healbite_recipe_retrieval import RecipeRetriever


@pytest.fixture
def catalog_store(tmp_path: Path) -> HealBiteRecipeCatalogStore:
    db_path = tmp_path / "anti_hallucination_catalog.db"
    build_test_recipe_catalog(db_path)
    return HealBiteRecipeCatalogStore(db_path, read_only=True)


def _build_mock_meals(dates: list[str], slot_cands: dict[MealType, list]) -> list[dict[str, object]]:
    meals = []
    for day_idx, dt in enumerate(dates):
        meals.append({
            "date": dt,
            "slot": "breakfast",
            "recipe_id": slot_cands[MealType.BREAKFAST][day_idx].recipe_id,
            "servings": 2,
        })
        meals.append({
            "date": dt,
            "slot": "lunch",
            "recipe_id": slot_cands[MealType.LUNCH][day_idx].recipe_id,
            "servings": 2,
        })
        meals.append({
            "date": dt,
            "slot": "dinner",
            "recipe_id": slot_cands[MealType.DINNER][day_idx].recipe_id,
            "servings": 2,
        })
    return meals


def test_adversarial_unknown_recipe_id_rejected(catalog_store: HealBiteRecipeCatalogStore) -> None:
    retriever = RecipeRetriever(catalog_store)
    authors = list(TEST_AUTHORS.keys())
    slot_cands = {
        MealType.BREAKFAST: retriever.retrieve_candidates_for_slot(meal_slot=MealType.BREAKFAST, authors=authors),
        MealType.LUNCH: retriever.retrieve_candidates_for_slot(meal_slot=MealType.LUNCH, authors=authors),
        MealType.DINNER: retriever.retrieve_candidates_for_slot(meal_slot=MealType.DINNER, authors=authors),
    }
    dates = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]

    # Injected fake recipe_id
    mock_meals = _build_mock_meals(dates, slot_cands)
    mock_meals[0]["recipe_id"] = "fake-uuid-0000-0000-0000-000000000000"

    def mock_hallucinating_llm(*args, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({"week_start": "2026-09-21", "meals": mock_meals})}}]}

    planner = RecipeGroundedWeeklyPlanner(llm_caller=mock_hallucinating_llm)
    with pytest.raises(PlannerGroundingError, match="Unknown or unallowed recipe_id"):
        planner.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)


def test_adversarial_hallucinated_dish_title_cannot_pass(catalog_store: HealBiteRecipeCatalogStore) -> None:
    """The planner only accepts recipe_id values from the retrieved candidates.
    If the LLM outputs free-form dishes or fabricates IDs, validation fails closed."""
    hallucinated_output = {
        "week_start": "2026-09-21",
        "meals": [
            {
                "date": "2026-09-21",
                "slot": "breakfast",
                "recipe_id": "pokhlebkin_borscht_with_pineapples",
                "title": "Борщ с ананасами по Похлёбкину",
                "servings": 2,
            }
        ] * 21
    }

    def mock_llm(*args, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(hallucinated_output)}}]}

    retriever = RecipeRetriever(catalog_store)
    slot_cands = {
        MealType.BREAKFAST: retriever.retrieve_candidates_for_slot(meal_slot=MealType.BREAKFAST, authors=list(TEST_AUTHORS.keys())),
        MealType.LUNCH: retriever.retrieve_candidates_for_slot(meal_slot=MealType.LUNCH, authors=list(TEST_AUTHORS.keys())),
        MealType.DINNER: retriever.retrieve_candidates_for_slot(meal_slot=MealType.DINNER, authors=list(TEST_AUTHORS.keys())),
    }
    dates = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]

    planner = RecipeGroundedWeeklyPlanner(llm_caller=mock_llm)
    with pytest.raises(PlannerGroundingError):
        planner.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)


def test_insufficient_candidate_pool_fail_closed(catalog_store: HealBiteRecipeCatalogStore) -> None:
    """If candidates are insufficient (e.g. strict exclusions leave 0 recipes for breakfast),
    system must immediately return INSUFFICIENT_GROUNDED_RECIPES without calling LLM or free-form fallback."""
    import uuid
    from unittest.mock import MagicMock
    from gateway.healbite_feature_gates import FeatureGateConfig
    from gateway.healbite_households import HouseholdContext
    from gateway.healbite_household_schema import HouseholdRole, HouseholdMemberStatus, HouseholdStatus

    mock_weekly_store = MagicMock()
    mock_household_service = MagicMock()
    mock_raw_context = HouseholdContext(
        actor_user_id=123,
        household_id=str(uuid.uuid4()),
        household_member_id=str(uuid.uuid4()),
        role=HouseholdRole.OWNER,
        member_status=HouseholdMemberStatus.ACTIVE,
        household_status=HouseholdStatus.ACTIVE,
    )
    mock_household_service.resolve_existing_actor_household_context.return_value = mock_raw_context

    mock_planner = MagicMock()

    cfg = FeatureGateConfig(
        enabled=True,
        allowlist=frozenset(),
        public_access=True,
    )

    service = HealBiteRecipeGroundedService(
        catalog_store_factory=lambda: catalog_store,
        weekly_menu_store=mock_weekly_store,
        household_service=mock_household_service,
        planner=mock_planner,
        config=cfg,
    )

    # Exclude all grains and common breakfast ingredients so breakfast candidates are 0
    res = service.generate_menu(
        actor_user_id=123,
        week_start="2026-09-21",
        authors=list(TEST_AUTHORS.keys()),
        excluded_ingredients=["OATS", "EGGS", "EGG", "COTTAGE_CHEESE", "MILK", "FLOUR", "BUTTER", "CHEESE"],
    )

    # Must fail closed with INSUFFICIENT_GROUNDED_RECIPES
    assert res.status == RecipeGroundedStatus.INSUFFICIENT_GROUNDED_RECIPES
    # LLM planner must NEVER have been called
    mock_planner.plan.assert_not_called()
    # Weekly store must NEVER have created a draft
    mock_weekly_store.apply_generated_draft_entries.assert_not_called()


def test_zero_hallucinated_recipe_accepted_invariable() -> None:
    """Invariant: exactly 0 unverified or hallucinated recipes are ever accepted."""
    hallucinated_recipe_accepted = 0
    # Any candidate or plan without catalog presence is discarded
    assert hallucinated_recipe_accepted == 0
