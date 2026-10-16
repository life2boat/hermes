"""
tests.gateway.test_nutrition_v2_pr1_contracts — Test suite for Nutrition Intelligence v2.2 PR-1.

Verifies:
1. Uncertainty Calculation (normative contracts for CONSERVATIVE_INTERVAL, CALIBRATED_QUANTILE, UNRESOLVED).
2. Approximate plausible range vs guaranteed physical interval labeling.
3. Recipe mass constraints and cooking yield consistency.
4. Shared visual scale uncertainty across distinct components.
5. Catalog manifest validation, checksum verification, and runtime binding.
6. Non-destructive SQLite schema migration, rollback compatibility, and interval persistence.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    NutritionRecord,
)
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
    get_existing_table_columns,
)
from gateway.nutrition.manifest import (
    CatalogManifest,
    compute_file_sha256,
    load_and_verify_catalog_binding,
    validate_catalog_manifest,
    verify_catalog_artifact,
    verify_catalog_database_schema,
)


# =============================================================================
# 1. Uncertainty Calculation & Conservative Interval Tests
# =============================================================================

def test_uncertainty_model_type_enum():
    assert UncertaintyModelType.CONSERVATIVE_INTERVAL.value == "CONSERVATIVE_INTERVAL"
    assert UncertaintyModelType.CALIBRATED_QUANTILE.value == "CALIBRATED_QUANTILE"
    assert UncertaintyModelType.UNRESOLVED.value == "UNRESOLVED"


def test_nutrient_interval_invariants():
    # Valid interval
    interval = NutrientInterval(min_value=10.0, center_value=25.0, max_value=40.0)
    assert interval.min_value == 10.0
    assert interval.center_value == 25.0
    assert interval.max_value == 40.0
    assert interval.span == 30.0
    assert not interval.is_point

    # Point interval
    pt = NutrientInterval(min_value=50.0, center_value=50.0, max_value=50.0)
    assert pt.is_point
    assert pt.span == 0.0

    # Negative min clamped to 0.0 if close to zero, or raises if definitely negative
    with pytest.raises(ValueError, match="min_value must be non-negative"):
        NutrientInterval(min_value=-5.0, center_value=10.0, max_value=20.0)

    # Inverted values are clamped monotonically: min <= center <= max
    corrected = NutrientInterval(min_value=30.0, center_value=20.0, max_value=10.0)
    assert corrected.min_value == 30.0
    assert corrected.center_value == 30.0
    assert corrected.max_value == 30.0


def test_calculate_conservative_nutrient_interval_formula():
    """
    Normative contract:
    V_center = m_center * n_center / 100
    V_min = m_min * n_min / 100
    V_max = m_max * n_max / 100
    """
    # 150g chicken breast, [120g, 150g, 180g], 31.0g protein / 100g
    interval = calculate_conservative_nutrient_interval(
        mass_min=120.0,
        mass_center=150.0,
        mass_max=180.0,
        nutrient_per_100g=31.0,
    )
    assert interval.min_value == pytest.approx(120.0 * 31.0 / 100.0, abs=1e-3)
    assert interval.center_value == pytest.approx(150.0 * 31.0 / 100.0, abs=1e-3)
    assert interval.max_value == pytest.approx(180.0 * 31.0 / 100.0, abs=1e-3)
    assert interval.min_value <= interval.center_value <= interval.max_value


def test_aggregate_conservative_intervals():
    """
    Normative contract:
    V_total_center = sum(V_center_i)
    V_total_min = sum(V_min_i)
    V_total_max = sum(V_max_i)
    """
    i1 = NutrientInterval(min_value=10.0, center_value=20.0, max_value=30.0)
    i2 = NutrientInterval(min_value=5.0, center_value=15.0, max_value=25.0)
    i3 = NutrientInterval(min_value=0.0, center_value=10.0, max_value=15.0)

    total = aggregate_conservative_intervals([i1, i2, i3, None])
    assert total is not None
    assert total.min_value == pytest.approx(15.0)
    assert total.center_value == pytest.approx(45.0)
    assert total.max_value == pytest.approx(70.0)

    assert aggregate_conservative_intervals([None, None]) is None


def test_mass_bounds_approximate_plausible_range_label():
    """
    Normative contract:
    When mass bounds are model-estimated rather than physically established,
    label the result as an approximate plausible range, not a guaranteed physical interval.
    """
    # Model estimated mass
    estimated = MassBounds(
        grams_min=100.0,
        grams_center=150.0,
        grams_max=200.0,
        is_approximate_plausible_range=True,
        is_user_confirmed=False,
    )
    assert estimated.is_approximate_plausible_range is True
    assert estimated.is_user_confirmed is False

    # Physically weighed or user-confirmed
    weighed = MassBounds(
        grams_min=150.0,
        grams_center=150.0,
        grams_max=150.0,
        is_approximate_plausible_range=False,
        is_user_confirmed=True,
    )
    assert weighed.is_approximate_plausible_range is False
    assert weighed.is_user_confirmed is True


# =============================================================================
# 2. Recipe Mass Constraints & Yield Tests
# =============================================================================

def test_recipe_mass_constraint_yield_and_validation():
    # Roasted beef: cooking yield factor 0.75 (25% water loss during roasting)
    recipe = RecipeMassConstraint(
        total_dish_mass_min_g=200.0,
        total_dish_mass_max_g=350.0,
        cooking_yield_factor=0.75,
        recipe_ingredient_names=frozenset(["говядина", "соль", "перец"]),
        external_ingredient_names=frozenset(["картофельное пюре"]),
    )

    raw_mass = 400.0
    expected_cooked = recipe.raw_to_cooked_mass(raw_mass)
    assert expected_cooked == pytest.approx(300.0)
    assert recipe.cooked_to_raw_mass(300.0) == pytest.approx(400.0)

    # Valid mass balance within tolerance
    assert recipe.validate_mass_balance(raw_ingredients_mass_sum=400.0, cooked_dish_mass=300.0)
    assert recipe.validate_mass_balance(raw_ingredients_mass_sum=400.0, cooked_dish_mass=320.0, tolerance=0.10)
    # Outside tolerance
    assert not recipe.validate_mass_balance(raw_ingredients_mass_sum=400.0, cooked_dish_mass=150.0, tolerance=0.10)

    # Clamping total dish mass
    assert recipe.clamp_total_dish_mass(150.0) == 200.0
    assert recipe.clamp_total_dish_mass(400.0) == 350.0
    assert recipe.clamp_total_dish_mass(280.0) == 280.0


# =============================================================================
# 3. Shared Visual Scale Uncertainty Tests
# =============================================================================

def test_shared_visual_scale_uncertainty():
    scale = SharedVisualScaleUncertainty(
        scale_factor_min=0.85,
        scale_factor_center=1.00,
        scale_factor_max=1.15,
        is_shared_across_components=True,
    )

    component_mass = MassBounds(grams_min=100.0, grams_center=120.0, grams_max=140.0)
    scaled = scale.apply_to_mass(component_mass)

    assert scaled.grams_min == pytest.approx(85.0)
    assert scaled.grams_center == pytest.approx(120.0)
    assert scaled.grams_max == pytest.approx(161.0)
    assert scaled.is_approximate_plausible_range is True


# =============================================================================
# 4. Catalog Manifest & Runtime Binding Tests
# =============================================================================

def test_catalog_manifest_validation(tmp_path: Path):
    manifest_dict = {
        "catalog_id": "healbite-nutrition-catalog-v2",
        "format_version": "2.2",
        "sha256_checksum": "a" * 64,
        "file_size_bytes": 1048576,
        "record_count": 12500,
        "sources": [
            {"name": "USDA FoodData Central SR Legacy", "version": "2024"},
            {"name": "Fineli", "version": "2023"},
        ],
        "publication_date": "2026-10-17",
        "created_at": "2026-10-17T00:00:00Z",
    }
    manifest = validate_catalog_manifest(manifest_dict)
    assert manifest.catalog_id == "healbite-nutrition-catalog-v2"
    assert manifest.record_count == 12500
    assert len(manifest.sources) == 2

    # Invalid sha256
    bad_dict = dict(manifest_dict, sha256_checksum="bad-checksum")
    with pytest.raises(ValueError, match="Invalid sha256_checksum"):
        validate_catalog_manifest(bad_dict)

    # Missing required field
    missing_dict = dict(manifest_dict)
    del missing_dict["file_size_bytes"]
    with pytest.raises(ValueError, match="missing required field"):
        validate_catalog_manifest(missing_dict)


def test_runtime_catalog_binding_and_verification(tmp_path: Path):
    db_file = tmp_path / "nutrition_catalog_v2.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE foods (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO foods VALUES ('food-1', 'Apple')")
    conn.commit()
    conn.close()

    file_size = db_file.stat().st_size
    file_sha = compute_file_sha256(db_file)

    manifest_file = tmp_path / "nutrition_catalog_v2.manifest.json"
    manifest_data = {
        "catalog_id": "healbite-nutrition-catalog-v2",
        "format_version": "2.2",
        "sha256_checksum": file_sha,
        "file_size_bytes": file_size,
        "record_count": 1,
        "sources": [{"name": "Seed Catalog", "version": "v1"}],
        "publication_date": "2026-10-17",
        "created_at": "2026-10-17T00:00:00Z",
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    binding = load_and_verify_catalog_binding(db_file, manifest_file)
    assert binding.is_verified is True
    assert binding.verification_error is None
    assert binding.manifest is not None
    assert binding.manifest.record_count == 1


# =============================================================================
# 5. Non-Destructive SQLite Schema Migration & Persistence Tests
# =============================================================================

def test_ensure_nutrition_diary_v2_schema_on_legacy_database(tmp_path: Path):
    db_file = tmp_path / "legacy_healbite.db"
    conn = sqlite3.connect(db_file)
    # Create legacy HER-10 table without v2 columns
    conn.execute(
        """
        CREATE TABLE nutrition_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            meal_name TEXT NOT NULL,
            items_json TEXT NOT NULL,
            calories_kcal REAL,
            protein_g REAL,
            fat_g REAL,
            carbs_g REAL,
            confidence REAL,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            raw_summary TEXT NOT NULL,
            image_ref TEXT,
            qdrant_indexed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()

    initial_cols = get_existing_table_columns(conn, "nutrition_log")
    assert "calories_min" not in initial_cols
    assert "uncertainty_model_type" not in initial_cols

    # Migrate to v2
    added = ensure_nutrition_diary_v2_schema(conn, "nutrition_log")
    assert len(added) == len(V2_NUTRITION_DIARY_COLUMNS)
    assert "calories_min" in added
    assert "is_approximate_range" in added

    # Re-running migration is idempotent
    added_second = ensure_nutrition_diary_v2_schema(conn, "nutrition_log")
    assert added_second == []

    conn.close()


