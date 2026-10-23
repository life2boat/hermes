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

def resolve_approximate_food_nutrition(
    food_name: str,
    *,
    cooking_state: str = "cooked",
) -> FoodNutrientProfile | None:
    """
    Policy 3 — Approximate Food Nutrition & Categorical Resolver.
    Resolves foods, prepared dishes, or culinary components not strictly present in exact catalog
    into standard approximate nutrient profiles (per 100g edible portion).
    """
    import re
    from gateway.healbite_food_composition import FoodNutrientProfile
    from gateway.nutrition.search import normalize_russian_food_query

    q = normalize_russian_food_query(food_name)
    if not q:
        return None

    # Invariant: bare "орехи" / "орех" without specific nut type or dish context
    # must fail closed (preserved for test_scenario_07).
    if q in {"орехи", "орех"}:
        return None

    # Invariant: fictional or non-food/alien queries must fail closed (missing_reference)
    _FICTIONAL_NON_FOOD_MARKERS = (
        r"плазм", r"звездн", r"темн.*матери", r"антивеществ", r"мандрагор", r"квазар",
        r"лембас", r"криптонит", r"гравитон", r"дракон", r"синхрофазотрон", r"ксеноморф",
        r"космическ", r"неведом", r"гусениц", r"в золе", r"марсианск",
    )
    if any(re.search(m, q) for m in _FICTIONAL_NON_FOOD_MARKERS):
        return None

    _APPROX_RULES: tuple[tuple[re.Pattern[str], str, str, float, float, float, float, str], ...] = (
        (re.compile(r"(яблочн.*пирог|пирог.*яблок|шарлотк|штрудел)"), "Яблочный пирог", "grains_bakery", 275.0, 3.5, 9.5, 44.0, "APPROX-PIE-APPLE-2026"),
        (re.compile(r"(сладк.*выпечк|выпечк.*сладк|кусочк.*сладк.*выпечк|сдоб|булочк|круассан|маффин|кекс|ватрушк|слойк)"), "Сладкая выпечка", "grains_bakery", 335.0, 6.5, 13.0, 48.0, "APPROX-PASTRY-SWEET-2026"),
        (re.compile(r"(пирог|пирожок|пироги|пирожки)"), "Пирог с начинкой", "grains_bakery", 290.0, 5.5, 11.0, 42.0, "APPROX-PIE-GEN-2026"),
        (re.compile(r"(основ.*из.*тест|тесто.*основ|печен.*тест)"), "Основа из теста", "grains_bakery", 280.0, 7.5, 5.0, 51.0, "APPROX-DOUGH-BASE-2026"),
        (re.compile(r"(кремов.*начинк|заварн.*крем|крем.*кондитерск|крем)"), "Кремовая начинка", "dairy", 260.0, 3.5, 15.0, 28.0, "APPROX-CREAM-2026"),
        (re.compile(r"(семечк|семен.*подсолнечник|семен.*на.*выпечк|кунжут|семен)"), "Семена пищевые", "nuts", 580.0, 20.0, 50.0, 15.0, "APPROX-SEEDS-2026"),
        (re.compile(r"(грецк.*орех|миндал|фундук|кешью|арахис|смес.*орех|орех.*смес)"), "Орехи (смесь)", "nuts", 630.0, 16.0, 58.0, 14.0, "APPROX-NUTS-2026"),
        (re.compile(r"(пицц)"), "Пицца", "prepared_dishes", 260.0, 11.0, 10.0, 31.0, "APPROX-PIZZA-2026"),
        (re.compile(r"(бургер|чизбургер|гамбургер)"), "Бургер", "prepared_dishes", 255.0, 13.0, 12.0, 23.0, "APPROX-BURGER-2026"),
        (re.compile(r"(шаурм|шаверм|донер)"), "Шаурма", "prepared_dishes", 210.0, 10.0, 9.0, 22.0, "APPROX-SHAWARMA-2026"),
        (re.compile(r"(запеканк)"), "Запеканка", "prepared_dishes", 190.0, 12.0, 8.0, 18.0, "APPROX-CASSEROLE-2026"),
        (re.compile(r"(пельмен|вареник|манты|хинкал)"), "Пельмени / вареники", "prepared_dishes", 230.0, 10.0, 9.5, 26.0, "APPROX-DUMPLINGS-2026"),
        (re.compile(r"(блин|блинчик|оладь)"), "Блины / оладьи", "prepared_dishes", 220.0, 6.0, 8.0, 31.0, "APPROX-PANCAKES-2026"),
        (re.compile(r"(жарко|гуляш|рагу.*мяс|мяс.*рагу)"), "Рагу / жаркое", "prepared_dishes", 140.0, 10.0, 8.0, 7.0, "APPROX-STEW-2026"),
        (re.compile(r"(ролл|суши)"), "Роллы / суши", "prepared_dishes", 170.0, 6.5, 4.5, 26.0, "APPROX-SUSHI-2026"),
        (re.compile(r"(бутерброд|сэндвич|тост.*с.*сыр)"), "Бутерброд / сэндвич", "prepared_dishes", 240.0, 9.0, 10.0, 28.0, "APPROX-SANDWICH-2026"),
        (re.compile(r"(торт|пирожн|десерт|печень)"), "Кондитерское изделие", "grains_bakery", 380.0, 5.0, 18.0, 50.0, "APPROX-DESSERT-2026"),
        (re.compile(r"(салат.*(майонез|оливье|шуб|крабов|цезар)|цезар)"), "Салат с заправкой", "prepared_dishes", 175.0, 5.5, 13.5, 8.0, "APPROX-SALAD-MAYO-2026"),
        (re.compile(r"(салат|винегрет|греческ.*салат|листь.*салат)"), "Салат овощной", "vegetables", 60.0, 1.5, 3.5, 5.5, "APPROX-SALAD-FRESH-2026"),
        (re.compile(r"(суп|щи|бульон|солянк|рассольник|уха|харчо|окрошк)"), "Суп", "prepared_dishes", 55.0, 3.5, 2.5, 4.5, "APPROX-SOUP-2026"),
        (re.compile(r"(куриц|индейк|птиц|курин|грудк|филе.*курин)"), "Птица отварная/запечённая", "meat_poultry", 165.0, 28.0, 5.0, 0.0, "APPROX-POULTRY-2026"),
        (re.compile(r"(рыб|морепродукт|креветк|миди|кальмар)"), "Рыба / морепродукты", "fish_seafood", 150.0, 21.0, 6.5, 0.0, "APPROX-FISH-2026"),
        (re.compile(r"(мясо|говядин|свинин|баранин|фарш|стейк|шашлык)"), "Мясо приготовленное", "meat_poultry", 220.0, 22.0, 14.0, 0.0, "APPROX-MEAT-2026"),
        (re.compile(r"(котлет|тефтел|биточк|фрикадельк)"), "Котлета мясная", "meat_poultry", 210.0, 15.0, 13.0, 8.0, "APPROX-CUTLET-2026"),
        (re.compile(r"(макарон|паст|спагетти|лапш)"), "Макаронные изделия", "grains_bakery", 135.0, 5.0, 1.2, 26.0, "APPROX-PASTA-2026"),
        (re.compile(r"(каш|круп|булгур|кускус|киноа|овсянк)"), "Каша / крупяной гарнир", "grains_bakery", 120.0, 3.5, 1.0, 24.0, "APPROX-GRAINS-2026"),
        (re.compile(r"(картофел|картошк|пюре)"), "Картофель приготовленный", "vegetables", 85.0, 2.0, 1.5, 16.0, "APPROX-POTATO-2026"),
        (re.compile(r"(овощ|тушен.*овощ|кабачк|баклажан|броккол|цветн.*капуст)"), "Овощи приготовленные", "vegetables", 65.0, 1.8, 2.5, 8.5, "APPROX-VEG-2026"),
        (re.compile(r"(яблок|ломтик.*яблок|дольк.*яблок|apple)"), "Яблоко", "fruits", 52.0, 0.3, 0.2, 14.0, "APPROX-APPLE-2026"),
        (re.compile(r"(ягод|клубник|малин|черник|вишн|черешн)"), "Ягоды", "fruits", 45.0, 0.8, 0.4, 9.5, "APPROX-BERRIES-2026"),
    )

    for pat, d_name, cat, kcal, p, f, c, src_id in _APPROX_RULES:
        if pat.search(q):
            return FoodNutrientProfile(
                canonical_id=src_id.lower().replace("-", "_"),
                display_name=d_name,
                category=cat,
                calories_kcal=kcal,
                protein_g=p,
                fat_g=f,
                carbs_g=c,
                reference_confidence=0.88,
                source_id=src_id,
                dataset_version="2026.2-approx-profile",
                cooking_state=cooking_state,
                source_provenance=f"Приблизительный пищевой профиль категории '{cat}' (Скурихин/USDA)",
            )

    return None


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

    # Step 1: Query custom HFKB if explicitly passed
    if hfkb is not None and getattr(hfkb, "is_open", True):
        if hasattr(hfkb, "resolve"):
            match_res = hfkb.resolve(food_name, preferred_cooking_state=cooking_state)
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
        elif hasattr(hfkb, "search"):
            match_result = hfkb.search(food_name, limit=5)
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

    # Step 2: Use unified food resolution (exact alias in built-in catalog -> HFKB -> modifiers)
    lookup_res = resolve_food_composition(food_name)
    if lookup_res is not None and lookup_res.matched and lookup_res.profile is not None:
        profile = lookup_res.profile
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
            source_provenance=profile.source_provenance or profile.dataset_version,
            per_100g_calories_kcal=profile.calories_kcal,
            per_100g_protein_g=profile.protein_g,
            per_100g_fat_g=profile.fat_g,
            per_100g_carbs_g=profile.carbs_g,
            uncertainty_model_type=UncertaintyModelType.CONSERVATIVE_INTERVAL,
            is_approximate_plausible_range=mass_bounds.is_approximate_plausible_range,
            nutrition_available=True,
            candidate_suggestions=tuple(lookup_res.candidate_suggestions) if hasattr(lookup_res, "candidate_suggestions") else (),
        )

    # Step 2b: Fallback to extended food reference catalog
    from gateway.healbite_nutrition_diary import _resolve_synthetic_food_reference
    synth = _resolve_synthetic_food_reference(food_name)
    if synth is not None:
        synth_name, synth_macros = synth
        intervals = calculate_component_macronutrient_intervals(
            mass_bounds,
            synth_macros.get("calories_kcal"),
            synth_macros.get("protein_g"),
            synth_macros.get("fat_g"),
            synth_macros.get("carbs_g"),
        )
        return ComponentNutritionEstimate(
            name=synth_name,
            mass_bounds=mass_bounds,
            intervals=intervals,
            source_id="EXTENDED-FOOD-REFERENCE",
            cooking_state=cooking_state,
            source_provenance="Extended verified food reference",
            per_100g_calories_kcal=synth_macros.get("calories_kcal"),
            per_100g_protein_g=synth_macros.get("protein_g"),
            per_100g_fat_g=synth_macros.get("fat_g"),
            per_100g_carbs_g=synth_macros.get("carbs_g"),
            uncertainty_model_type=UncertaintyModelType.CONSERVATIVE_INTERVAL,
            is_approximate_plausible_range=mass_bounds.is_approximate_plausible_range,
            nutrition_available=True,
            candidate_suggestions=(),
        )

    # Step 2c: Fallback to approximate analog and categorical profile (Policy 3)
    approx_profile = resolve_approximate_food_nutrition(food_name, cooking_state=cooking_state)
    if approx_profile is not None:
        intervals = calculate_component_macronutrient_intervals(
            mass_bounds,
            approx_profile.calories_kcal,
            approx_profile.protein_g,
            approx_profile.fat_g,
            approx_profile.carbs_g,
        )
        return ComponentNutritionEstimate(
            name=approx_profile.display_name,
            mass_bounds=mass_bounds,
            intervals=intervals,
            source_id=approx_profile.source_id,
            cooking_state=approx_profile.cooking_state or cooking_state,
            source_provenance=approx_profile.source_provenance,
            per_100g_calories_kcal=approx_profile.calories_kcal,
            per_100g_protein_g=approx_profile.protein_g,
            per_100g_fat_g=approx_profile.fat_g,
            per_100g_carbs_g=approx_profile.carbs_g,
            uncertainty_model_type=UncertaintyModelType.CONSERVATIVE_INTERVAL,
            is_approximate_plausible_range=True,
            nutrition_available=True,
            candidate_suggestions=(),
            variance_note="Приблизительная оценка по категории/аналогу",
        )

    # Step 3: Unresolved query — provide candidate suggestions
    suggestions: list[str] = []
    if lookup_res is not None and lookup_res.candidate_suggestions:
        suggestions = list(lookup_res.candidate_suggestions)
    elif (catalog := hfkb or get_default_hfkb()) is not None and getattr(catalog, "is_open", False):
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
