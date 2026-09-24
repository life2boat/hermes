from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Protocol, Sequence

from gateway.healbite_recipe_catalog_domain import (
    MealType,
    Recipe,
    RightsStatus,
    TargetRightsScope,
    VerificationStatus,
    load_target_rights_scope,
)
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore


@dataclass(frozen=True, slots=True)
class RecipeCandidate:
    recipe_id: str
    recipe_version: int
    author_id: str
    source_id: str
    title: str
    meal_types: tuple[MealType, ...]
    servings: Decimal
    ingredient_overlap_count: int
    missing_ingredient_count: int
    retrieval_score: float
    source_verified: bool
    recipe: Recipe


class RecipeVectorIndexProtocol(Protocol):
    def search_similar(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        ...


class RecipeRetriever:
    def __init__(
        self,
        catalog_store: HealBiteRecipeCatalogStore,
        *,
        vector_index: RecipeVectorIndexProtocol | None = None,
        target_rights_scope: str | Sequence[str] | TargetRightsScope | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._catalog = catalog_store
        self._vector_index = vector_index
        self._target_rights_scope = (
            load_target_rights_scope(target_rights_scope, env=env)
            or catalog_store.target_rights_scope
        )

    def retrieve_candidates_for_slot(
        self,
        *,
        meal_slot: MealType | str,
        authors: Sequence[str],
        available_ingredients: Mapping[str, Decimal | None] | None = None,
        excluded_ingredients: Sequence[str] | None = None,
        dietary_constraints: Sequence[str] | None = None,
        max_time_minutes: int | None = None,
        limit: int = 12,
        production_only: bool = False,
        target_rights_scope: str | Sequence[str] | TargetRightsScope | None = None,
    ) -> list[RecipeCandidate]:
        """
        Applies strict hard filters (verified, author, exclusions, meal slot)
        then ranks by soft criteria (inventory overlap, missing ingredients).
        """
        target_slot = MealType(meal_slot) if isinstance(meal_slot, str) else meal_slot
        selected_authors = set(authors) if authors else set()
        excluded_set = {ing.strip().upper() for ing in (excluded_ingredients or ())}
        inv_map = available_ingredients or {}

        effective_scope = (
            load_target_rights_scope(target_rights_scope)
            or self._target_rights_scope
        )

        all_recipes = self._catalog.get_all_recipes(
            verified_only=True,
            target_rights_scope=effective_scope,
        )
        candidates: list[RecipeCandidate] = []

        for recipe in all_recipes:
            # 1. Hard filter: verified for planning
            if not recipe.is_verified_for_planning:
                continue

            if production_only and not recipe.is_production_cleared_for_scope(effective_scope):
                continue


            # 2. Hard filter: author selection
            if selected_authors and recipe.author_id not in selected_authors:
                continue

            # 3. Hard filter: meal slot
            if target_slot not in recipe.meal_types:
                continue

            # 4. Hard filter: time
            if max_time_minutes and recipe.total_minutes and recipe.total_minutes > max_time_minutes:
                continue

            # 5. Hard filter: exclusions (allergies / diets)
            recipe_ingredient_ids = {ing.ingredient_id.upper() for ing in recipe.ingredients}
            if any(exc in recipe_ingredient_ids for exc in excluded_set):
                continue

            # 6. Soft scoring: inventory overlap
            overlap_count = 0
            missing_count = 0
            for ing in recipe.ingredients:
                ing_id = ing.ingredient_id.upper()
                if ing_id in inv_map and (inv_map[ing_id] is None or inv_map[ing_id] > 0):
                    overlap_count += 1
                else:
                    if not ing.optional:
                        missing_count += 1

            base_score = 10.0
            score = base_score + (overlap_count * 3.0) - (missing_count * 0.5)

            candidates.append(
                RecipeCandidate(
                    recipe_id=recipe.recipe_id,
                    recipe_version=recipe.recipe_version,
                    author_id=recipe.author_id,
                    source_id=recipe.source_id,
                    title=recipe.title,
                    meal_types=recipe.meal_types,
                    servings=recipe.servings,
                    ingredient_overlap_count=overlap_count,
                    missing_ingredient_count=missing_count,
                    retrieval_score=round(score, 2),
                    source_verified=True,
                    recipe=recipe,
                )
            )

        # Sort descending by score
        candidates.sort(key=lambda c: c.retrieval_score, reverse=True)
        return candidates[:limit]