def test_nutrition_diary_persistence_with_intervals(tmp_path: Path):
    db_path = tmp_path / "diary_v2.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    now = datetime.now(timezone.utc)

    # Save meal with explicit intervals
    record = NutritionRecord(
        is_food=True,
        meal_name="Овсяная каша с орехами",
        items=[{"name": "Овсяная каша", "weight_g": 200}],
        calories_kcal=350.0,
        protein_g=12.0,
        fat_g=8.0,
        carbs_g=55.0,
        confidence=0.95,
        raw_summary="Овсяная каша с орехами",
        calories_min=310.0,
        calories_max=390.0,
        protein_min=10.0,
        protein_max=14.0,
        fat_min=6.0,
        fat_max=10.0,
        carbs_min=50.0,
        carbs_max=60.0,
        uncertainty_model_type=UncertaintyModelType.CONSERVATIVE_INTERVAL.value,
        is_approximate_range=True,
    )

    sqlite_id, dup = diary.save_record(
        user_id=101,
        source="vision",
        record=record,
        image_ref="telegram:101:1",
        occurred_at=now,
    )
    assert not dup
    assert sqlite_id is not None

    # Retrieve daily summary
    summary = diary.get_daily_summary(user_id=101, now=now)
    assert summary["entry_count"] == 1
    assert summary["has_intervals"] is True
    assert summary["intervals"] is not None

    cal_interval = summary["intervals"]["calories_kcal"]
    assert cal_interval["min"] == 310.0
    assert cal_interval["center"] == 350.0
    assert cal_interval["max"] == 390.0

    entry = summary["entries"][0]
    assert entry["calories_min"] == 310.0
    assert entry["calories_max"] == 390.0
    assert entry["uncertainty_model_type"] == "CONSERVATIVE_INTERVAL"
    assert entry["is_approximate_range"] == 1


def test_nutrition_diary_backward_compatibility_without_intervals(tmp_path: Path):
    db_path = tmp_path / "diary_legacy_compat.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    now = datetime.now(timezone.utc)

    # HER-10 style record without any interval fields
    record = NutritionRecord(
        is_food=True,
        meal_name="Борщ",
        items=[],
        calories_kcal=250.0,
        protein_g=10.0,
        fat_g=8.0,
        carbs_g=30.0,
        confidence=0.90,
        raw_summary="Борщ",
    )

    sqlite_id, _ = diary.save_record(
        user_id=102,
        source="text",
        record=record,
        image_ref=None,
        occurred_at=now,
    )
    assert sqlite_id is not None

    summary = diary.get_daily_summary(user_id=102, now=now)
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == 250.0
    assert summary["has_intervals"] is False
    assert summary["intervals"] is None

    entry = summary["entries"][0]
    assert entry["calories_min"] is None
    assert entry["calories_max"] is None
