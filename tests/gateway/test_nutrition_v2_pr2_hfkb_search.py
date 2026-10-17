"""
tests.gateway.test_nutrition_v2_pr2_hfkb_search — Test suite for Nutrition Intelligence v2.2 PR-2.

Verifies:
1. Russian food name normalization, tokenization, stemming, and multi-stage search.
2. USDA FoodData Central, Fineli, and Skurikhin ETL pipelines.
3. HFKB SQLite database construction, indexing, and deterministic query resolution.
4. Manifest generation and verification.
5. Integration and fallback with healbite_food_composition.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from gateway.healbite_food_composition import resolve_food_composition
from gateway.nutrition.etl.catalog_builder import build_nutrition_catalog
from gateway.nutrition.etl.fineli_etl import extract_fineli_items
from gateway.nutrition.etl.skurikhin_etl import extract_skurikhin_items
from gateway.nutrition.etl.usda_etl import extract_usda_items
from gateway.nutrition.hfkb import (
    HFKBCatalog,
    get_default_hfkb,
    set_default_hfkb,
)
from gateway.nutrition.manifest import load_and_verify_catalog_binding
from gateway.nutrition.search import (
    extract_stemmed_tokens,
    match_query_against_aliases,
    normalize_russian_food_text,
    reduce_russian_stem,
)


# =============================================================================
# 1. Russian Food Search & Normalization Engine Tests
# =============================================================================

def test_normalize_russian_food_text():
    # Yo -> e, lowercase, punctuation strip, whitespace collapse
    assert normalize_russian_food_text("Творог 5% (варёный)!") == "творог 5 вареный"
    assert normalize_russian_food_text("  Зелёное   ЯБЛОКО  ") == "зеленое яблоко"
    assert normalize_russian_food_text("") == ""


def test_reduce_russian_stem():
    assert reduce_russian_stem("куриная") == "курин"
    assert reduce_russian_stem("куриное") == "курин"
    assert reduce_russian_stem("вареная") == "варен"
    assert reduce_russian_stem("вареный") == "варен"
    assert reduce_russian_stem("яблоки") == "яблок"
    assert reduce_russian_stem("сыр") == "сыр"  # Short word preserved


def test_extract_stemmed_tokens():
    tokens = extract_stemmed_tokens("Отварная куриная грудка")
    assert "курин" in tokens
    assert "грудк" in tokens
    assert "отварн" in tokens


def test_match_query_against_aliases_multi_stage():
    alias_map = {
        "куриная грудка": "chicken-breast-raw",
        "куриное филе": "chicken-breast-raw",
        "яблоко": "apple",
    }
    id_to_name = {
        "chicken-breast-raw": "Куриная грудка",
        "apple": "Яблоко",
    }
    id_to_stems = {
        "chicken-breast-raw": ("грудк", "курин"),
        "apple": ("яблок",),
    }

    # Stage 1: Exact
    target, conf, stage, _ = match_query_against_aliases(
        "Куриная грудка",
        alias_map,
        id_to_name,
        id_to_stems,
    )
    assert target == "chicken-breast-raw"
    assert stage == "exact"
    assert conf == 1.0

    # Stage 2: Token set (inverted word order)
    target, conf, stage, _ = match_query_against_aliases(
        "грудка куриная",
        alias_map,
        id_to_name,
        id_to_stems,
    )
    assert target == "chicken-breast-raw"
    assert stage == "token_set"
    assert conf >= 0.70

    # Stage 3: Substring
    target, conf, stage, _ = match_query_against_aliases(
        "спелое яблоко свежее",
        alias_map,
        id_to_name,
        id_to_stems,
    )
    assert target == "apple"
    assert stage in ("substring", "token_set")

    # Stage 4: Fuzzy matching typo
    target, conf, stage, _ = match_query_against_aliases(
        "курриная грудка",
        alias_map,
        id_to_name,
        id_to_stems,
    )
    assert target == "chicken-breast-raw"
    assert stage in ("fuzzy", "token_set")

    # Unresolved with candidate suggestions
    target, conf, stage, suggestions = match_query_against_aliases(
        "яблочный пирог",
        alias_map,
        id_to_name,
        id_to_stems,
    )
    assert target is None
    assert stage == "none"
    assert "Яблоко" in suggestions


# =============================================================================
# 2. ETL Extractor Tests
# =============================================================================

def test_usda_etl_extractor():
    items = extract_usda_items()
    assert len(items) >= 15

    chicken_raw = next((item for item in items if item.source_id == "USDA-FDC-171077"), None)
    assert chicken_raw is not None
    assert chicken_raw.calories_kcal == 120.0
    assert chicken_raw.protein_g == 22.5
    assert chicken_raw.cooking_state == "raw"
    assert "куриная грудка" in chicken_raw.aliases
    assert len(chicken_raw.portions) >= 1

    chicken_cooked = next((item for item in items if item.source_id == "USDA-FDC-171477"), None)
    assert chicken_cooked is not None
    assert chicken_cooked.calories_kcal == 165.0
    assert chicken_cooked.protein_g == 31.0
    assert chicken_cooked.cooking_state == "roasted"


def test_fineli_etl_extractor():
    items = extract_fineli_items()
    assert len(items) >= 5

    rye_bread = next((item for item in items if item.source_id == "FINELI-10"), None)
    assert rye_bread is not None
    assert rye_bread.name_ru == "Ржаной хлеб"
    assert rye_bread.calories_kcal == 218.0
    assert rye_bread.fiber_g == 11.2
    assert "ржаной хлеб" in rye_bread.aliases


def test_skurikhin_etl_extractor():
    items = extract_skurikhin_items()
    assert len(items) >= 10

    # Ensure fat percentages are strictly distinguished
    tvorog_5 = next((item for item in items if item.source_id == "INTERNAL-SKURIKHIN-2008-0112"), None)
    tvorog_9 = next((item for item in items if item.source_id == "INTERNAL-SKURIKHIN-2008-0113"), None)
    assert tvorog_5 is not None
    assert tvorog_9 is not None
    assert tvorog_5.fat_g == 5.0
    assert tvorog_9.fat_g == 9.0
    assert tvorog_5.calories_kcal != tvorog_9.calories_kcal

    smetana_15 = next((item for item in items if item.source_id == "INTERNAL-SKURIKHIN-2008-0125"), None)
    smetana_20 = next((item for item in items if item.source_id == "INTERNAL-SKURIKHIN-2008-0126"), None)
    assert smetana_15.fat_g == 15.0
    assert smetana_20.fat_g == 20.0


# =============================================================================
# 3. HFKB Catalog Construction & Manifest Tests
# =============================================================================

def test_build_nutrition_catalog_and_manifest(tmp_path: Path):
    db_path = tmp_path / "test_nutrition_catalog_v2.db"
    manifest_path = tmp_path / "test_nutrition_catalog_v2.manifest.json"

    catalog, manifest = build_nutrition_catalog(
        output_db_path=db_path,
        output_manifest_path=manifest_path,
    )

    assert db_path.exists()
    assert manifest_path.exists()
    assert manifest.record_count > 30
    assert manifest.file_size_bytes == db_path.stat().st_size
    assert len(manifest.sha256_checksum) == 64

    # Verify runtime binding
    binding = load_and_verify_catalog_binding(db_path, manifest_path)
    assert binding.is_verified is True
    assert binding.verification_error is None

    # Query items from catalog
    res_chicken = catalog.resolve("куриная грудка запеченная")
    assert res_chicken.matched is True
    assert res_chicken.profile is not None
    assert res_chicken.profile.calories_kcal == 165.0
    assert res_chicken.source_id == "USDA-FDC-171477"

    res_tvorog = catalog.resolve("творог 9%")
    assert res_tvorog.matched is True
    assert res_tvorog.profile is not None
    assert res_tvorog.profile.fat_g == 9.0
    assert res_tvorog.source_id == "INTERNAL-SKURIKHIN-2008-0113"


# =============================================================================
# 4. healbite_food_composition Fallback & Integration Tests
# =============================================================================

def test_resolve_food_composition_with_and_without_hfkb(tmp_path: Path):
    # 1. Unbound HFKB fallback to built-in catalog
    set_default_hfkb(None)
    fallback_res = resolve_food_composition("яблоко")
    assert fallback_res.matched is True
    assert fallback_res.profile is not None
    assert fallback_res.profile.display_name == "Яблоко"

    # 2. Bound HFKB
    db_path = tmp_path / "hfkb_test.db"
    catalog, _ = build_nutrition_catalog(output_db_path=db_path)
    set_default_hfkb(catalog)

    bound_res = resolve_food_composition("ржаной хлеб")
    assert bound_res.matched is True
    assert bound_res.profile is not None
    assert bound_res.profile.display_name == "Ржаной хлеб"
    assert bound_res.source_id == "FINELI-10"

    # Clean up global state
    set_default_hfkb(None)
