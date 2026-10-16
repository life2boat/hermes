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
from gateway.nutrition.manifest import (
    CatalogManifest,
    RuntimeCatalogBinding,
    compute_file_sha256,
    load_and_verify_catalog_binding,
    validate_catalog_manifest,
    verify_catalog_artifact,
    verify_catalog_database_schema,
)

__all__ = [
    "CalibratedQuantileSpec",
    "CatalogManifest",
    "ComponentNutritionEstimate",
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
    "calculate_component_macronutrient_intervals",
    "calculate_conservative_nutrient_interval",
    "compute_file_sha256",
    "ensure_nutrition_diary_v2_schema",
    "extract_macronutrient_intervals_from_row",
    "load_and_verify_catalog_binding",
    "validate_catalog_manifest",
    "verify_catalog_artifact",
    "verify_catalog_database_schema",
]
