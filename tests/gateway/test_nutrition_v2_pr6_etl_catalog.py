"""
tests.gateway.test_nutrition_v2_pr6_etl_catalog — PR-6 Validation Suite.

Covers:
1. Ingestion of real-format USDA and Fineli data into HFKB.
2. Strict verification mode: load_verified_hfkb(strict=True) on genuine catalog.
3. Strict verification failure on 1-byte file tampering.
4. Strict verification failure on corrupt or missing manifest.
5. Russian food composition resolution against verified catalog with 0 network calls.
6. Query performance meets latency requirement (< 50 ms for typical lookups).
7. Fallback path when full catalog is absent in development.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import socket
import time

import pytest

from gateway.nutrition.etl.catalog_builder import build_nutrition_catalog
from gateway.nutrition.etl.fineli_etl import extract_fineli_items
from gateway.nutrition.etl.usda_etl import extract_usda_items
from gateway.nutrition.hfkb import (
    CatalogVerificationError,
    HFKBCatalog,
    get_default_hfkb,
    load_verified_hfkb,
    set_default_hfkb,
)


@pytest.fixture(autouse=True)
def reset_global_hfkb():
    yield
    set_default_hfkb(None)


# -----------------------------------------------------------------------------
# Test 1: Ingest real-format USDA and Fineli data into HFKB
# -----------------------------------------------------------------------------
def test_ingest_real_format_usda_and_fineli_data(tmp_path):
    # 1. Realistic USDA FoodData Central JSON export format
    usda_raw_json = [
        {
            "fdcId": 990001,
            "description": "Turkey breast, meat only, cooked, roasted",
            "name_ru": "Индейка филе запеченное",
            "foodCategory": {"description": "Poultry Products"},
            "cooking_state": "roasted",
            "foodNutrients": [
                {"nutrientName": "Energy", "amount": 135.0, "unitName": "KCAL", "nutrientId": 1008},
                {"nutrientName": "Protein", "amount": 30.0, "unitName": "G", "nutrientId": 1003},
                {"nutrientName": "Total lipid (fat)", "amount": 1.5, "unitName": "G", "nutrientId": 1004},
                {"nutrientName": "Carbohydrate, by difference", "amount": 0.0, "unitName": "G", "nutrientId": 1005},
                {"nutrientName": "Fiber, total dietary", "amount": 0.0, "unitName": "G", "nutrientId": 1079},
                {"nutrientName": "Sugars, total including NLEA", "amount": 0.0, "unitName": "G", "nutrientId": 2000},
            ],
            "aliases": ["филе индейки", "индейка грудка", "запеченная индейка"],
            "portions": [["1 порция", 150.0]],
        }
    ]
    usda_json_path = tmp_path / "usda_real.json"
    usda_json_path.write_text(json.dumps(usda_raw_json, ensure_ascii=False), encoding="utf-8")

    # 2. Realistic Fineli JSON export format
    fineli_raw_json = [
        {
            "fineli_id": 991001,
            "name_en": "Karelian pasty, rye crust, rice filling",
            "name_ru": "Калитка с рисом",
            "category": "bakery",
            "cooking_state": "baked",
            "energyKcal": 220.0,
            "protein": 5.0,
            "fat": 6.5,
            "carbohydrate": 35.0,
            "fiber": 2.5,
            "sugar": 1.0,
            "aliases": ["калитка", "карельский пирожок"],
            "portions": [["1 шт", 70.0]],
        }
    ]
    fineli_json_path = tmp_path / "fineli_real.json"
    fineli_json_path.write_text(json.dumps(fineli_raw_json, ensure_ascii=False), encoding="utf-8")

    # 3. Realistic USDA CSV format
    usda_csv_path = tmp_path / "usda_real.csv"
    with usda_csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["fdc_id", "description", "name_ru", "category", "calories", "protein", "fat", "carbohydrates", "fiber", "sugars", "aliases"],
        )
        writer.writeheader()
        writer.writerow({
            "fdc_id": "990002",
            "description": "Beef, flank steak, lean, braised",
            "name_ru": "Говядина фланк тушеная",
            "category": "meat",
            "calories": "190.0",
            "protein": "28.0",
            "fat": "8.0",
            "carbohydrates": "0.0",
            "fiber": "0.0",
            "sugars": "0.0",
            "aliases": "фланк стейк;говядина фланк;тушеная говядина",
        })

    # Test individual extractions
    usda_items_json = extract_usda_items(usda_json_path)
    assert len(usda_items_json) == 1
    assert usda_items_json[0].food_id == "usda-990001"
    assert usda_items_json[0].calories_kcal == 135.0
    assert usda_items_json[0].protein_g == 30.0
    assert "филе индейки" in usda_items_json[0].aliases

    usda_items_csv = extract_usda_items(usda_csv_path)
    assert len(usda_items_csv) == 1
    assert usda_items_csv[0].food_id == "usda-990002"
    assert usda_items_csv[0].calories_kcal == 190.0

    fineli_items = extract_fineli_items(fineli_json_path)
    assert len(fineli_items) == 1
    assert fineli_items[0].food_id == "fineli-991001"
    assert fineli_items[0].carbs_g == 35.0

    # Build catalog incorporating custom real extractions
    out_db = tmp_path / "integrated_catalog.db"
    out_manifest = tmp_path / "integrated_catalog.manifest.json"
    catalog, manifest = build_nutrition_catalog(
        output_db_path=out_db,
        output_manifest_path=out_manifest,
        usda_source_path=usda_json_path,
        fineli_source_path=fineli_json_path,
    )

    assert catalog.count_records() > 0
    assert manifest.record_count == catalog.count_records()
    assert out_db.exists()
    assert out_manifest.exists()

    # Verify lookups from custom ingested records
    catalog.build_in_memory_index()
    res_usda = catalog.resolve("индейка филе")
    assert res_usda.matched is True
    assert res_usda.profile.calories_kcal == 135.0

    res_fineli = catalog.resolve("калитка")
    assert res_fineli.matched is True
    assert res_fineli.profile.calories_kcal == 220.0


# -----------------------------------------------------------------------------
# Test 2: Strict verification succeeds on genuine catalog
# -----------------------------------------------------------------------------
def test_strict_verification_succeeds_on_genuine_catalog(tmp_path):
    db_path = tmp_path / "genuine_catalog.db"
    manifest_path = tmp_path / "genuine_catalog.manifest.json"

    build_nutrition_catalog(db_path, manifest_path)

    # Strict load should succeed and build index
    catalog = load_verified_hfkb(db_path, manifest_path, strict=True)
    assert isinstance(catalog, HFKBCatalog)
    assert catalog.count_records() > 0

    # Test with colocated default naming (catalog_path.name + .manifest.json)
    catalog_colocated = load_verified_hfkb(db_path, strict=True)
    assert catalog_colocated.count_records() == catalog.count_records()


# -----------------------------------------------------------------------------
# Test 3: Strict verification fails on 1-byte file tampering
# -----------------------------------------------------------------------------
def test_strict_verification_fails_on_1_byte_file_tampering(tmp_path):
    db_path = tmp_path / "tamper_catalog.db"
    manifest_path = tmp_path / "tamper_catalog.manifest.json"

    build_nutrition_catalog(db_path, manifest_path)

    # Verify initial genuine state passes
    assert load_verified_hfkb(db_path, manifest_path, strict=True) is not None

    # Alter exactly 1 byte in the database file
    file_bytes = bytearray(db_path.read_bytes())
    # Modify a byte in the database content (offset 200, beyond fixed header)
    file_bytes[200] ^= 0xFF
    db_path.write_bytes(bytes(file_bytes))

    # Strict load must fail closed with CatalogVerificationError
    with pytest.raises(CatalogVerificationError) as exc_info:
        load_verified_hfkb(db_path, manifest_path, strict=True)

    err_msg = str(exc_info.value).lower()
    assert "verification failed" in err_msg or "mismatch" in err_msg or "integrity" in err_msg


# -----------------------------------------------------------------------------
# Test 4: Strict verification fails on corrupt manifest
# -----------------------------------------------------------------------------
def test_strict_verification_fails_on_corrupt_manifest(tmp_path):
    db_path = tmp_path / "manifest_test.db"
    manifest_path = tmp_path / "manifest_test.manifest.json"

    build_nutrition_catalog(db_path, manifest_path)

    # Case A: Corrupted JSON syntax
    manifest_path.write_text("{invalid_json_corrupted", encoding="utf-8")
    with pytest.raises(CatalogVerificationError):
        load_verified_hfkb(db_path, manifest_path, strict=True)

    # Case B: Altered SHA-256 checksum in manifest
    manifest_data = {
        "catalog_id": "healbite-nutrition-catalog-v2",
        "format_version": "2.2",
        "sha256_checksum": "0" * 64,
        "file_size_bytes": db_path.stat().st_size,
        "record_count": 10,
        "sources": [],
        "publication_date": "2026-10-17",
        "created_at": "2026-10-17T00:00:00Z",
    }
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    with pytest.raises(CatalogVerificationError) as exc_b:
        load_verified_hfkb(db_path, manifest_path, strict=True)
    assert "mismatch" in str(exc_b.value).lower()

    # Case C: Missing manifest file entirely
    manifest_path.unlink()
    alt_manifest = db_path.parent / f"{db_path.name}.manifest.json"
    if alt_manifest.exists():
        alt_manifest.unlink()

    with pytest.raises(CatalogVerificationError) as exc_c:
        load_verified_hfkb(db_path, strict=True)
    assert "not found" in str(exc_c.value).lower() or "no manifest" in str(exc_c.value).lower()


# -----------------------------------------------------------------------------
# Test 5: Verified catalog supports Russian food search with 0 network calls
# -----------------------------------------------------------------------------
def test_verified_catalog_supports_russian_food_search_zero_network_calls(tmp_path, monkeypatch):
    db_path = tmp_path / "offline_catalog.db"
    manifest_path = tmp_path / "offline_catalog.manifest.json"

    build_nutrition_catalog(db_path, manifest_path)
    catalog = load_verified_hfkb(db_path, manifest_path, strict=True)

    # Strict network firewall: Any network socket creation raises RuntimeError
    def guard_socket(*args, **kwargs):
        raise RuntimeError("Forbidden live network call during catalog lookup!")

    monkeypatch.setattr(socket, "socket", guard_socket)

    # Multi-item Russian search queries across staples and preparations
    test_queries = [
        ("куриная грудка", "Куриная грудка запеченная"),
        ("ржаной хлеб", "Ржаной хлеб"),
        ("овсяная каша", "Овсяная каша"),
        ("гречневая каша", "Гречневая каша"),
        ("творог", "Творог"),
        ("яйцо", "Куриное яйцо сырое"),
    ]

    for query, expected_name_part in test_queries:
        res = catalog.resolve(query)
        assert res.matched is True, f"Failed to match Russian query: {query}"
        assert res.profile is not None
        assert res.profile.calories_kcal > 0
        assert res.source_id != ""
        assert res.dataset_version != ""
        assert res.source_provenance != ""


# -----------------------------------------------------------------------------
# Test 6: Query performance meets latency requirements (< 50 ms for typical lookups)
# -----------------------------------------------------------------------------
def test_query_performance_latency_under_50ms(tmp_path):
    db_path = tmp_path / "perf_catalog.db"
    manifest_path = tmp_path / "perf_catalog.manifest.json"

    build_nutrition_catalog(db_path, manifest_path)
    catalog = load_verified_hfkb(db_path, manifest_path, strict=True)

    queries = [
        "куриная грудка вареная",
        "ржаной хлеб",
        "гречка",
        "овсянка с молоком",
        "свежий огурец",
        "помидоры черри",
        "сыр российский",
        "творог мягкий",
        "индейка филе",
        "вареные яйца",
    ]

    latencies_ms = []
    # Warmup and timed runs (100 total lookups)
    for _ in range(10):
        for q in queries:
            t0 = time.perf_counter()
            res = catalog.resolve(q)
            t1 = time.perf_counter()
            latencies_ms.append((t1 - t0) * 1000.0)
            assert res is not None

    avg_latency = sum(latencies_ms) / len(latencies_ms)
    max_latency = max(latencies_ms)
    p95_latency = sorted(latencies_ms)[int(len(latencies_ms) * 0.95)]

    # Normative requirement: < 50 ms typical lookup
    assert avg_latency < 50.0, f"Average latency {avg_latency:.2f} ms exceeds 50 ms threshold"
    assert p95_latency < 50.0, f"p95 latency {p95_latency:.2f} ms exceeds 50 ms threshold"


# -----------------------------------------------------------------------------
# Test 7: Fallback path when full catalog is absent in development
# -----------------------------------------------------------------------------
def test_fallback_path_when_catalog_absent_in_development(tmp_path):
    non_existent_db = tmp_path / "missing_catalog.db"

    # Strict mode fails closed
    with pytest.raises(CatalogVerificationError):
        load_verified_hfkb(non_existent_db, strict=True)

    # get_default_hfkb safely returns None on missing file (allowing synthetic/seed fallback)
    default_cat = get_default_hfkb(non_existent_db)
    assert default_cat is None
