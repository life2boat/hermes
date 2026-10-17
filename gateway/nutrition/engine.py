"""
gateway.nutrition.engine — Nutrition Calculation Engine.

Provides:
1. Component nutrition resolution and conservative interval calculation.
2. Recipe mass constraints & cooking yield consistency solver.
3. Shared visual scale uncertainty propagation across meal components.
4. Full meal nutrition aggregation and Telegram-friendly range formatting.
"""

from __future__ import annotations

from typing import Sequence

from gateway.healbite_food_composition import resolve_food_composition
from gateway.nutrition.contracts import (
    ComponentNutritionEstimate,
    MacronutrientIntervals,
    MassBounds,
    MealNutritionEstimate,
    NutrientInterval,
    RecipeMassConstraint,
    SharedVisualScaleUncertainty,
    UncertaintyModelType,
    aggregate_conservative_intervals,
    calculate_component_macronutrient_intervals,
)
from gateway.nutrition.hfkb import HFKBCatalog, get_default_hfkb
from gateway.nutrition.search import find_candidate_suggestions, normalize_russian_food_query


# -----------------------------------------------------------------------------
# Component Level Calculation
# -----------------------------------------------------------------------------

def resolve_and_calculate_component(
    food_name: str,
    mass_bounds: MassBounds,
    *,
    cooking_state: str = "cooked",
    hfkb: HFKBCatalog | None = None,
) -> ComponentNutritionEstimate:
    """
    Resolves the food item in the HFKB or legacy alias index and calculates
    conservative nutrient intervals.

    If unresolved, marks uncertainty_model_type=UNRESOLVED, nutrition_available=False,
    and attaches candidate suggestions.
    """
    catalog = hfkb or get_default_hfkb()

    # Step 1: Query HFKB if bound
    if catalog is not None and getattr(catalog, "is_open", True):
        if hasattr(catalog, "resolve"):
            match_res = catalog.resolve(food_name, preferred_cooking_state=cooking_state)
            if match_res.matched and match_res.profile is not None and match_res.confidence >= 0.70:
                profile = match_res.profile
                intervals = calculate_component_macronutrient_intervals(
                    mass_bounds,
                    profile.calories_kcal,
                    profile.protein_g,
                    profile.fat_g,
                    profile.carbs_g,
                )
                return ComponentNutritionEstimate(
                    name=profile.display_name,
                    mass_bounds=mass_bounds,
                    intervals=intervals,
                    source_id=profile.source_id,
                    cooking_state=profile.cooking_state or cooking_state,
                    source_provenance=profile.source_provenance,
                    per_100g_calories_kcal=profile.calories_kcal,
                    per_100g_protein_g=profile.protein_g,
                    per_100g_fat_g=profile.fat_g,
                    per_100g_carbs_g=profile.carbs_g,
                    uncertainty_model_type=UncertaintyModelType.CONSERVATIVE_INTERVAL,
                    is_approximate_plausible_range=mass_bounds.is_approximate_plausible_range,
                    nutrition_available=True,
                    candidate_suggestions=tuple(match_res.candidate_suggestions),
                )
        elif hasattr(catalog, "search"):
            match_result = catalog.search(food_name, limit=5)
            if match_result.best_item is not None and match_result.confidence >= 0.70:
                item = match_result.best_item
                profile = item.nutrient_profile
                intervals = calculate_component_macronutrient_intervals(
                    mass_bounds,
                    profile.calories_kcal,
                    profile.protein_g,
                    profile.fat_g,
                    profile.carbs_g,
                )
                return ComponentNutritionEstimate(
                    name=item.canonical_name,
                    mass_bounds=mass_bounds,
                    intervals=intervals,
                    source_id=item.source_id,
                    cooking_state=item.preparation_state or cooking_state,
                    source_provenance=item.source_provenance,
                    per_100g_calories_kcal=profile.calories_kcal,
                    per_100g_protein_g=profile.protein_g,
                    per_100g_fat_g=profile.fat_g,
                    per_100g_carbs_g=profile.carbs_g,
                    uncertainty_model_type=UncertaintyModelType.CONSERVATIVE_INTERVAL,
                    is_approximate_plausible_range=mass_bounds.is_approximate_plausible_range,
                    nutrition_available=True,
                    candidate_suggestions=(),
                )

    # Step 2: Fall back to in-memory verified catalog / alias index
    legacy_result = resolve_food_composition(food_name)
    if legacy_result is not None and legacy_result.matched and legacy_result.profile is not None:
        legacy_profile = legacy_result.profile
        intervals = calculate_component_macronutrient_intervals(
            mass_bounds,
            legacy_profile.calories_kcal,
            legacy_profile.protein_g,
            legacy_profile.fat_g,
            legacy_profile.carbs_g,
        )
        return ComponentNutritionEstimate(
            name=legacy_profile.display_name,
            mass_bounds=mass_bounds,
            intervals=intervals,
            source_id=legacy_profile.source_id,
            cooking_state=legacy_profile.cooking_state or cooking_state,
            source_provenance=legacy_profile.dataset_version,
            per_100g_calories_kcal=legacy_profile.calories_kcal,
            per_100g_protein_g=legacy_profile.protein_g,
            per_100g_fat_g=legacy_profile.fat_g,
            per_100g_carbs_g=legacy_profile.carbs_g,
            uncertainty_model_type=UncertaintyModelType.CONSERVATIVE_INTERVAL,
            is_approximate_plausible_range=mass_bounds.is_approximate_plausible_range,
            nutrition_available=True,
            candidate_suggestions=(),
        )

    # Step 3: Unresolved query — provide candidate suggestions
    suggestions: list[str] = []
    if legacy_result is not None and legacy_result.candidate_suggestions:
        suggestions = list(legacy_result.candidate_suggestions)
    elif catalog is not None and catalog.is_open:
        search_res = catalog.search(food_name, limit=3)
        suggestions = list(search_res.candidate_suggestions)
    if not suggestions:
        from gateway.healbite_food_composition import _ALIAS_INDEX
        suggestions = find_candidate_suggestions(food_name, _ALIAS_INDEX.keys(), limit=3)

    return ComponentNutritionEstimate(
        name=food_name,
        mass_bounds=mass_bounds,
        intervals=MacronutrientIntervals(),
        source_id="",
        cooking_state=cooking_state,
        source_provenance="",
        uncertainty_model_type=UncertaintyModelType.UNRESOLVED,
        is_approximate_plausible_range=True,
        nutrition_available=False,
        candidate_suggestions=tuple(suggestions),
        variance_note="Не удалось сопоставить продукт со справочником питания",
    )


