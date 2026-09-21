from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import pytest

from gateway.healbite_recipe_catalog_domain import MealType
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore
from gateway.healbite_recipe_fixtures import (
    TEST_AUTHORS,
    TEST_AUTHOR_A,
    TEST_AUTHOR_B,
    TEST_AUTHOR_C,
    build_test_recipe_catalog,
)
from gateway.healbite_recipe_retrieval import RecipeRetriever


@pytest.fixture
def catalog_store(tmp_path: Path) -> HealBiteRecipeCatalogStore:
    db_path = tmp_path / "test_catalog.db"
    build_test_recipe_catalog(db_path)
    return HealBiteRecipeCatalogStore(db_path, read_only=True)


def test_retrieval_hard_filter_slot(catalog_store: HealBiteRecipeCatalogStore) -> None:
    retriever = RecipeRetriever(catalog_store)
    candidates = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.BREAKFAST,
        authors=list(TEST_AUTHORS.keys()),
        limit=20,
    )
    assert len(candidates) > 0
    for cand in candidates:
        assert MealType.BREAKFAST in cand.meal_types


def test_retrieval_hard_filter_author(catalog_store: HealBiteRecipeCatalogStore) -> None:
    retriever = RecipeRetriever(catalog_store)
    # Test only Author A
    candidates_a = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.LUNCH,
        authors=[TEST_AUTHOR_A],
        limit=20,
    )
    assert len(candidates_a) > 0
    for cand in candidates_a:
        assert cand.author_id == TEST_AUTHOR_A

    # Test Authors A and B
    candidates_ab = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.DINNER,
        authors=[TEST_AUTHOR_A, TEST_AUTHOR_B],
        limit=20,
    )
    authors_found = {c.author_id for c in candidates_ab}
    assert TEST_AUTHOR_C not in authors_found


def test_retrieval_hard_filter_exclusions(catalog_store: HealBiteRecipeCatalogStore) -> None:
    retriever = RecipeRetriever(catalog_store)
    # Exclude POTATO
    candidates_without_potato = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.LUNCH,
        authors=list(TEST_AUTHORS.keys()),
        excluded_ingredients=["POTATO"],
        limit=20,
    )
    for cand in candidates_without_potato:
        ing_ids = {ing.ingredient_id.upper() for ing in cand.recipe.ingredients}
        assert "POTATO" not in ing_ids


def test_retrieval_soft_ranking_inventory(catalog_store: HealBiteRecipeCatalogStore) -> None:
    retriever = RecipeRetriever(catalog_store)
    # Provide inventory that matches specific ingredients (e.g. OATS, MILK)
    inv = {"OATS": Decimal("500"), "MILK": Decimal("1000")}
    candidates = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.BREAKFAST,
        authors=list(TEST_AUTHORS.keys()),
        available_ingredients=inv,
        limit=10,
    )
    assert len(candidates) >= 2
    # The top candidate should have positive ingredient overlap
    top = candidates[0]
    assert top.ingredient_overlap_count >= 1
    assert top.retrieval_score > candidates[-1].retrieval_score or top.ingredient_overlap_count >= candidates[-1].ingredient_overlap_count


def test_retrieval_candidate_limit(catalog_store: HealBiteRecipeCatalogStore) -> None:
    retriever = RecipeRetriever(catalog_store)
    candidates = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.DINNER,
        authors=list(TEST_AUTHORS.keys()),
        limit=3,
    )
    assert len(candidates) <= 3
