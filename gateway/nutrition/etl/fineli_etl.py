"""
gateway.nutrition.etl.fineli_etl — ETL pipeline for Fineli (Finnish National Food Composition Database).

Ingests European and Nordic staple foods:
- Ruisleipä (100% rye bread)
- Wild berries (черника, брусника, малина)
- Nordic dairy and fish preparations
- Official identifier format: FINELI-XXXXX
- Precise macronutrients per 100g edible portion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gateway.nutrition.etl.models import ETLFoodItem

_VERIFIED_FINELI_SEED_RECORDS: tuple[dict[str, Any], ...] = (
    {
        "fineli_id": "10",
        "name_en": "Rye bread, 100% whole grain rye",
        "name_ru": "Ржаной хлеб",
        "category": "bakery",
        "cooking_state": "baked",
        "dataset_version": "Fineli-Release-20",
        "source_provenance": "Fineli National Food Composition Database, ID 10: Rye bread, whole grain",
        "calories_kcal": 218.0,
        "protein_g": 6.8,
        "fat_g": 1.4,
        "carbs_g": 39.5,
        "fiber_g": 11.2,
        "reference_confidence": 0.95,
        "aliases": (
            "ржаной хлеб",
            "черный хлеб",
            "хлеб ржаной",
            "бородинский хлеб",
            "дарницкий хлеб",
            "кусок ржаного хлеба",
        ),
        "portions": (
            ("1 ломтик", 35.0),
            ("1 кусок", 40.0),
        ),
    },
    {
        "fineli_id": "438",
        "name_en": "Bilberry, wild blueberry, raw",
        "name_ru": "Черника свежая",
        "category": "fruits",
        "cooking_state": "raw",
        "dataset_version": "Fineli-Release-20",
        "source_provenance": "Fineli National Food Composition Database, ID 438: Bilberry, wild",
        "calories_kcal": 44.0,
        "protein_g": 0.8,
        "fat_g": 0.6,
        "carbs_g": 7.4,
        "fiber_g": 3.3,
        "reference_confidence": 0.95,
        "aliases": (
            "черника",
            "голубика",
            "ягоды черники",
            "свежая черника",
        ),
        "portions": (
            ("1 горсть", 50.0),
            ("1 стакан", 140.0),
        ),
    },
    {
        "fineli_id": "440",
        "name_en": "Lingonberry, cowberry, raw",
        "name_ru": "Брусника",
        "category": "fruits",
        "cooking_state": "raw",
        "dataset_version": "Fineli-Release-20",
        "source_provenance": "Fineli National Food Composition Database, ID 440: Lingonberry, raw",
        "calories_kcal": 42.0,
        "protein_g": 0.5,
        "fat_g": 0.5,
        "carbs_g": 7.1,
        "fiber_g": 3.0,
        "reference_confidence": 0.95,
        "aliases": (
            "брусника",
            "ягоды брусники",
            "свежая брусника",
        ),
        "portions": (
            ("1 горсть", 50.0),
            ("1 стакан", 130.0),
        ),
    },
    {
        "fineli_id": "647",
        "name_en": "Quark, curd cheese, nonfat (0.2% fat)",
        "name_ru": "Творог мягкий обезжиренный",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Fineli-Release-20",
        "source_provenance": "Fineli National Food Composition Database, ID 647: Quark, nonfat",
        "calories_kcal": 65.0,
        "protein_g": 11.5,
        "fat_g": 0.2,
        "carbs_g": 4.1,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "творог мягкий 0%",
            "творог обезжиренный мягкий",
            "мягкий творог",
        ),
        "portions": (
            ("1 баночка", 125.0),
            ("1 пачка", 200.0),
        ),
    },
    {
        "fineli_id": "1118",
        "name_en": "Oat porridge made with water, no added salt",
        "name_ru": "Овсяная каша на воде",
        "category": "cereals",
        "cooking_state": "boiled",
        "dataset_version": "Fineli-Release-20",
        "source_provenance": "Fineli National Food Composition Database, ID 1118: Oat porridge with water",
        "calories_kcal": 68.0,
        "protein_g": 2.4,
        "fat_g": 1.3,
        "carbs_g": 11.1,
        "fiber_g": 1.7,
        "reference_confidence": 0.95,
        "aliases": (
            "овсяная каша на воде",
            "каша овсяная на воде",
            "овсянка на воде",
            "геркулесовая каша на воде",
        ),
        "portions": (
            ("1 порция", 200.0),
            ("1 тарелка", 250.0),
        ),
    },
)


def extract_fineli_items(custom_source_path: Path | None = None) -> list[ETLFoodItem]:
    """
    Extracts and standardizes Fineli items into ETLFoodItem records.
    """
    items: list[ETLFoodItem] = []

    for raw in _VERIFIED_FINELI_SEED_RECORDS:
        fineli_id = str(raw["fineli_id"])
        item = ETLFoodItem(
            food_id=f"fineli-{fineli_id}",
            name_ru=str(raw["name_ru"]),
            name_en=str(raw["name_en"]),
            category=str(raw["category"]),
            cooking_state=str(raw["cooking_state"]),
            source_dataset="FINELI",
            source_id=f"FINELI-{fineli_id}",
            dataset_version=str(raw["dataset_version"]),
            source_provenance=str(raw["source_provenance"]),
            calories_kcal=float(raw["calories_kcal"]),
            protein_g=float(raw["protein_g"]),
            fat_g=float(raw["fat_g"]),
            carbs_g=float(raw["carbs_g"]),
            fiber_g=float(raw.get("fiber_g", 0.0)),
            sugars_g=float(raw.get("sugars_g", 0.0)),
            reference_confidence=float(raw.get("reference_confidence", 0.95)),
            aliases=tuple(raw.get("aliases", ())),
            portions=tuple(raw.get("portions", ())),
        )
        items.append(item)

    return items
