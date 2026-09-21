from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from gateway.healbite_recipe_catalog_domain import (
    MealType,
    Recipe,
    RightsStatus,
    VerificationStatus,
)
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore
from gateway.healbite_recipe_grounded_planner import PlannedMealSlot, RecipeGroundedPlan


@dataclass(frozen=True, slots=True)
class ValidatedMealEntry:
    date: str
    slot: MealType
    position: int
    recipe: Recipe
    target_servings: Decimal
    author_display_name: str
    source_title: str
    source_locator: str | None


@dataclass(frozen=True, slots=True)
class GroundingValidationResult:
    valid: bool
    total_meals: int
    verified_recipes: int
    verified_sources: int
    source_coverage: str
    grounding_ratio: float
    unknown_recipe_ids: int
    unverified_recipe_ids: int
    author_distribution: dict[str, int]
    author_warnings: tuple[str, ...] = ()
    variety_violations: tuple[str, ...] = ()
    error_message: str | None = None
    resolved_meals: tuple[ValidatedMealEntry, ...] | None = None


class RecipeGroundingValidator:
    def __init__(self, catalog_store: HealBiteRecipeCatalogStore) -> None:
        self._catalog = catalog_store

    def validate_plan(
        self,
        plan: RecipeGroundedPlan,
        *,
        expected_dates: Sequence[str],
        selected_authors: Sequence[str] | None = None,
    ) -> GroundingValidationResult:
        meals = plan.meals
        total_meals = len(meals)

        if total_meals != 21:
            return GroundingValidationResult(
                valid=False,
                total_meals=total_meals,
                verified_recipes=0,
                verified_sources=0,
                source_coverage=f"{total_meals}/21",
                grounding_ratio=round(total_meals / 21.0, 3),
                unknown_recipe_ids=0,
                unverified_recipe_ids=0,
                author_distribution={},
                error_message=f"INSUFFICIENT_GROUNDED_RECIPES: Expected 21 meals, got {total_meals}",
            )

        recipe_ids = [m.recipe_id for m in meals]
        recipes_by_id = self._catalog.get_recipes_by_ids(recipe_ids)

        unknown_ids = 0
        unverified_ids = 0
        verified_count = 0
        verified_sources_count = 0
        author_dist: dict[str, int] = {}
        resolved_entries: list[ValidatedMealEntry] = []
        seen_recipes: set[str] = set()
        variety_violations: list[str] = []

        # Check unique recipes
        for m in meals:
            if m.recipe_id in seen_recipes:
                variety_violations.append(f"Duplicate recipe_id '{m.recipe_id}' in weekly plan")
            seen_recipes.add(m.recipe_id)

        # Slot order mapping
        slot_positions = {
            MealType.BREAKFAST: 1,
            MealType.LUNCH: 2,
            MealType.DINNER: 3,
        }

        # Check adjacent dinner proteins
        dinners_by_date = {m.date: m.recipe_id for m in meals if m.slot is MealType.DINNER}
        sorted_dates = sorted(expected_dates)
        for i in range(len(sorted_dates) - 1):
            d1, d2 = sorted_dates[i], sorted_dates[i + 1]
            r1 = recipes_by_id.get(dinners_by_date.get(d1, ""))
            r2 = recipes_by_id.get(dinners_by_date.get(d2, ""))
            if r1 and r2:
                # Find main protein tags
                p1 = {t for t in r1.tags if t in ("говядина", "курица", "рыба", "свинина", "лосось", "треска")}
                p2 = {t for t in r2.tags if t in ("говядина", "курица", "рыба", "свинина", "лосось", "треска")}
                if p1 and p2 and p1 == p2:
                    # Soft variety violation note
                    variety_violations.append(
                        f"Consecutive dinner protein repetition on {d1} and {d2}: {','.join(p1)}"
                    )

        for m in meals:
            recipe = recipes_by_id.get(m.recipe_id)
            if not recipe:
                unknown_ids += 1
                continue

            if not recipe.is_verified_for_planning:
                unverified_ids += 1
                continue

            source = self._catalog.get_source(recipe.source_id)
            if not source or source.rights_status in (RightsStatus.UNKNOWN, RightsStatus.LINK_ONLY):
                unverified_ids += 1
                continue

            author = self._catalog.get_author(recipe.author_id)
            author_name = author.display_name if author else recipe.author_id

            author_dist[recipe.author_id] = author_dist.get(recipe.author_id, 0) + 1
            verified_count += 1
            verified_sources_count += 1

            resolved_entries.append(
                ValidatedMealEntry(
                    date=m.date,
                    slot=m.slot,
                    position=1,
                    recipe=recipe,
                    target_servings=m.target_servings,
                    author_display_name=author_name,
                    source_title=source.title,
                    source_locator=recipe.source_locator or source.source_locator,
                )
            )

        grounding_ratio = round(verified_count / 21.0, 3)
        source_coverage = f"{verified_count}/21"

        # Check author warnings if multiple authors were requested
        author_warnings: list[str] = []
        if selected_authors and len(selected_authors) > 1:
            for auth in selected_authors:
                count = author_dist.get(auth, 0)
                if count == 0:
                    author_warnings.append(f"Автор {auth} не представлен в итоговом плане из-за строгих ограничений")
                elif count < 2:
                    author_warnings.append(f"Автор {auth} представлен только {count} блюдом")

        is_valid = (
            verified_count == 21
            and verified_sources_count == 21
            and unknown_ids == 0
            and unverified_ids == 0
            and len(variety_violations) == 0
        )

        error_msg = None
        if not is_valid:
            if verified_count < 21:
                error_msg = f"INSUFFICIENT_GROUNDED_RECIPES: Only {verified_count}/21 verified recipes satisfied constraints"
            elif variety_violations:
                error_msg = f"VARIETY_RULE_VIOLATION: {'; '.join(variety_violations)}"
            elif unknown_ids > 0:
                error_msg = f"UNKNOWN_RECIPE_IDS: {unknown_ids} unknown recipe IDs found in plan"

        return GroundingValidationResult(
            valid=is_valid,
            total_meals=total_meals,
            verified_recipes=verified_count,
            verified_sources=verified_sources_count,
            source_coverage=source_coverage,
            grounding_ratio=grounding_ratio,
            unknown_recipe_ids=unknown_ids,
            unverified_recipe_ids=unverified_ids,
            author_distribution=author_dist,
            author_warnings=tuple(author_warnings),
            variety_violations=tuple(variety_violations),
            error_message=error_msg,
            resolved_meals=tuple(resolved_entries) if is_valid else None,
        )
