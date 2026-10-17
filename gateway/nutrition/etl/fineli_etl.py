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


def _parse_fineli_json(path: Path) -> list[ETLFoodItem]:
    import json
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    raw_list: list[dict[str, Any]] = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key in ("foods", "items", "data"):
            if key in data and isinstance(data[key], list):
                raw_list.extend(data[key])
        if not raw_list and ("fineli_id" in data or "id" in data):
            raw_list = [data]

    items: list[ETLFoodItem] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        fid = str(raw.get("fineli_id") or raw.get("id") or "").strip()
        if not fid:
            continue
        name_en = str(raw.get("name_en") or raw.get("name") or "").strip()
        name_ru = str(raw.get("name_ru") or name_en).strip()
        category = str(raw.get("category") or "general").strip()

        cals = float(raw.get("calories_kcal") or raw.get("energyKcal") or raw.get("energy") or raw.get("ENERC") or 0.0)
        prot = float(raw.get("protein_g") or raw.get("protein") or raw.get("PROT") or 0.0)
        fat = float(raw.get("fat_g") or raw.get("fat") or raw.get("FAT") or 0.0)
        carbs = float(raw.get("carbs_g") or raw.get("carbohydrate") or raw.get("CHOAVL") or 0.0)
        fiber = float(raw.get("fiber_g") or raw.get("fiber") or raw.get("FIBB") or 0.0)
        sugars = float(raw.get("sugars_g") or raw.get("sugar") or raw.get("SUGAR") or 0.0)

        aliases = list(raw.get("aliases", []))
        if not aliases and name_ru:
            aliases.append(name_ru.casefold())

        portions = []
        for p in raw.get("portions", []):
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                portions.append((str(p[0]), float(p[1])))
            elif isinstance(p, dict):
                portions.append((str(p.get("name") or "порция"), float(p.get("grams") or 100.0)))

        item = ETLFoodItem(
            food_id=f"fineli-{fid}",
            name_ru=name_ru,
            name_en=name_en,
            category=category,
            cooking_state=str(raw.get("cooking_state") or "raw"),
            source_dataset="FINELI",
            source_id=f"FINELI-{fid}",
            dataset_version=str(raw.get("dataset_version") or "Fineli-Release-20"),
            source_provenance=f"Fineli National Food Composition Database, ID {fid}: {name_en or name_ru}",
            calories_kcal=round(cals, 1),
            protein_g=round(prot, 1),
            fat_g=round(fat, 1),
            carbs_g=round(carbs, 1),
            fiber_g=round(fiber, 1),
            sugars_g=round(sugars, 1),
            reference_confidence=float(raw.get("reference_confidence") or 0.95),
            aliases=tuple(aliases),
            portions=tuple(portions),
        )
        items.append(item)
    return items


def _parse_fineli_csv(path: Path) -> list[ETLFoodItem]:
    import csv
    items: list[ETLFoodItem] = []
    with path.open("r", encoding="utf-8") as f:
        sample = f.read(2048)
        f.seek(0)
        delimiter = ";" if ";" in sample and "," not in sample else ","
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            fid = str(row.get("fineli_id") or row.get("id") or "").strip()
            if not fid:
                continue
            name_en = str(row.get("name_en") or row.get("name") or "").strip()
            name_ru = str(row.get("name_ru") or name_en).strip()
            category = str(row.get("category") or "general").strip()

            cals = float(row.get("calories_kcal") or row.get("energyKcal") or row.get("energy") or row.get("ENERC") or 0.0)
            prot = float(row.get("protein_g") or row.get("protein") or row.get("PROT") or 0.0)
            fat = float(row.get("fat_g") or row.get("fat") or row.get("FAT") or 0.0)
            carbs = float(row.get("carbs_g") or row.get("carbohydrate") or row.get("CHOAVL") or 0.0)
            fiber = float(row.get("fiber_g") or row.get("fiber") or row.get("FIBB") or 0.0)
            sugars = float(row.get("sugars_g") or row.get("sugar") or row.get("SUGAR") or 0.0)

            raw_aliases = row.get("aliases", "")
            aliases = tuple(a.strip() for a in raw_aliases.split(";") if a.strip()) if raw_aliases else (name_ru.casefold(),)

            item = ETLFoodItem(
                food_id=f"fineli-{fid}",
                name_ru=name_ru,
                name_en=name_en,
                category=category,
                cooking_state=str(row.get("cooking_state") or "raw"),
                source_dataset="FINELI",
                source_id=f"FINELI-{fid}",
                dataset_version=str(row.get("dataset_version") or "Fineli-Release-20"),
                source_provenance=f"Fineli National Food Composition Database, ID {fid}: {name_en or name_ru}",
                calories_kcal=round(cals, 1),
                protein_g=round(prot, 1),
                fat_g=round(fat, 1),
                carbs_g=round(carbs, 1),
                fiber_g=round(fiber, 1),
                sugars_g=round(sugars, 1),
                reference_confidence=float(row.get("reference_confidence") or 0.95),
                aliases=aliases,
                portions=(),
            )
            items.append(item)
    return items


def extract_fineli_items(custom_source_path: Path | None = None) -> list[ETLFoodItem]:
    """
    Extracts and standardizes Fineli items into ETLFoodItem records.
    If custom_source_path is provided, reads raw Fineli export JSON/CSV; otherwise uses verified seed set.
    """
    if custom_source_path is not None:
        p = Path(custom_source_path)
        if not p.exists():
            raise FileNotFoundError(f"Custom Fineli source path not found: {p}")
        if p.is_dir():
            json_files = list(p.glob("*.json"))
            if json_files:
                return _parse_fineli_json(json_files[0])
            csv_files = list(p.glob("*.csv"))
            if csv_files:
                return _parse_fineli_csv(csv_files[0])
            raise ValueError(f"No JSON or CSV files found in Fineli directory: {p}")
        if p.suffix.lower() == ".json":
            return _parse_fineli_json(p)
        if p.suffix.lower() == ".csv":
            return _parse_fineli_csv(p)
        raise ValueError(f"Unsupported Fineli source format: {p.suffix}")

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
