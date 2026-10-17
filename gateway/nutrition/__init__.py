"""
gateway.nutrition — Nutrition Intelligence v2.2 Package.

Exports core contracts, uncertainty calculation, catalog manifest and diary schema.
"""

from gateway.nutrition.contracts import (
    CalibratedQuantileSpec,
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
    calculate_conservative_nutrient_interval,
)
from gateway.nutrition.diary_schema_v2 import (
    V2_NUTRITION_DIARY_COLUMNS,
    ensure_nutrition_diary_v2_schema,
    extract_macronutrient_intervals_from_row,
)
from gateway.nutrition.etl import (
    ETLFoodItem,
    build_nutrition_catalog,
    extract_fineli_items,
    extract_skurikhin_items,
    extract_usda_items,
)
from gateway.nutrition.hfkb import (
    HFKBCatalog,
    HFKBFoodItem,
    get_default_hfkb,
    hfkb_item_to_nutrient_profile,
    set_default_hfkb,
)
from gateway.nutrition.manifest import (
    CatalogManifest,
    RuntimeCatalogBinding,
    compute_file_sha256,
    load_and_verify_catalog_binding,
    validate_catalog_manifest,
    verify_catalog_artifact,
    verify_catalog_database_schema,
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
    FoodDensity,
    PortionPrior,
    VisualAnchorSpec,
    VisualAnchorType,
    convert_volume_to_grams,
    estimate_portion_mass,
    get_anchor_spec,
    get_food_density,
    get_portion_prior,
)
from gateway.nutrition.search import (
    extract_stemmed_tokens,
    match_query_against_aliases,
    normalize_russian_food_text,
    reduce_russian_stem,
)

__all__ = [
    "CalibratedQuantileSpec",
    "CatalogManifest",
    "ComponentNutritionEstimate",
    "ETLFoodItem",
    "HFKBCatalog",
    "HFKBFoodItem",
    "MacronutrientIntervals",
    "MassBounds",
    "MealNutritionEstimate",
    "NutrientInterval",
    "RecipeMassConstraint",
    "RuntimeCatalogBinding",
    "SharedVisualScaleUncertainty",
    "UncertaintyModelType",
    "V2_NUTRITION_DIARY_COLUMNS",
    "aggregate_conservative_intervals",
    "build_nutrition_catalog",
    "calculate_component_macronutrient_intervals",
    "calculate_conservative_nutrient_interval",
    "compute_file_sha256",
    "ensure_nutrition_diary_v2_schema",
    "extract_fineli_items",
    "extract_macronutrient_intervals_from_row",
    "extract_skurikhin_items",
    "extract_stemmed_tokens",
    "extract_usda_items",
    "get_default_hfkb",
    "hfkb_item_to_nutrient_profile",
    "load_and_verify_catalog_binding",
    "match_query_against_aliases",
    "normalize_russian_food_text",
    "reduce_russian_stem",
    "set_default_hfkb",
    "FoodDensity",
    "PortionPrior",
    "VisualAnchorSpec",
    "VisualAnchorType",
    "apply_shared_visual_scale_uncertainty",
    "calculate_meal_nutrition",
    "estimate_portion_mass",
    "format_meal_summary_text",
    "format_nutrient_range",
    "get_anchor_spec",
    "get_food_density",
    "get_portion_prior",
    "resolve_and_calculate_component",
    "solve_recipe_mass_constraints",
    "validate_catalog_manifest",
    "verify_catalog_artifact",
    "verify_catalog_database_schema",
]