# -----------------------------------------------------------------------------
# Recipe Mass Constraints & Yield Consistency Solver
# -----------------------------------------------------------------------------

def _matches_any_ingredient(name: str, candidate_set: set[str]) -> bool:
    if not candidate_set:
        return False
    norm_name = normalize_russian_food_query(name)
    if norm_name in candidate_set:
        return True
    from gateway.nutrition.search import extract_stemmed_tokens
    name_stems = set(extract_stemmed_tokens(norm_name))
    for cand in candidate_set:
        if cand in norm_name or norm_name in cand:
            return True
        cand_stems = set(extract_stemmed_tokens(cand))
        if cand_stems and (cand_stems.issubset(name_stems) or name_stems.issubset(cand_stems) or bool(cand_stems & name_stems)):
            return True
    return False


def solve_recipe_mass_constraints(
    components: Sequence[ComponentNutritionEstimate],
    constraint: RecipeMassConstraint,
) -> list[ComponentNutritionEstimate]:
    """
    Enforces total dish mass limits and cooking yield consistency across ingredients.

    Ingredients belonging to the recipe are scaled proportionally to meet
    [total_dish_mass_min_g, total_dish_mass_max_g] while preserving ingredient mass ratios.
    External side dishes/toppings remain unaffected.
    """
    if constraint.total_dish_mass_min_g is None and constraint.total_dish_mass_max_g is None:
        return list(components)

    recipe_names = {normalize_russian_food_query(n) for n in constraint.recipe_ingredient_names}
    external_names = {normalize_russian_food_query(n) for n in constraint.external_ingredient_names}

    recipe_indices: list[int] = []
    for idx, c in enumerate(components):
        if recipe_names:
            if _matches_any_ingredient(c.name, recipe_names):
                recipe_indices.append(idx)
        else:
            if not _matches_any_ingredient(c.name, external_names):
                recipe_indices.append(idx)

    if not recipe_indices:
        return list(components)

    # Sum raw center masses of recipe ingredients
    raw_center_sum = sum(components[i].mass_bounds.grams_center for i in recipe_indices)
    if raw_center_sum <= 0.0:
        return list(components)

    # Convert to implied cooked dish mass
    implied_cooked_mass = constraint.raw_to_cooked_mass(raw_center_sum)
    clamped_cooked_mass = constraint.clamp_total_dish_mass(implied_cooked_mass)

    scaling_factor = clamped_cooked_mass / implied_cooked_mass

    # If within bounds, no scaling needed
    if abs(scaling_factor - 1.0) < 1e-4:
        return list(components)

    out: list[ComponentNutritionEstimate] = list(components)
    for i in recipe_indices:
        comp = components[i]
        old_mass = comp.mass_bounds
        new_mass = MassBounds(
            grams_min=round(old_mass.grams_min * scaling_factor, 1),
            grams_center=round(old_mass.grams_center * scaling_factor, 1),
            grams_max=round(old_mass.grams_max * scaling_factor, 1),
            is_approximate_plausible_range=old_mass.is_approximate_plausible_range,
            is_user_confirmed=old_mass.is_user_confirmed,
        )

        # Recalculate nutrient intervals if nutrition is available
        if comp.nutrition_available:
            new_intervals = calculate_component_macronutrient_intervals(
                new_mass,
                comp.per_100g_calories_kcal,
                comp.per_100g_protein_g,
                comp.per_100g_fat_g,
                comp.per_100g_carbs_g,
            )
        else:
            new_intervals = comp.intervals

        out[i] = ComponentNutritionEstimate(
            name=comp.name,
            mass_bounds=new_mass,
            intervals=new_intervals,
            source_id=comp.source_id,
            cooking_state=comp.cooking_state,
            source_provenance=comp.source_provenance,
            per_100g_calories_kcal=comp.per_100g_calories_kcal,
            per_100g_protein_g=comp.per_100g_protein_g,
            per_100g_fat_g=comp.per_100g_fat_g,
            per_100g_carbs_g=comp.per_100g_carbs_g,
            uncertainty_model_type=comp.uncertainty_model_type,
            is_approximate_plausible_range=comp.is_approximate_plausible_range,
            nutrition_available=comp.nutrition_available,
            candidate_suggestions=comp.candidate_suggestions,
            variance_note=f"Скорректировано по массе готового блюда (коэффициент {scaling_factor:.2f})",
        )

    return out


