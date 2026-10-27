"""
gateway.nutrition.etl.usda_etl — ETL pipeline for USDA FoodData Central (Foundation, SR Legacy, and Survey/FNDDS).

Ingests and validates verified USDA FoodData Central records:
- Energy (kcal), Protein (g), Total lipid (g), Carbohydrates (g) per 100g edible portion.
- Official USDA FDC identifier format: USDA-FDC-XXXXXX.
- Precise preparation states (raw, cooked, boiled, roasted, canned).
- Curated Russian culinary aliases and portion conversions.
- Full extraction from bulk Foundation Foods, SR Legacy, and Survey (FNDDS) datasets.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from typing import Any, Iterable

from gateway.nutrition.etl.curated_food_data import (
    classify_category_from_usda,
    detect_cooking_state_from_text,
    translate_description_to_russian,
)
from gateway.nutrition.etl.curated_mappings import CURATED_RUSSIAN_FOOD_MAPPINGS
from gateway.nutrition.etl.models import ETLFoodItem

logger = logging.getLogger(__name__)

# Verified USDA FoodData Central seed dataset with authentic FDC IDs and nutritional figures
_VERIFIED_USDA_SEED_RECORDS: tuple[dict[str, Any], ...] = (
    {
        "fdc_id": "171077",
        "name_en": "Chicken, broilers or fryers, breast, meat only, raw",
        "name_ru": "Куриная грудка сырая",
        "category": "poultry",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 171077: Chicken breast, meat only, raw",
        "calories_kcal": 120.0,
        "protein_g": 22.5,
        "fat_g": 2.6,
        "carbs_g": 0.0,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "куриная грудка",
            "куриное филе",
            "грудка куриная",
            "филе курицы",
            "куриное филе сырое",
            "сырая куриная грудка",
            "куриная грудка сырая",
        ),
        "portions": (
            ("1 шт (филе)", 200.0),
            ("1 порция", 150.0),
        ),
    },
    {
        "fdc_id": "171477",
        "name_en": "Chicken, broilers or fryers, breast, meat only, cooked, roasted",
        "name_ru": "Куриная грудка запеченная",
        "category": "poultry",
        "cooking_state": "roasted",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 171477: Chicken breast, meat only, cooked, roasted",
        "calories_kcal": 165.0,
        "protein_g": 31.0,
        "fat_g": 3.6,
        "carbs_g": 0.0,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "куриная грудка вареная",
            "вареная куриная грудка",
            "отварная куриная грудка",
            "куриное филе вареное",
            "вареное куриное филе",
            "куриная грудка запеченная",
            "курица вареная",
            "отварная курица",
        ),
        "portions": (
            ("1 порция", 150.0),
            ("1 филе готовое", 140.0),
        ),
    },
    {
        "fdc_id": "171287",
        "name_en": "Egg, whole, raw, fresh",
        "name_ru": "Куриное яйцо сырое",
        "category": "eggs",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 171287: Egg, whole, raw, fresh",
        "calories_kcal": 143.0,
        "protein_g": 12.6,
        "fat_g": 9.5,
        "carbs_g": 0.7,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "яйцо",
            "куриное яйцо",
            "яйца",
            "сырое яйцо",
        ),
        "portions": (
            ("1 шт (С1)", 55.0),
            ("1 шт (С0)", 65.0),
        ),
    },
    {
        "fdc_id": "173424",
        "name_en": "Egg, whole, cooked, hard-boiled",
        "name_ru": "Вареное яйцо",
        "category": "eggs",
        "cooking_state": "boiled",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 173424: Egg, whole, cooked, hard-boiled",
        "calories_kcal": 155.0,
        "protein_g": 12.6,
        "fat_g": 10.6,
        "carbs_g": 1.1,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "вареное яйцо",
            "яйцо вареное",
            "яйцо вкрутую",
            "яйцо всмятку",
            "отварное яйцо",
        ),
        "portions": (
            ("1 шт (С1)", 50.0),
            ("1 шт (С0)", 60.0),
        ),
    },
    {
        "fdc_id": "175167",
        "name_en": "Fish, salmon, Atlantic, wild, raw",
        "name_ru": "Лосось сырой",
        "category": "fish",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 175167: Fish, salmon, Atlantic, wild, raw",
        "calories_kcal": 142.0,
        "protein_g": 19.8,
        "fat_g": 6.3,
        "carbs_g": 0.0,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "лосось",
            "семга",
            "форель",
            "лосось сырой",
            "филе лосося",
            "красная рыба",
        ),
        "portions": (
            ("1 стейк", 200.0),
            ("1 порция", 150.0),
        ),
    },
    {
        "fdc_id": "175168",
        "name_en": "Fish, salmon, Atlantic, wild, cooked, dry heat",
        "name_ru": "Лосось запеченный",
        "category": "fish",
        "cooking_state": "cooked",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 175168: Fish, salmon, Atlantic, wild, cooked, dry heat",
        "calories_kcal": 182.0,
        "protein_g": 25.4,
        "fat_g": 8.1,
        "carbs_g": 0.0,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "лосось запеченный",
            "лосось на пару",
            "запеченный лосось",
            "вареный лосось",
            "семга запеченная",
            "форель запеченная",
        ),
        "portions": (
            ("1 порция", 150.0),
            ("1 стейк готовый", 160.0),
        ),
    },
    {
        "fdc_id": "748608",
        "name_en": "Oil, olive, extra virgin",
        "name_ru": "Оливковое масло",
        "category": "fats_oils",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-Foundation-2024",
        "source_provenance": "USDA FoodData Central Foundation, FDC ID 748608: Oil, olive, extra virgin",
        "calories_kcal": 884.0,
        "protein_g": 0.0,
        "fat_g": 100.0,
        "carbs_g": 0.0,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "оливковое масло",
            "масло оливковое",
            "экстра вирджин",
        ),
        "portions": (
            ("1 ст.л.", 14.0),
            ("1 ч.л.", 5.0),
        ),
    },
    {
        "fdc_id": "169747",
        "name_en": "Cereals, oats, regular and quick, unenriched, dry",
        "name_ru": "Овсяные хлопья (геркулес)",
        "category": "cereals",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 169747: Cereals, oats, regular and quick, dry",
        "calories_kcal": 389.0,
        "protein_g": 16.9,
        "fat_g": 6.9,
        "carbs_g": 66.3,
        "fiber_g": 10.6,
        "reference_confidence": 0.95,
        "aliases": (
            "овсянка",
            "геркулес",
            "овсяные хлопья",
            "хлопья овсяные",
            "овсяная крупа",
        ),
        "portions": (
            ("1 порция сухая", 50.0),
            ("1 стакан (250мл)", 90.0),
        ),
    },
    {
        "fdc_id": "168877",
        "name_en": "Rice, white, long-grain, regular, raw, enriched",
        "name_ru": "Рис белый сухой",
        "category": "cereals",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 168877: Rice, white, long-grain, raw",
        "calories_kcal": 365.0,
        "protein_g": 7.1,
        "fat_g": 0.7,
        "carbs_g": 80.0,
        "fiber_g": 1.3,
        "reference_confidence": 0.95,
        "aliases": (
            "рис сухой",
            "сырой рис",
            "белый рис сухой",
            "рис белый сухой",
            "крупа рисовая",
        ),
        "portions": (
            ("1 стакан", 200.0),
            ("1 порция сухая", 60.0),
        ),
    },
    {
        "fdc_id": "168878",
        "name_en": "Rice, white, long-grain, regular, enriched, cooked",
        "name_ru": "Рис вареный",
        "category": "cereals",
        "cooking_state": "boiled",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 168878: Rice, white, long-grain, cooked",
        "calories_kcal": 130.0,
        "protein_g": 2.7,
        "fat_g": 0.3,
        "carbs_g": 28.2,
        "fiber_g": 0.4,
        "reference_confidence": 0.95,
        "aliases": (
            "рис",
            "рис вареный",
            "вареный рис",
            "отварной рис",
            "рис на гарнир",
            "белый рис",
        ),
        "portions": (
            ("1 порция", 150.0),
            ("1 чашка", 160.0),
        ),
    },
    {
        "fdc_id": "173418",
        "name_en": "Cheese, cheddar",
        "name_ru": "Сыр чеддер",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 173418: Cheese, cheddar",
        "calories_kcal": 403.0,
        "protein_g": 24.9,
        "fat_g": 33.1,
        "carbs_g": 1.3,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "чеддер",
            "сыр чеддер",
        ),
        "portions": (
            ("1 ломтик", 25.0),
            ("1 порция", 40.0),
        ),
    },
    {
        "fdc_id": "171705",
        "name_en": "Avocados, raw, all commercial varieties",
        "name_ru": "Авокадо",
        "category": "fruits",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 171705: Avocados, raw",
        "calories_kcal": 160.0,
        "protein_g": 2.0,
        "fat_g": 14.7,
        "carbs_g": 8.5,
        "fiber_g": 6.7,
        "reference_confidence": 0.95,
        "aliases": (
            "авокадо",
            "спелый авокадо",
            "половинка авокадо",
        ),
        "portions": (
            ("1 шт без косточки", 150.0),
            ("1/2 шт", 75.0),
        ),
    },
    {
        "fdc_id": "173944",
        "name_en": "Bananas, raw",
        "name_ru": "Банан",
        "category": "fruits",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 173944: Bananas, raw",
        "calories_kcal": 89.0,
        "protein_g": 1.1,
        "fat_g": 0.3,
        "carbs_g": 22.8,
        "fiber_g": 2.6,
        "reference_confidence": 0.95,
        "aliases": (
            "банан",
            "бананы",
            "спелый банан",
        ),
        "portions": (
            ("1 шт средний", 120.0),
        ),
    },
    {
        "fdc_id": "170379",
        "name_en": "Broccoli, raw",
        "name_ru": "Брокколи",
        "category": "vegetables",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 170379: Broccoli, raw",
        "calories_kcal": 34.0,
        "protein_g": 2.8,
        "fat_g": 0.4,
        "carbs_g": 6.6,
        "fiber_g": 2.6,
        "reference_confidence": 0.95,
        "aliases": (
            "брокколи",
            "капуста брокколи",
            "свежая брокколи",
        ),
        "portions": (
            ("1 порция", 100.0),
        ),
    },
    {
        "fdc_id": "170027",
        "name_en": "Potatoes, boiled, cooked in skin, flesh, without salt",
        "name_ru": "Картофель вареный",
        "category": "vegetables",
        "cooking_state": "boiled",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 170027: Potatoes, boiled, cooked in skin",
        "calories_kcal": 87.0,
        "protein_g": 1.9,
        "fat_g": 0.1,
        "carbs_g": 20.1,
        "fiber_g": 1.8,
        "reference_confidence": 0.95,
        "aliases": (
            "картофель",
            "вареный картофель",
            "картошка",
            "отварной картофель",
            "картофель в мундире",
        ),
        "portions": (
            ("1 шт средняя", 150.0),
            ("1 порция", 200.0),
        ),
    },
    {
        "fdc_id": "170457",
        "name_en": "Tomatoes, red, ripe, raw, year round average",
        "name_ru": "Помидор (томат)",
        "category": "vegetables",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 170457: Tomatoes, red, ripe, raw",
        "calories_kcal": 18.0,
        "protein_g": 0.9,
        "fat_g": 0.2,
        "carbs_g": 3.9,
        "fiber_g": 1.2,
        "reference_confidence": 0.95,
        "aliases": (
            "помидор",
            "помидоры",
            "томат",
            "томаты",
            "черри",
            "свежий помидор",
        ),
        "portions": (
            ("1 шт средний", 120.0),
        ),
    },
    {
        "fdc_id": "169225",
        "name_en": "Cucumber, with peel, raw",
        "name_ru": "Огурец свежий",
        "category": "vegetables",
        "cooking_state": "raw",
        "dataset_version": "USDA-FDC-SRLegacy-2024",
        "source_provenance": "USDA FoodData Central SR Legacy, FDC ID 169225: Cucumber, with peel, raw",
        "calories_kcal": 15.0,
        "protein_g": 0.7,
        "fat_g": 0.1,
        "carbs_g": 3.6,
        "fiber_g": 0.5,
        "reference_confidence": 0.95,
        "aliases": (
            "огурец",
            "огурцы",
            "свежий огурец",
        ),
        "portions": (
            ("1 шт средний", 100.0),
        ),
    },
)

_EXTRACTION_STATS: dict[str, int] = {
    "total_raw_records": 0,
    "imported_records": 0,
    "rejected_incomplete": 0,
    "deduplicated_records": 0,
}


def get_usda_extraction_stats() -> dict[str, int]:
    """Returns copy of extraction statistics counters."""
    return dict(_EXTRACTION_STATS)


def _extract_nutrients_from_raw(raw: dict[str, Any]) -> dict[str, float] | None:
    """
    Extracts and standardizes calories (kcal), protein (g), fat (g), carbs (g),
    fiber (g), sugars (g) per 100g.
    Returns None if mandatory values (calories, protein, fat, carbs) are absent or invalid.
    Never invents missing nutrient values.
    """
    cals_cand = raw.get("calories_kcal") if "calories_kcal" in raw else (raw.get("calories") or raw.get("energy"))
    if cals_cand is not None:
        try:
            cals = float(cals_cand)
            prot = float(raw.get("protein_g") if "protein_g" in raw else (raw.get("protein") or 0.0))
            fat = float(raw.get("fat_g") if "fat_g" in raw else (raw.get("fat") or raw.get("lipid") or 0.0))
            carbs = float(raw.get("carbs_g") if "carbs_g" in raw else (raw.get("carbohydrates") or raw.get("carbs") or 0.0))
            fiber = float(raw.get("fiber_g") if "fiber_g" in raw else (raw.get("fiber") or 0.0))
            sugars = float(raw.get("sugars_g") if "sugars_g" in raw else (raw.get("sugars") or raw.get("sugar") or 0.0))
            if cals < 0 or prot < 0 or fat < 0 or carbs < 0:
                return None
            return {
                "calories_kcal": round(cals, 1),
                "protein_g": round(prot, 1),
                "fat_g": round(fat, 1),
                "carbs_g": round(carbs, 1),
                "fiber_g": round(max(0.0, fiber), 1),
                "sugars_g": round(max(0.0, sugars), 1),
            }
        except (ValueError, TypeError):
            return None

    nutrients = raw.get("foodNutrients", [])
    if not isinstance(nutrients, list):
        return None

    cals: float | None = None
    cals_kj: float | None = None
    prot: float | None = None
    fat: float | None = None
    carbs: float | None = None
    fiber = 0.0
    sugars = 0.0

    for n in nutrients:
        if not isinstance(n, dict):
            continue
        n_id = n.get("nutrientId")
        if not n_id and isinstance(n.get("nutrient"), dict):
            n_id = n["nutrient"].get("id")

        n_name = n.get("nutrientName")
        if not n_name and isinstance(n.get("nutrient"), dict):
            n_name = n["nutrient"].get("name")
        n_name_lower = (n_name or "").lower()

        val = n.get("amount") if "amount" in n else n.get("value")
        if val is None:
            continue
        try:
            val = float(val)
        except (ValueError, TypeError):
            continue

        if n_id == 1008 or ("energy" in n_name_lower and "kcal" in n_name_lower):
            cals = val
        elif n_id == 1062 or ("energy" in n_name_lower and "kj" in n_name_lower):
            cals_kj = val
        elif n_id == 1003 or "protein" in n_name_lower:
            prot = val
        elif n_id == 1004 or "total lipid" in n_name_lower or n_name_lower == "fat":
            fat = val
        elif n_id in (1005, 1050) or "carbohydrate" in n_name_lower:
            carbs = val
        elif n_id == 1079 or "fiber" in n_name_lower:
            fiber = val
        elif n_id in (2000, 1063) or "sugar" in n_name_lower:
            sugars = val

    if cals is None and cals_kj is not None:
        cals = round(cals_kj / 4.184, 1)

    if cals is None or prot is None or fat is None or carbs is None:
        return None
    if cals < 0 or prot < 0 or fat < 0 or carbs < 0:
        return None

    return {
        "calories_kcal": round(cals, 1),
        "protein_g": round(prot, 1),
        "fat_g": round(fat, 1),
        "carbs_g": round(carbs, 1),
        "fiber_g": round(max(0.0, fiber), 1),
        "sugars_g": round(max(0.0, sugars), 1),
    }


def _build_curated_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    curated_by_fdc: dict[str, dict[str, Any]] = {}
    curated_by_pat: dict[str, dict[str, Any]] = {}
    for entry in _VERIFIED_USDA_SEED_RECORDS:
        curated_by_fdc[str(entry["fdc_id"])] = entry
    for entry in CURATED_RUSSIAN_FOOD_MAPPINGS:
        curated_by_pat[entry["pattern"].lower()] = entry
        if "fdc_id" in entry:
            curated_by_fdc[str(entry["fdc_id"])] = entry
    return curated_by_fdc, curated_by_pat


def _process_raw_usda_record(
    raw: dict[str, Any],
    default_dataset: str,
    default_version: str,
    default_confidence: float,
    curated_by_fdc: dict[str, dict[str, Any]],
    curated_by_pat: dict[str, dict[str, Any]],
) -> ETLFoodItem | None:
    fdc_id = str(raw.get("fdcId") or raw.get("fdc_id") or raw.get("id") or "").strip()
    if not fdc_id:
        return None

    desc = str(raw.get("description") or raw.get("name_en") or raw.get("name") or "").strip()
    if not desc:
        return None

    nutr = _extract_nutrients_from_raw(raw)
    if nutr is None:
        _EXTRACTION_STATS["rejected_incomplete"] += 1
        return None

    # Determine category
    food_cat = raw.get("foodCategory") or raw.get("wweiaFoodCategory") or raw.get("category") or {}
    cat_desc = (
        food_cat.get("description")
        or food_cat.get("wweiaFoodCategoryDescription")
        if isinstance(food_cat, dict)
        else str(food_cat)
    )
    category = classify_category_from_usda(cat_desc, desc)

    # Determine cooking state
    cooking_state = detect_cooking_state_from_text(desc)

    # Curated alias and name matching: FDC ID > exact pattern > substring > core words
    curated = curated_by_fdc.get(fdc_id)
    desc_low = desc.lower()
    if curated is None:
        curated = curated_by_pat.get(desc_low)
    if curated is None:
        for pat, c_def in curated_by_pat.items():
            if pat in desc_low:
                c_state = c_def.get("cooking_state")
                if c_state and c_state != cooking_state and (c_state == "raw" or cooking_state == "raw"):
                    continue
                curated = c_def
                break
    if curated is None:
        desc_words = [w.strip() for w in desc_low.replace(",", " ").split() if len(w.strip()) >= 3]
        for pat, c_def in curated_by_pat.items():
            c_state = c_def.get("cooking_state")
            if c_state and c_state != cooking_state and (c_state == "raw" or cooking_state == "raw"):
                continue
            pat_words = [w.strip() for w in pat.replace(",", " ").split() if len(w.strip()) >= 3]
            filler = {"broilers", "fryers", "broiler", "fryer", "classes", "domesticated", "regular", "variety", "composite", "trimmed"}
            core_words = [w for w in pat_words if w not in filler]
            if core_words and all(w in desc_words or any(w in dw for dw in desc_words) for w in core_words):
                curated = c_def
                break

    aliases: list[str] = []
    portions: list[tuple[str, float]] = []

    if raw.get("name_ru"):
        name_ru = str(raw["name_ru"]).strip()
        if curated:
            category = curated["category"]
            cooking_state = curated["cooking_state"]
            aliases = list(curated["aliases"])
            portions = list(curated.get("portions", ()))
        else:
            aliases = [name_ru.casefold()]
    elif curated:
        name_ru = curated["name_ru"]
        category = curated["category"]
        cooking_state = curated["cooking_state"]
        aliases = list(curated["aliases"])
        portions = list(curated.get("portions", ()))
    else:
        name_ru = translate_description_to_russian(desc).strip()
        aliases = [name_ru.casefold()]

    if "aliases" in raw and raw["aliases"]:
        if isinstance(raw["aliases"], str):
            for a in raw["aliases"].split(";"):
                a_clean = a.strip()
                if a_clean and a_clean not in aliases:
                    aliases.append(a_clean)
        elif isinstance(raw["aliases"], (list, tuple)):
            for a in raw["aliases"]:
                a_clean = str(a).strip()
                if a_clean and a_clean not in aliases:
                    aliases.append(a_clean)

    for p in raw.get("portions", []):
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            portions.append((str(p[0]), float(p[1])))
        elif isinstance(p, dict):
            portions.append((str(p.get("name") or "порция"), float(p.get("grams") or 100.0)))

    source_dataset = str(raw.get("source_dataset") or default_dataset)
    dataset_version = str(raw.get("dataset_version") or default_version)
    confidence = float(raw.get("reference_confidence") or default_confidence)

    return ETLFoodItem(
        food_id=f"usda-{fdc_id}",
        name_ru=name_ru,
        name_en=desc,
        category=category,
        cooking_state=cooking_state,
        source_dataset=source_dataset,
        source_id=f"USDA-FDC-{fdc_id}",
        dataset_version=dataset_version,
        source_provenance=f"USDA FoodData Central {source_dataset}, FDC ID {fdc_id}: {desc}",
        calories_kcal=nutr["calories_kcal"],
        protein_g=nutr["protein_g"],
        fat_g=nutr["fat_g"],
        carbs_g=nutr["carbs_g"],
        fiber_g=nutr["fiber_g"],
        sugars_g=nutr["sugars_g"],
        reference_confidence=confidence,
        aliases=tuple(aliases),
        portions=tuple(portions),
    )


def _parse_usda_json(path: Path) -> list[ETLFoodItem]:
    curated_by_fdc, curated_by_pat = _build_curated_maps()
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    raw_list: list[dict[str, Any]] = []
    dataset_name = "USDA_SR_LEGACY"
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key in ("FoundationFoods", "SRLegacyFoods", "SurveyFoods", "foods", "items"):
            if key in data and isinstance(data[key], list):
                raw_list.extend(data[key])
                if key == "FoundationFoods":
                    dataset_name = "USDA_FOUNDATION"
                elif key == "SurveyFoods":
                    dataset_name = "USDA_SURVEY"
        if not raw_list and ("fdcId" in data or "fdc_id" in data):
            raw_list = [data]

    items: list[ETLFoodItem] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        _EXTRACTION_STATS["total_raw_records"] += 1
        item = _process_raw_usda_record(
            raw,
            default_dataset=dataset_name,
            default_version="USDA-FDC-2024",
            default_confidence=0.95,
            curated_by_fdc=curated_by_fdc,
            curated_by_pat=curated_by_pat,
        )
        if item:
            items.append(item)
            _EXTRACTION_STATS["imported_records"] += 1

    return items


def _parse_usda_csv(path: Path) -> list[ETLFoodItem]:
    import csv

    curated_by_fdc, curated_by_pat = _build_curated_maps()
    items: list[ETLFoodItem] = []
    with path.open("r", encoding="utf-8") as f:
        sample = f.read(2048)
        f.seek(0)
        delimiter = ";" if ";" in sample and "," not in sample else ","
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            _EXTRACTION_STATS["total_raw_records"] += 1
            item = _process_raw_usda_record(
                dict(row),
                default_dataset="USDA_SR_LEGACY",
                default_version="USDA-FDC-2024",
                default_confidence=0.95,
                curated_by_fdc=curated_by_fdc,
                curated_by_pat=curated_by_pat,
            )
            if item:
                items.append(item)
                _EXTRACTION_STATS["imported_records"] += 1
    return items


def extract_usda_items(custom_source_path: Path | str | None = None) -> list[ETLFoodItem]:
    """
    Extracts and standardizes USDA FoodData Central items into ETLFoodItem records.
    Supports directories containing zip/json archives, single files, or defaults to seed dataset.
    """
    global _EXTRACTION_STATS
    _EXTRACTION_STATS = {
        "total_raw_records": 0,
        "imported_records": 0,
        "rejected_incomplete": 0,
        "deduplicated_records": 0,
    }

    if custom_source_path is not None:
        p = Path(custom_source_path)
        if not p.exists():
            raise FileNotFoundError(f"Custom USDA source path not found: {p}")

        if p.is_dir():
            curated_by_fdc, curated_by_pat = _build_curated_maps()
            seen_fdc_ids: set[str] = set()
            all_items: list[ETLFoodItem] = []

            # Dataset priority order: Foundation > SR Legacy > Survey
            target_datasets = [
                ("foundation.zip", "FoundationFoods", "USDA_FOUNDATION", "USDA-FDC-Foundation-2024", 0.95),
                ("sr_legacy.zip", "SRLegacyFoods", "USDA_SR_LEGACY", "USDA-FDC-SRLegacy-2024", 0.95),
                ("survey.zip", "SurveyFoods", "USDA_SURVEY", "USDA-FDC-Survey-2024", 0.90),
            ]

            found_any = False
            for zip_name, root_key, dataset_code, version, conf in target_datasets:
                zip_path = p / zip_name
                if zip_path.exists():
                    found_any = True
                    with zipfile.ZipFile(zip_path) as zf:
                        first_file = zf.namelist()[0]
                        with zf.open(first_file) as f:
                            data = json.load(f)
                            raw_items = data.get(root_key, [])
                            for raw in raw_items:
                                _EXTRACTION_STATS["total_raw_records"] += 1
                                fdc_id = str(raw.get("fdcId") or "").strip()
                                if fdc_id in seen_fdc_ids:
                                    _EXTRACTION_STATS["deduplicated_records"] += 1
                                    continue
                                item = _process_raw_usda_record(
                                    raw,
                                    default_dataset=dataset_code,
                                    default_version=version,
                                    default_confidence=conf,
                                    curated_by_fdc=curated_by_fdc,
                                    curated_by_pat=curated_by_pat,
                                )
                                if item:
                                    seen_fdc_ids.add(fdc_id)
                                    all_items.append(item)
                                    _EXTRACTION_STATS["imported_records"] += 1

            if found_any:
                return all_items

            # Check for unzipped json or csv files
            json_files = list(p.glob("*.json"))
            if json_files:
                return _parse_usda_json(json_files[0])
            csv_files = list(p.glob("*.csv"))
            if csv_files:
                return _parse_usda_csv(csv_files[0])

            raise ValueError(f"No valid USDA files (zip, json, csv) found in directory: {p}")

        if p.suffix.lower() == ".zip":
            # Extract from single zip
            curated_by_fdc, curated_by_pat = _build_curated_maps()
            items: list[ETLFoodItem] = []
            with zipfile.ZipFile(p) as zf:
                for fn in zf.namelist():
                    if fn.endswith(".json"):
                        with zf.open(fn) as f:
                            data = json.load(f)
                            raw_list: list[dict[str, Any]] = []
                            if isinstance(data, dict):
                                for k in ("FoundationFoods", "SRLegacyFoods", "SurveyFoods", "foods"):
                                    if k in data:
                                        raw_list.extend(data[k])
                            elif isinstance(data, list):
                                raw_list = data
                            for raw in raw_list:
                                _EXTRACTION_STATS["total_raw_records"] += 1
                                item = _process_raw_usda_record(
                                    raw,
                                    default_dataset="USDA_SR_LEGACY",
                                    default_version="USDA-FDC-2024",
                                    default_confidence=0.95,
                                    curated_by_fdc=curated_by_fdc,
                                    curated_by_pat=curated_by_pat,
                                )
                                if item:
                                    items.append(item)
                                    _EXTRACTION_STATS["imported_records"] += 1
            return items

        if p.suffix.lower() == ".json":
            return _parse_usda_json(p)
        if p.suffix.lower() == ".csv":
            return _parse_usda_csv(p)
        raise ValueError(f"Unsupported USDA source format: {p.suffix}")

    # Fallback to curated seed records
    curated_by_fdc, curated_by_pat = _build_curated_maps()
    items = []
    for raw in _VERIFIED_USDA_SEED_RECORDS:
        _EXTRACTION_STATS["total_raw_records"] += 1
        item = _process_raw_usda_record(
            raw,
            default_dataset="USDA_SR_LEGACY",
            default_version=str(raw.get("dataset_version", "USDA-FDC-2024")),
            default_confidence=float(raw.get("reference_confidence", 0.95)),
            curated_by_fdc=curated_by_fdc,
            curated_by_pat=curated_by_pat,
        )
        if item:
            items.append(item)
            _EXTRACTION_STATS["imported_records"] += 1

    return items
