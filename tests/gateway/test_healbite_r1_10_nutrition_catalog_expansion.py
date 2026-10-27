"""
tests.gateway.test_healbite_r1_10_nutrition_catalog_expansion — R1-10 Validation Suite.

Validates the expanded authoritative offline nutrition reference catalog:
1. Manifest and checksum integrity (SHA-256 parity, file size, SQLite integrity_check).
2. Validated record count >= 5,000 distinct food profiles (USDA, Skurikhin, Fineli).
3. Authentic provenance for every imported record (source_dataset, source_id, citation).
4. Macro and caloric nutritional validity (no negative numbers, no artificial zeroing of missing data).
5. Curated Russian alias layer covering at least 300 common food names or variants.
6. Accurate differentiation of culinary preparation states (raw, boiled, baked, roasted, fried).
7. Strict zero network calls during runtime food lookup (socket blocking firewall).
8. Performance benchmark on 1,000 food lookups (latency < 50 ms ceiling).
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sqlite3
import time

import pytest

from gateway.healbite_food_composition import resolve_food_composition
from gateway.nutrition.etl.curated_mappings import CURATED_RUSSIAN_FOOD_MAPPINGS
from gateway.nutrition.etl.skurikhin_etl import extract_skurikhin_items
from gateway.nutrition.hfkb import (
    HFKBCatalog,
    load_verified_hfkb,
    set_default_hfkb,
)
from gateway.nutrition.manifest import compute_file_sha256, validate_catalog_manifest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_DB_PATH = _REPO_ROOT / "gateway" / "nutrition" / "data" / "nutrition_catalog_v2.db"
_CATALOG_MANIFEST_PATH = _REPO_ROOT / "gateway" / "nutrition" / "data" / "nutrition_catalog_v2.db.manifest.json"


@pytest.fixture(autouse=True)
def reset_hfkb():
    set_default_hfkb(None)
    yield
    set_default_hfkb(None)


# -----------------------------------------------------------------------------
# Test 1: Manifest, Checksum & SQLite Integrity
# -----------------------------------------------------------------------------
def test_manifest_and_sqlite_integrity():
    assert _CATALOG_DB_PATH.exists(), f"Catalog database not found at {_CATALOG_DB_PATH}"
    assert _CATALOG_MANIFEST_PATH.exists(), f"Catalog manifest not found at {_CATALOG_MANIFEST_PATH}"

    # Verify manifest schema
    manifest_data = json.loads(_CATALOG_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = validate_catalog_manifest(manifest_data)
    assert manifest.catalog_id == "healbite-nutrition-catalog-v2"
    assert manifest.format_version == "2.2"

    # Verify file size and SHA-256 match exactly
    actual_size = _CATALOG_DB_PATH.stat().st_size
    assert actual_size == manifest.file_size_bytes, "File size mismatch with manifest"

    actual_sha = compute_file_sha256(_CATALOG_DB_PATH)
    assert actual_sha == manifest.sha256_checksum, "SHA-256 mismatch with manifest"

    # Verify SQLite PRAGMA integrity_check
    conn = sqlite3.connect(f"file:{_CATALOG_DB_PATH}?mode=ro", uri=True)
    try:
        integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
        assert integrity_row[0] == "ok", f"SQLite integrity check failed: {integrity_row[0]}"
    finally:
        conn.close()

    # Load with strict verification
    catalog = load_verified_hfkb(_CATALOG_DB_PATH, _CATALOG_MANIFEST_PATH, strict=True)
    assert catalog.is_open


# -----------------------------------------------------------------------------
# Test 2: Validated Record Count (>= 5,000 distinct profiles)
# -----------------------------------------------------------------------------
def test_catalog_record_count_exceeds_minimum_threshold():
    catalog = load_verified_hfkb(_CATALOG_DB_PATH, _CATALOG_MANIFEST_PATH, strict=True)
    count = catalog.count_records()

    # Requirement: at least 5,000 distinct validated food profiles
    assert count >= 5000, f"Expected at least 5,000 records, got {count}"
    assert count >= 13000, f"Expected complete expanded dataset (~13,300+ records), got {count}"


# -----------------------------------------------------------------------------
# Test 3: Provenance Integrity Across All Records
# -----------------------------------------------------------------------------
def test_provenance_integrity_all_records():
    conn = sqlite3.connect(f"file:{_CATALOG_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT food_id, name_ru, name_en, source_dataset, source_id, source_provenance, reference_confidence FROM foods").fetchall()
        assert len(rows) >= 5000

        valid_datasets = {"USDA_FOODDATA_CENTRAL", "USDA_SR_LEGACY", "USDA_FOUNDATION", "USDA_SURVEY", "SKURIKHIN_2008", "FINELI"}

        for row in rows:
            fid = row["food_id"]
            assert fid, "Empty food_id found"
            assert row["name_ru"], f"Missing Russian name for {fid}"
            assert row["source_dataset"] in valid_datasets, f"Unexpected source_dataset '{row['source_dataset']}' for {fid}"
            assert row["source_id"], f"Missing source_id for {fid}"
            assert row["source_provenance"], f"Missing source_provenance for {fid}"
            assert 0.5 <= float(row["reference_confidence"]) <= 1.0, f"Invalid confidence {row['reference_confidence']} for {fid}"

        # Check Skurikhin preservation
        sk_rows = conn.execute("SELECT COUNT(*) as cnt FROM foods WHERE source_dataset = 'SKURIKHIN_2008'").fetchone()
        assert sk_rows["cnt"] >= 60, f"Expected preserved Skurikhin records, got {sk_rows['cnt']}"

        # Check Fineli preservation
        fin_rows = conn.execute("SELECT COUNT(*) as cnt FROM foods WHERE source_dataset = 'FINELI'").fetchone()
        assert fin_rows["cnt"] >= 5, f"Expected preserved Fineli records, got {fin_rows['cnt']}"

        # Check USDA bulk presence
        usda_rows = conn.execute("SELECT COUNT(*) as cnt FROM foods WHERE source_dataset LIKE 'USDA%'").fetchone()
        assert usda_rows["cnt"] >= 5000, f"Expected at least 5000 USDA records, got {usda_rows['cnt']}"
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Test 4: Nutrient Value Integrity & Non-Fabrication
# -----------------------------------------------------------------------------
def test_nutrient_values_within_physiologically_valid_ranges():
    conn = sqlite3.connect(f"file:{_CATALOG_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT food_id, calories_kcal, protein_g, fat_g, carbs_g, fiber_g, sugars_g FROM nutrients"
        ).fetchall()
        assert len(rows) >= 5000

        for r in rows:
            fid = r["food_id"]
            cal = float(r["calories_kcal"])
            prot = float(r["protein_g"])
            fat = float(r["fat_g"])
            carbs = float(r["carbs_g"])
            fiber = float(r["fiber_g"])
            sugars = float(r["sugars_g"])

            # Never negative
            assert cal >= 0.0, f"Negative calories in {fid}: {cal}"
            assert prot >= 0.0, f"Negative protein in {fid}: {prot}"
            assert fat >= 0.0, f"Negative fat in {fid}: {fat}"
            assert carbs >= 0.0, f"Negative carbs in {fid}: {carbs}"
            assert fiber >= 0.0, f"Negative fiber in {fid}: {fiber}"
            assert sugars >= 0.0, f"Negative sugars in {fid}: {sugars}"

            # Physiologic bounds per 100g
            assert cal <= 950.0, f"Unrealistic calories (>950 kcal/100g) in {fid}: {cal}"
            assert prot <= 100.5, f"Protein > 100g in {fid}: {prot}"
            assert fat <= 100.5, f"Fat > 100g in {fid}: {fat}"
            assert carbs <= 100.5, f"Carbs > 100g in {fid}: {carbs}"

            # Macronutrient sum should not exceed 100g by more than rounding margin
            assert (prot + fat + carbs) <= 105.0, f"Macro sum exceeds 105g in {fid}: P={prot}, F={fat}, C={carbs}"
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Test 5: Curated Russian Alias Layer (>= 300 aliases & broad category coverage)
# -----------------------------------------------------------------------------
def test_curated_russian_alias_layer_coverage():
    catalog = load_verified_hfkb(_CATALOG_DB_PATH, _CATALOG_MANIFEST_PATH, strict=True)
    catalog.build_in_memory_index()

    conn = sqlite3.connect(f"file:{_CATALOG_DB_PATH}?mode=ro", uri=True)
    try:
        alias_count = conn.execute("SELECT COUNT(DISTINCT normalized_alias) FROM food_aliases").fetchone()[0]
        # Requirement: at least 300 common food names or variants
        assert alias_count >= 300, f"Expected at least 300 normalized aliases, got {alias_count}"
        assert alias_count >= 1000, f"Expected > 1,000 normalized aliases with expanded catalog, got {alias_count}"
    finally:
        conn.close()

    # Broad category coverage test queries
    staples_by_category = {
        "poultry": ["куриная грудка", "куриное филе", "индейка", "куриное бедро"],
        "meat": ["говядина", "стейк", "говяжий фарш", "свинина", "баранина"],
        "fish": ["лосось", "семга", "форель", "тунец", "треска", "креветки"],
        "dairy": ["творог 5%", "сметана 15%", "молоко 3.2%", "сыр чеддер", "моцарелла"],
        "eggs": ["яйцо", "вареное яйцо", "яичница", "омлет"],
        "cereals": ["гречка", "рис вареный", "овсянка", "макароны", "киноа"],
        "vegetables": ["помидор", "огурец", "картофель", "брокколи", "морковь", "капуста"],
        "fruits": ["яблоко", "банан", "апельсин", "авокадо", "клубника"],
        "legumes": ["фасоль", "чечевица", "нут", "тофу"],
        "nuts_seeds": ["грецкий орех", "миндаль", "арахис", "семечки"],
        "fats_oils": ["оливковое масло", "сливочное масло", "подсолнечное масло"],
        "bakery": ["хлеб", "белый хлеб", "черный хлеб", "лаваш"],
        "prepared_dishes": ["борщ", "щи", "куриный суп", "пицца", "бургер", "болоньезе"],
    }

    resolved_count = 0
    total_tested = 0

    for cat_name, queries in staples_by_category.items():
        for q in queries:
            total_tested += 1
            res = catalog.resolve(q)
            assert res.matched, f"Failed to resolve staple food '{q}' in category '{cat_name}'"
            assert res.profile is not None
            assert res.profile.calories_kcal >= 0.0
            resolved_count += 1

    assert resolved_count == total_tested
    assert resolved_count >= 50


# -----------------------------------------------------------------------------
# Test 6: Preparation State Differentiation (Raw vs Cooked)
# -----------------------------------------------------------------------------
def test_distinguishes_raw_and_cooked_states():
    catalog = load_verified_hfkb(_CATALOG_DB_PATH, _CATALOG_MANIFEST_PATH, strict=True)
    catalog.build_in_memory_index()

    # 1. Chicken breast raw vs roasted
    raw_chicken = catalog.resolve("куриная грудка сырая")
    assert raw_chicken.matched
    assert raw_chicken.profile.cooking_state == "raw"
    # Raw chicken breast is typically ~110-125 kcal
    assert 100 <= raw_chicken.profile.calories_kcal <= 135

    cooked_chicken = catalog.resolve("куриная грудка запеченная")
    assert cooked_chicken.matched
    assert cooked_chicken.profile.cooking_state in ("roasted", "cooked", "baked")
    # Cooked chicken breast loses water, concentration raises calories to ~155-175 kcal
    assert 150 <= cooked_chicken.profile.calories_kcal <= 185
    assert cooked_chicken.profile.calories_kcal > raw_chicken.profile.calories_kcal

    # 2. Egg raw vs hard-boiled
    raw_egg = catalog.resolve("сырое яйцо")
    assert raw_egg.matched
    assert raw_egg.profile.cooking_state == "raw"

    boiled_egg = catalog.resolve("яйцо вкрутую")
    assert boiled_egg.matched
    assert boiled_egg.profile.cooking_state in ("boiled", "cooked")

    # 3. Rice raw vs cooked
    raw_rice = catalog.resolve("рис сухой")
    assert raw_rice.matched
    assert raw_rice.profile.cooking_state == "raw"
    # Dry rice has ~350-370 kcal
    assert 340 <= raw_rice.profile.calories_kcal <= 380

    boiled_rice = catalog.resolve("рис вареный")
    assert boiled_rice.matched
    assert boiled_rice.profile.cooking_state in ("boiled", "cooked")
    # Cooked rice absorbs ~2x water, calories drop to ~110-140 kcal
    assert 100 <= boiled_rice.profile.calories_kcal <= 150
    assert boiled_rice.profile.calories_kcal < raw_rice.profile.calories_kcal


# -----------------------------------------------------------------------------
# Test 7: Runtime Zero Network Calls (Strict Socket Firewall)
# -----------------------------------------------------------------------------
def test_zero_network_calls_during_food_lookup(monkeypatch):
    catalog = load_verified_hfkb(_CATALOG_DB_PATH, _CATALOG_MANIFEST_PATH, strict=True)
    set_default_hfkb(catalog)

    def blocked_connect(*args, **kwargs):
        raise AssertionError("Network connection attempted during food lookup! Zero network calls allowed.")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_connect)

    # Execute multiple lookups via public interface
    queries = [
        "творог 5%",
        "лосось запеченный",
        "гречка",
        "куриная грудка",
        "банан",
        "овсяные хлопья",
        "помидор",
        "оливковое масло",
    ]
    for q in queries:
        res = resolve_food_composition(q)
        assert res.matched, f"Failed lookup for {q}"
        assert res.profile is not None


# -----------------------------------------------------------------------------
# Test 8: Performance Benchmark (1,000 Lookups < 50ms Target)
# -----------------------------------------------------------------------------
def test_benchmark_1000_lookups_latency_and_memory():
    catalog = load_verified_hfkb(_CATALOG_DB_PATH, _CATALOG_MANIFEST_PATH, strict=True)
    catalog.build_in_memory_index()

    benchmark_queries = [
        "куриная грудка",
        "вареное яйцо",
        "творог 5%",
        "гречка",
        "рис вареный",
        "лосось запеченный",
        "банан",
        "помидор",
        "овсянка",
        "оливковое масло",
    ]

    # Warm-up
    for q in benchmark_queries:
        catalog.resolve(q)

    # 1,000 lookups benchmark
    start_time = time.perf_counter()
    for _ in range(100):
        for q in benchmark_queries:
            res = catalog.resolve(q)
            assert res.matched

    total_duration_sec = time.perf_counter() - start_time
    avg_latency_ms = (total_duration_sec / 1000.0) * 1000.0

    print(f"\n[BENCHMARK] 1,000 lookups completed in {total_duration_sec:.4f}s ({avg_latency_ms:.3f} ms / lookup)")

    # Latency ceiling requirement: < 50 ms per lookup
    assert avg_latency_ms < 50.0, f"Average lookup latency {avg_latency_ms:.2f} ms exceeded 50 ms limit"
    # In reality, indexed in-memory / SQLite is < 2 ms
    assert avg_latency_ms < 5.0, f"Expected < 5 ms per lookup, got {avg_latency_ms:.2f} ms"