# -----------------------------------------------------------------------------
# Shared Visual Scale Uncertainty Propagation
# -----------------------------------------------------------------------------

def apply_shared_visual_scale_uncertainty(
    components: Sequence[ComponentNutritionEstimate],
    shared_scale: SharedVisualScaleUncertainty,
) -> list[ComponentNutritionEstimate]:
    """
    Applies shared visual scale uncertainty factor across components on the same plate.
    Components with user-confirmed masses are not modified.
    """
    out: list[ComponentNutritionEstimate] = []
    for comp in components:
        if comp.mass_bounds.is_user_confirmed:
            out.append(comp)
            continue

        scaled_mass = shared_scale.apply_to_mass(comp.mass_bounds)
        if comp.nutrition_available:
            scaled_intervals = calculate_component_macronutrient_intervals(
                scaled_mass,
                comp.per_100g_calories_kcal,
                comp.per_100g_protein_g,
                comp.per_100g_fat_g,
                comp.per_100g_carbs_g,
            )
        else:
            scaled_intervals = comp.intervals

        out.append(
            ComponentNutritionEstimate(
                name=comp.name,
                mass_bounds=scaled_mass,
                intervals=scaled_intervals,
                source_id=comp.source_id,
                cooking_state=comp.cooking_state,
                source_provenance=comp.source_provenance,
                per_100g_calories_kcal=comp.per_100g_calories_kcal,
                per_100g_protein_g=comp.per_100g_protein_g,
                per_100g_fat_g=comp.per_100g_fat_g,
                per_100g_carbs_g=comp.per_100g_carbs_g,
                uncertainty_model_type=comp.uncertainty_model_type,
                is_approximate_plausible_range=True,
                nutrition_available=comp.nutrition_available,
                candidate_suggestions=comp.candidate_suggestions,
                variance_note=comp.variance_note,
            )
        )
    return out


# -----------------------------------------------------------------------------
# Full Meal Aggregator
# -----------------------------------------------------------------------------

