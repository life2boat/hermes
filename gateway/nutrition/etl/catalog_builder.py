"""
gateway.nutrition.etl.catalog_builder — Builds and packages the authoritative nutrition_catalog_v2.db and manifest.

Orchestrates USDA, Fineli, and Skurikhin ETL extractors, constructs SQLite HFKB database,
verifies integrity and checksums, and emits the normative manifest file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from gateway.nutrition.etl.fineli_etl import extract_fineli_items
from gateway.nutrition.etl.models import ETLFoodItem
from gateway.nutrition.etl.skurikhin_etl import extract_skurikhin_items
from gateway.nutrition.etl.usda_etl import extract_usda_items
from gateway.nutrition.hfkb import HFKBCatalog, HFKBFoodItem
from gateway.nutrition.manifest import CatalogManifest, compute_file_sha256

logger = logging.getLogger(__name__)


def build_nutrition_catalog(
    output_db_path: Path | str,
    output_manifest_path: Path | str | None = None,
    extra_items: Sequence[ETLFoodItem] | None = None,
    usda_source_path: Path | str | None = None,
    fineli_source_path: Path | str | None = None,
) -> tuple[HFKBCatalog, CatalogManifest]:
    """
    Builds nutrition_catalog_v2.db and corresponding manifest.
    """
    db_path = Path(output_db_path)
    if db_path.exists():
        db_path.unlink()

    catalog = HFKBCatalog.initialize_database(db_path)

    # 1. Extract from all sources
    usda_items = extract_usda_items(Path(usda_source_path) if usda_source_path else None)
    fineli_items = extract_fineli_items(Path(fineli_source_path) if fineli_source_path else None)
    skurikhin_items = extract_skurikhin_items()

    all_items: list[ETLFoodItem] = []
    all_items.extend(usda_items)
    all_items.extend(fineli_items)
    all_items.extend(skurikhin_items)
    if extra_items:
        all_items.extend(extra_items)

    # 2. Insert into HFKB database
    conn = catalog.connect()
    # Temporarily open read-write
    conn.close()
    rw_conn = catalog.connect() if not catalog.read_only else None
    # Use insert_food_item
    for item in all_items:
        hfkb_item = HFKBFoodItem(
            food_id=item.food_id,
            name_ru=item.name_ru,
            name_en=item.name_en,
            category=item.category,
            cooking_state=item.cooking_state,
            source_dataset=item.source_dataset,
            source_id=item.source_id,
            dataset_version=item.dataset_version,
            source_provenance=item.source_provenance,
            edible_portion_basis="per 100g",
            reference_confidence=item.reference_confidence,
            variance_note=item.variance_note,
            calories_kcal=item.calories_kcal,
            protein_g=item.protein_g,
            fat_g=item.fat_g,
            carbs_g=item.carbs_g,
            fiber_g=item.fiber_g,
            sugars_g=item.sugars_g,
            aliases=item.aliases,
            portions=item.portions,
        )
        catalog.insert_food_item(hfkb_item)

    record_count = catalog.count_records()

    # 3. Compute artifact hashes & manifest
    file_size = db_path.stat().st_size
    file_sha256 = compute_file_sha256(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    manifest = CatalogManifest(
        catalog_id="healbite-nutrition-catalog-v2",
        format_version="2.2",
        sha256_checksum=file_sha256,
        file_size_bytes=file_size,
        record_count=record_count,
        sources=[
            {
                "dataset": "USDA_SR_LEGACY",
                "version": "2024",
                "records_count": len(usda_items),
                "url": "https://fdc.nal.usda.gov/download-datasets/",
            },
            {
                "dataset": "FINELI",
                "version": "Release-20",
                "records_count": len(fineli_items),
                "url": "https://fineli.fi/fineli/en/index",
            },
            {
                "dataset": "SKURIKHIN_2008",
                "version": "Skurikhin-2008-Curated",
                "records_count": len(skurikhin_items),
                "citation": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник 'Таблицы химического состава'",
            },
        ],
        publication_date=now_iso[:10],
        created_at=now_iso,
    )

    manifest_path = (
        Path(output_manifest_path)
        if output_manifest_path
        else db_path.parent / f"{db_path.name}.manifest.json"
    )
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    return catalog, manifest
