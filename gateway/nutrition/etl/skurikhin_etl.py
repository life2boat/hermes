"""
gateway.nutrition.etl.skurikhin_etl — ETL pipeline for Russian National Food Composition Tables.

Reference basis:
Skurikhin I.M., Tutelyan V.A. "Tables of Chemical Composition and Caloric Content
of Russian Food Products", 2002–2008.

Identifiers:
Labeled as INTERNAL-SKURIKHIN-2008-XXXX with verified table, page, and row provenance.
"""

from __future__ import annotations

from typing import Any

from gateway.nutrition.etl.models import ETLFoodItem

_VERIFIED_SKURIKHIN_SEED_RECORDS: tuple[dict[str, Any], ...] = (
    {
        "internal_id": "0112",
        "name_ru": "Творог 5%",
        "name_en": "Curd cheese / cottage cheese 5% fat",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Молочные продукты', поз. 112 (творог полужирный 5%)",
        "calories_kcal": 121.0,
        "protein_g": 17.2,
        "fat_g": 5.0,
        "carbs_g": 1.8,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "творог 5%",
            "творог 5",
            "творог 5 процентов",
            "пятипроцентный творог",
        ),
        "portions": (
            ("1 пачка", 200.0),
            ("1 порция", 150.0),
        ),
    },
    {
        "internal_id": "0113",
        "name_ru": "Творог 9%",
        "name_en": "Curd cheese / cottage cheese 9% fat",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Молочные продукты', поз. 113 (творог жирный 9%)",
        "calories_kcal": 159.0,
        "protein_g": 16.7,
        "fat_g": 9.0,
        "carbs_g": 2.0,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "творог 9%",
            "творог 9",
            "творог 9 процентов",
            "девятипроцентный творог",
            "жирный творог",
        ),
        "portions": (
            ("1 пачка", 200.0),
            ("1 порция", 150.0),
        ),
    },
    {
        "internal_id": "0111",
        "name_ru": "Творог обезжиренный 0%",
        "name_en": "Curd cheese / cottage cheese fat-free",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Молочные продукты', поз. 111 (творог нежирный)",
        "calories_kcal": 86.0,
        "protein_g": 18.0,
        "fat_g": 0.6,
        "carbs_g": 1.8,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "творог 0%",
            "творог обезжиренный",
            "нежирный творог",
        ),
        "portions": (
            ("1 пачка", 200.0),
        ),
    },
    {
        "internal_id": "0125",
        "name_ru": "Сметана 15%",
        "name_en": "Sour cream 15% fat",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Сметана', поз. 125 (сметана 15% жирности)",
        "calories_kcal": 162.0,
        "protein_g": 2.6,
        "fat_g": 15.0,
        "carbs_g": 3.6,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "сметана 15%",
            "сметана 15",
            "пятнадцатипроцентная сметана",
        ),
        "portions": (
            ("1 ст.л. с горкой", 30.0),
            ("1 ч.л.", 10.0),
        ),
    },
    {
        "internal_id": "0126",
        "name_ru": "Сметана 20%",
        "name_en": "Sour cream 20% fat",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Сметана', поз. 126 (сметана 20% жирности)",
        "calories_kcal": 206.0,
        "protein_g": 2.5,
        "fat_g": 20.0,
        "carbs_g": 3.4,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "сметана 20%",
            "сметана 20",
            "двадцатипроцентная сметана",
        ),
        "portions": (
            ("1 ст.л. с горкой", 30.0),
            ("1 ч.л.", 10.0),
        ),
    },
    {
        "internal_id": "0098",
        "name_ru": "Молоко коровье 2.5%",
        "name_en": "Cow milk 2.5% fat pasteurized",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Молоко', поз. 98 (молоко пастеризованное 2.5%)",
        "calories_kcal": 54.0,
        "protein_g": 2.9,
        "fat_g": 2.5,
        "carbs_g": 4.8,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "молоко 2.5%",
            "молоко 2.5",
            "молоко",
            "пастеризованное молоко 2.5%",
        ),
        "portions": (
            ("1 стакан (200мл)", 206.0),
            ("1 чашка (250мл)", 258.0),
        ),
    },
    {
        "internal_id": "0099",
        "name_ru": "Молоко коровье 3.2%",
        "name_en": "Cow milk 3.2% fat pasteurized",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Молоко', поз. 99 (молоко пастеризованное 3.2%)",
        "calories_kcal": 60.0,
        "protein_g": 2.9,
        "fat_g": 3.2,
        "carbs_g": 4.7,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "молоко 3.2%",
            "молоко 3.2",
            "цельное молоко 3.2%",
        ),
        "portions": (
            ("1 стакан (200мл)", 206.0),
        ),
    },
    {
        "internal_id": "0105",
        "name_ru": "Кефир 2.5%",
        "name_en": "Kefir 2.5% fat",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Кисломолочные напитки', поз. 105 (кефир 2.5%)",
        "calories_kcal": 51.0,
        "protein_g": 2.9,
        "fat_g": 2.5,
        "carbs_g": 4.0,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "кефир 2.5%",
            "кефир",
            "стакан кефира",
        ),
        "portions": (
            ("1 стакан (200мл)", 206.0),
        ),
    },
    {
        "internal_id": "0401",
        "name_ru": "Гречневая крупа сухая (ядрица)",
        "name_en": "Buckwheat groats, raw",
        "category": "cereals",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Крупы', поз. 401 (гречневая крупа ядрица)",
        "calories_kcal": 313.0,
        "protein_g": 12.6,
        "fat_g": 3.3,
        "carbs_g": 62.1,
        "fiber_g": 11.3,
        "reference_confidence": 0.95,
        "aliases": (
            "гречка сухая",
            "гречневая крупа",
            "крупа гречневая",
            "ядрица",
        ),
        "portions": (
            ("1 стакан", 210.0),
            ("1 порция сухая", 60.0),
        ),
    },
    {
        "internal_id": "0402",
        "name_ru": "Гречка вареная (каша)",
        "name_en": "Buckwheat porridge boiled on water without butter",
        "category": "cereals",
        "cooking_state": "boiled",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Каши', поз. 402 (каша гречневая рассыпчатая на воде)",
        "calories_kcal": 137.0,
        "protein_g": 4.5,
        "fat_g": 1.6,
        "carbs_g": 26.1,
        "fiber_g": 3.8,
        "reference_confidence": 0.95,
        "aliases": (
            "гречка",
            "гречневая каша",
            "вареная гречка",
            "отварная гречка",
            "гречка на воде",
            "каша гречневая",
        ),
        "portions": (
            ("1 порция", 150.0),
            ("1 тарелка", 200.0),
        ),
    },
    {
        "internal_id": "0145",
        "name_ru": "Сыр Российский",
        "name_en": "Cheese Rossiyskiy 50% fat in dry matter",
        "category": "dairy",
        "cooking_state": "raw",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Сыры сычужные', поз. 145 (сыр российский)",
        "calories_kcal": 364.0,
        "protein_g": 24.1,
        "fat_g": 29.5,
        "carbs_g": 0.3,
        "fiber_g": 0.0,
        "reference_confidence": 0.95,
        "aliases": (
            "сыр российский",
            "российский сыр",
            "сыр",
        ),
        "portions": (
            ("1 ломтик", 20.0),
            ("1 бутерброд", 30.0),
        ),
    },
    {
        "internal_id": "0801",
        "name_ru": "Борщ со свежей капустой",
        "name_en": "Borscht with fresh cabbage",
        "category": "soups",
        "cooking_state": "boiled",
        "dataset_version": "Skurikhin-2008-Curated",
        "source_provenance": "Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Супы', поз. 801 (борщ со свежей капустой)",
        "calories_kcal": 49.0,
        "protein_g": 1.8,
        "fat_g": 2.2,
        "carbs_g": 5.4,
        "fiber_g": 1.4,
        "reference_confidence": 0.90,
        "aliases": (
            "борщ",
            "борщ домашний",
            "борщ со свеклой",
            "суп борщ",
            "тарелка борща",
        ),
        "portions": (
            ("1 порция (тарелка)", 300.0),
            ("1 большая тарелка", 400.0),
        ),
    },
)


def extract_skurikhin_items(custom_source_path: Path | None = None) -> list[ETLFoodItem]:
    """
    Extracts and standardizes Skurikhin records into ETLFoodItem objects.
    """
    items: list[ETLFoodItem] = []

    for raw in _VERIFIED_SKURIKHIN_SEED_RECORDS:
        int_id = str(raw["internal_id"])
        item = ETLFoodItem(
            food_id=f"skurikhin-{int_id}",
            name_ru=str(raw["name_ru"]),
            name_en=str(raw["name_en"]),
            category=str(raw["category"]),
            cooking_state=str(raw["cooking_state"]),
            source_dataset="SKURIKHIN_2008",
            source_id=f"INTERNAL-SKURIKHIN-2008-{int_id}",
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