def calculate_meal_nutrition(
    components: Sequence[ComponentNutritionEstimate],
    *,
    meal_name: str = "Прием пищи",
    recipe_constraint: RecipeMassConstraint | None = None,
    shared_scale: SharedVisualScaleUncertainty | None = None,
) -> MealNutritionEstimate:
    """
    Aggregates full meal nutrition with recipe constraints and shared scale factors.
    """
    processed = list(components)

    # 1. Apply recipe constraints if given
    if recipe_constraint is not None:
        processed = solve_recipe_mass_constraints(processed, recipe_constraint)

    # 2. Apply shared visual scale uncertainty if given
    if shared_scale is not None and shared_scale.is_shared_across_components:
        processed = apply_shared_visual_scale_uncertainty(processed, shared_scale)

    # 3. Aggregate nutrient intervals across available components
    available_cal = [c.intervals.calories_kcal for c in processed if c.nutrition_available]
    available_prot = [c.intervals.protein_g for c in processed if c.nutrition_available]
    available_fat = [c.intervals.fat_g for c in processed if c.nutrition_available]
    available_carbs = [c.intervals.carbs_g for c in processed if c.nutrition_available]

    total_cal = aggregate_conservative_intervals(available_cal)
    total_prot = aggregate_conservative_intervals(available_prot)
    total_fat = aggregate_conservative_intervals(available_fat)
    total_carbs = aggregate_conservative_intervals(available_carbs)

    total_intervals = MacronutrientIntervals(
        calories_kcal=total_cal,
        protein_g=total_prot,
        fat_g=total_fat,
        carbs_g=total_carbs,
    )

    has_unresolved = any(not c.nutrition_available for c in processed)
    all_unresolved = len(processed) > 0 and all(not c.nutrition_available for c in processed)

    if all_unresolved:
        model_type = UncertaintyModelType.UNRESOLVED
    else:
        model_type = UncertaintyModelType.CONSERVATIVE_INTERVAL

    is_approximate = any(c.is_approximate_plausible_range for c in processed)

    return MealNutritionEstimate(
        meal_name=meal_name,
        components=tuple(processed),
        total_intervals=total_intervals,
        uncertainty_model_type=model_type,
        is_approximate_plausible_range=is_approximate,
        recipe_mass_constraint=recipe_constraint,
        shared_visual_scale_uncertainty=shared_scale,
        has_unresolved_components=has_unresolved,
        overall_confidence=0.60 if has_unresolved else (0.85 if is_approximate else 1.0),
    )


# -----------------------------------------------------------------------------
# User-Facing Range Formatting Utilities
# -----------------------------------------------------------------------------

def format_nutrient_range(
    interval: NutrientInterval | None,
    unit: str = "г",
    *,
    round_digits: int = 0,
) -> str:
    """
    Formats a NutrientInterval into user-facing text.
    - If None: "—"
    - If point: "150 г" (or "150.5 г")
    - If interval: "130–170 г"
    """
    if interval is None:
        return "—"

    if round_digits == 0:
        val_min = round(interval.min_value)
        val_center = round(interval.center_value)
        val_max = round(interval.max_value)
    else:
        val_min = round(interval.min_value, round_digits)
        val_center = round(interval.center_value, round_digits)
        val_max = round(interval.max_value, round_digits)

    if interval.is_point or val_min == val_max:
        return f"{val_center} {unit}".strip()

    return f"{val_min}–{val_max} {unit}".strip()


def format_meal_summary_text(meal: MealNutritionEstimate) -> str:
    """
    Formats the complete MealNutritionEstimate for Telegram messages.
    Follows normative rules:
    - Distinguishes approximate plausible ranges from exact user-confirmed values.
    - Labels partial totals if some components have unresolved KBZHU.
    """
    lines: list[str] = []

    # Title line
    if meal.is_approximate_plausible_range:
        lines.append(f"🍽 {meal.meal_name} (оценка диапазона):")
    else:
        lines.append(f"🍽 {meal.meal_name}:")

    # Components
    for c in meal.components:
        mass_text = (
            f"{c.mass_bounds.grams_center:.0f} г"
            if c.mass_bounds.is_user_confirmed or c.mass_bounds.grams_min == c.mass_bounds.grams_max
            else f"~{c.mass_bounds.grams_min:.0f}–{c.mass_bounds.grams_max:.0f} г"
        )
        if c.nutrition_available and c.intervals.calories_kcal is not None:
            cal_str = format_nutrient_range(c.intervals.calories_kcal, "ккал")
            lines.append(f"• {c.name} ({mass_text}) — {cal_str}")
        else:
            lines.append(f"• {c.name} ({mass_text}) — КБЖУ не определено")

    lines.append("")

    # Totals
    cal_total = format_nutrient_range(meal.total_intervals.calories_kcal, "ккал")
    prot_total = format_nutrient_range(meal.total_intervals.protein_g, "г")
    fat_total = format_nutrient_range(meal.total_intervals.fat_g, "г")
    carbs_total = format_nutrient_range(meal.total_intervals.carbs_g, "г")

    total_label = "Итого (частично)" if meal.has_unresolved_components else "Итого"
    lines.append(f"{total_label}: {cal_total}")
    lines.append(f"Б {prot_total} · Ж {fat_total} · У {carbs_total}")

    if meal.has_unresolved_components:
        lines.append("\n⚠️ Внимание: часть продуктов не определена в справочнике, итог частичный.")

    return "\n".join(lines)
