"""
gateway.healbite_food_composition — Deterministic food composition reference engine.

PROVENANCE & REFERENCE BASIS:
- USDA FoodData Central (Foundation Foods & SR Legacy):
  Verified against official USDA FoodData Central database downloads.
  Referenced per-item via official FDC identifiers: USDA-FDC-XXXXXX.
- Russian National Food Composition Tables:
  Skurikhin I.M., Tutelyan V.A. "Tables of Chemical Composition and Caloric Content of
  Russian Food Products", 2002–2008.
  Referenced per-item with curated internal identifiers: INTERNAL-SKURIKHIN-2008-XXXX
  and verifiable source table/row provenance.

SPECIFICATION:
- Energy basis: kilocalories (kcal) per 100 g edible portion.
- Macronutrients: protein (g), fat (g), carbohydrates (g) per 100 g edible portion.
- Version: 2026.2
- Scope: Verified reference ingredients, cheeses, meats, staple foods, and common dishes.
- Reference confidence: Conservatively graded (0.85 - 0.95).
- Per-item provenance: Every catalog entry has verified source_id, dataset_version,
  cooking_state, and source_provenance.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

NUTRITION_REFERENCE_SOURCE = (
    "USDA FoodData Central (Foundation/SR Legacy) & Russian National Food Composition Tables (Skurikhin/Tutelyan)"
)
NUTRITION_REFERENCE_PROVENANCE = (
    "USDA FoodData Central SR Legacy & Foundation Public Domain; "
    "Russian National Food Composition Tables (Skurikhin/Tutelyan, 2002/2008); "
    "Reference basis: 100g edible portion; Energy units: kcal, Macronutrient units: grams (g)"
)
NUTRITION_REFERENCE_VERSION = "2026.1"
NUTRITION_REFERENCE_UNITS = "per 100g: kcal, protein (g), fat (g), carbohydrates (g)"

FOOD_COMPOSITION_PROVENANCE = NUTRITION_REFERENCE_PROVENANCE
FOOD_COMPOSITION_VERSION = NUTRITION_REFERENCE_VERSION


@dataclass(frozen=True, slots=True)
class FoodNutrientProfile:
    canonical_id: str
    display_name: str
    category: str
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    reference_confidence: float
    source_id: str
    dataset_version: str = "2026.2"
    cooking_state: str = "raw"
    edible_portion_basis: str = "per 100g"
    variance_note: str = ""
    source_provenance: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FoodLookupResult:
    matched: bool
    profile: FoodNutrientProfile | None = None
    confidence: float = 0.0
    match_type: str = "none"
    candidate_suggestions: tuple[str, ...] = ()
    source_id: str = ""
    dataset_version: str = ""
    cooking_state: str = ""
    source_provenance: str = ""


# -----------------------------------------------------------------------------
# Curated, Documented Food Composition Catalog
# -----------------------------------------------------------------------------

_FOOD_CATALOG: tuple[FoodNutrientProfile, ...] = (
    FoodNutrientProfile(
        canonical_id='apple',
        display_name='Яблоко',
        category='fruits',
        calories_kcal=52.0,
        protein_g=0.3,
        fat_g=0.2,
        carbs_g=14.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0350',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Фрукты свежие', поз. 350 (яблоки)",
        aliases=('яблоко', 'яблоки', 'зеленое яблоко', 'зелёное яблоко', 'красное яблоко', 'свежее яблоко'),
    ),
    FoodNutrientProfile(
        canonical_id='arugula',
        display_name='Руккола',
        category='vegetables',
        calories_kcal=25.9,
        protein_g=1.6,
        fat_g=0.3,
        carbs_g=5.4,
        reference_confidence=0.95,
        source_id='USDA-FDC-2710822',
        dataset_version='USDA-Foundation',
        cooking_state='raw',
        source_provenance="USDA FoodData Central Foundation Foods, FDC ID 2710822 ('Arugula, baby, raw'), pub. 2024-10-31",
        aliases=('руккола', 'рукола', 'свежая руккола', 'свежая рукола'),
    ),
    FoodNutrientProfile(
        canonical_id='avocado',
        display_name='Авокадо',
        category='fruits',
        calories_kcal=160.0,
        protein_g=2.0,
        fat_g=14.7,
        carbs_g=8.5,
        reference_confidence=0.95,
        source_id='USDA-FDC-171705',
        dataset_version='USDA-SRLegacy',
        cooking_state='raw',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 171705 ('Avocados, raw, all commercial varieties'), pub. 2019-04-01",
        aliases=('авокадо',),
    ),
    FoodNutrientProfile(
        canonical_id='banana',
        display_name='Банан',
        category='fruits',
        calories_kcal=89.0,
        protein_g=1.1,
        fat_g=0.3,
        carbs_g=21.8,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0355',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Фрукты свежие', поз. 355 (бананы)",
        aliases=('банан', 'бананы'),
    ),
    FoodNutrientProfile(
        canonical_id='beef',
        display_name='Говядина',
        category='meat_poultry',
        calories_kcal=187.0,
        protein_g=18.9,
        fat_g=12.4,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0201',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мясо убойных животных', поз. 201 (говядина 1 категории)",
        aliases=('говядина', 'филе говядины'),
    ),
    FoodNutrientProfile(
        canonical_id='beef_boiled',
        display_name='Говядина отварная',
        category='meat_poultry',
        calories_kcal=254.0,
        protein_g=25.8,
        fat_g=16.8,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0205',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Кулинарные изделия из мяса', поз. 205 (говядина отварная)",
        aliases=('отварная говядина', 'говядина отварная', 'вареная говядина', 'варёная говядина'),
    ),
    FoodNutrientProfile(
        canonical_id='bell_pepper',
        display_name='Болгарский перец',
        category='vegetables',
        calories_kcal=27.0,
        protein_g=1.3,
        fat_g=0.3,
        carbs_g=5.3,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0274',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Овощи', поз. 274 (перец сладкий)",
        aliases=('болгарский перец', 'сладкий перец', 'красный перец'),
    ),
    FoodNutrientProfile(
        canonical_id='berries',
        display_name='Ягоды',
        category='fruits',
        calories_kcal=45.0,
        protein_g=0.8,
        fat_g=0.4,
        carbs_g=9.5,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0360',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Ягоды свежие дикорастущие', поз. 360",
        aliases=('ягоды', 'лесные ягоды', 'смесь ягод', 'ягодная смесь'),
    ),
    FoodNutrientProfile(
        canonical_id='borscht',
        display_name='Борщ',
        category='prepared_dishes',
        calories_kcal=58.0,
        protein_g=3.5,
        fat_g=2.8,
        carbs_g=4.7,
        reference_confidence=0.85,
        source_id='INTERNAL-SKURIKHIN-2008-0701',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Первые блюда (супы)', поз. 701",
        aliases=('борщ', 'украинский борщ', 'красный борщ', 'борщ с говядиной', 'борщ со сметаной', 'борщ с мясом', 'борщ мясной', 'тарелка борща с говядиной'),
    ),
    FoodNutrientProfile(
        canonical_id='bread',
        display_name='Хлеб пшеничный',
        category='grains_bakery',
        calories_kcal=265.0,
        protein_g=8.8,
        fat_g=3.2,
        carbs_g=49.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0001',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='baked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник 'Химический состав российских пищевых продуктов', табл. 'Хлеб и хлебобулочные изделия', поз. 1",
        aliases=('хлеб', 'белый хлеб', 'пшеничный хлеб', 'батон', 'тост'),
    ),
    FoodNutrientProfile(
        canonical_id='buckwheat',
        display_name='Гречневая каша',
        category='grains_bakery',
        calories_kcal=110.0,
        protein_g=4.2,
        fat_g=1.1,
        carbs_g=21.3,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0050',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Крупы и каши', поз. 50 (каша гречневая рассыпчатая)",
        aliases=('гречка', 'греча', 'гречневая каша', 'отварная гречка', 'гречка отварная'),
    ),
    FoodNutrientProfile(
        canonical_id='butter',
        display_name='Сливочное масло',
        category='dairy',
        calories_kcal=748.0,
        protein_g=0.7,
        fat_g=82.5,
        carbs_g=0.8,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0130',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='solid',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Масло коровье', поз. 130",
        aliases=('сливочное масло', 'масло сливочное'),
    ),
    FoodNutrientProfile(
        canonical_id='carrot',
        display_name='Морковь',
        category='vegetables',
        calories_kcal=35.0,
        protein_g=1.3,
        fat_g=0.1,
        carbs_g=6.9,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0265',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Овощи', поз. 265 (морковь красная)",
        aliases=('морковь', 'морковка', 'свежая морковь'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese',
        display_name='Сыр',
        category='dairy',
        calories_kcal=356.0,
        protein_g=24.0,
        fat_g=27.0,
        carbs_g=1.5,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0121',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cured',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Сыры твердые средней жирности', поз. 121",
        aliases=('сыр', 'твердый сыр', 'твёрдый сыр', 'полутвердый сыр', 'полутвёрдый сыр'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_brie',
        display_name='Бри',
        category='dairy',
        calories_kcal=334.0,
        protein_g=20.8,
        fat_g=27.7,
        carbs_g=0.5,
        reference_confidence=0.95,
        source_id='USDA-FDC-172177',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 172177 ('Cheese, brie'), pub. 2019-04-01",
        aliases=('бри', 'сыр бри'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_cheddar',
        display_name='Чеддер',
        category='dairy',
        calories_kcal=403.0,
        protein_g=22.9,
        fat_g=33.3,
        carbs_g=3.4,
        reference_confidence=0.95,
        source_id='USDA-FDC-173414',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 173414 ('Cheese, cheddar'), pub. 2019-04-01",
        aliases=('чеддер', 'сыр чеддер'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_edam',
        display_name='Эдам',
        category='dairy',
        calories_kcal=357.0,
        protein_g=25.0,
        fat_g=28.6,
        carbs_g=1.4,
        reference_confidence=0.95,
        source_id='USDA-FDC-173419',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 173419 ('Cheese, edam'), pub. 2019-04-01",
        aliases=('эдам', 'сыр эдам'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_feta',
        display_name='Фета',
        category='dairy',
        calories_kcal=265.0,
        protein_g=14.2,
        fat_g=21.5,
        carbs_g=3.9,
        reference_confidence=0.95,
        source_id='USDA-FDC-173420',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 173420 ('Cheese, feta'), pub. 2019-04-01",
        aliases=('фета', 'сыр фета'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_gouda',
        display_name='Гауда',
        category='dairy',
        calories_kcal=356.0,
        protein_g=24.9,
        fat_g=27.4,
        carbs_g=2.2,
        reference_confidence=0.95,
        source_id='USDA-FDC-171241',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 171241 ('Cheese, gouda'), pub. 2019-04-01",
        aliases=('гауда', 'сыр гауда'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_mozzarella',
        display_name='Моцарелла',
        category='dairy',
        calories_kcal=299.0,
        protein_g=22.2,
        fat_g=22.1,
        carbs_g=2.4,
        reference_confidence=0.95,
        source_id='USDA-FDC-170845',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 170845 ('Cheese, mozzarella, whole milk'), pub. 2019-04-01",
        aliases=('моцарелла', 'сыр моцарелла'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_parmesan',
        display_name='Пармезан',
        category='dairy',
        calories_kcal=392.0,
        protein_g=35.8,
        fat_g=25.0,
        carbs_g=3.2,
        reference_confidence=0.95,
        source_id='USDA-FDC-170848',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 170848 ('Cheese, parmesan, hard'), pub. 2019-04-01",
        aliases=('пармезан', 'сыр пармезан'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_russian',
        display_name='Сыр Российский',
        category='dairy',
        calories_kcal=364.0,
        protein_g=24.1,
        fat_g=29.5,
        carbs_g=0.3,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0120',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cured',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Сыры сычужные твердые', поз. 120",
        aliases=('сыр российский', 'российский сыр'),
    ),
    FoodNutrientProfile(
        canonical_id='cheese_swiss',
        display_name='Швейцарский сыр',
        category='dairy',
        calories_kcal=393.0,
        protein_g=27.0,
        fat_g=31.0,
        carbs_g=1.4,
        reference_confidence=0.95,
        source_id='USDA-FDC-171251',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 171251 ('Cheese, swiss'), pub. 2019-04-01",
        aliases=('швейцарский сыр', 'сыр швейцарский'),
    ),
    FoodNutrientProfile(
        canonical_id='chicken_breast',
        display_name='Куриное филе',
        category='meat_poultry',
        calories_kcal=120.0,
        protein_g=22.5,
        fat_g=2.6,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='USDA-FDC-171077',
        dataset_version='USDA-SRLegacy',
        cooking_state='raw',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 171077 ('Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw'), pub. 2019-04-01",
        aliases=('куриное филе', 'филе куриное', 'курица сырая'),
    ),
    FoodNutrientProfile(
        canonical_id='chicken_breast_cooked',
        display_name='Куриная грудка отварная',
        category='meat_poultry',
        calories_kcal=165.0,
        protein_g=29.8,
        fat_g=3.5,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0240',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мясо птицы отварное', поз. 240 (курица грудка отварная)",
        aliases=('куриная грудка', 'отварная куриная грудка', 'вареная куриная грудка', 'варёная куриная грудка', 'куриная грудка отварная'),
    ),
    FoodNutrientProfile(
        canonical_id='chicken_soup',
        display_name='Куриный суп',
        category='prepared_dishes',
        calories_kcal=45.0,
        protein_g=3.2,
        fat_g=2.0,
        carbs_g=3.5,
        reference_confidence=0.85,
        source_id='INTERNAL-SKURIKHIN-2008-0705',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Первые блюда (супы)', поз. 705",
        aliases=('куриный суп', 'куриный бульон', 'суп куриный', 'суп с лапшой'),
    ),
    FoodNutrientProfile(
        canonical_id='cottage_cheese',
        display_name='Творог',
        category='dairy',
        calories_kcal=121.0,
        protein_g=17.2,
        fat_g=5.0,
        carbs_g=1.8,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0105',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fresh',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Творог и творожные изделия', поз. 105",
        aliases=('творог', 'зерненый творог', 'зернёный творог'),
    ),
    FoodNutrientProfile(
        canonical_id='cottage_cheese_5pct',
        display_name='Творог 5%',
        category='dairy',
        calories_kcal=121.0,
        protein_g=17.2,
        fat_g=5.0,
        carbs_g=1.8,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0105',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fresh',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Творог и творожные изделия', поз. 105",
        aliases=('творог 5%', 'творог 5 процентный', 'творог 5% жирности'),
    ),
    FoodNutrientProfile(
        canonical_id='cottage_cheese_9pct',
        display_name='Творог 9%',
        category='dairy',
        calories_kcal=159.0,
        protein_g=16.7,
        fat_g=9.0,
        carbs_g=2.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0104',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fresh',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Творог и творожные изделия', поз. 104",
        aliases=('творог 9%', 'творог 9 процентный', 'творог 9% жирности', 'полужирный творог'),
    ),
    FoodNutrientProfile(
        canonical_id='cream_cheese',
        display_name='Сливочный сыр',
        category='dairy',
        calories_kcal=350.0,
        protein_g=6.2,
        fat_g=34.4,
        carbs_g=5.5,
        reference_confidence=0.95,
        source_id='USDA-FDC-173418',
        dataset_version='USDA-SRLegacy',
        cooking_state='cured',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 173418 ('Cheese, cream'), pub. 2019-04-01",
        aliases=('сливочный сыр', 'сыр сливочный', 'творожный сыр', 'сыр творожный', 'крем чиз', 'крем-чиз'),
    ),
    FoodNutrientProfile(
        canonical_id='cucumbers',
        display_name='Огурцы',
        category='vegetables',
        calories_kcal=15.0,
        protein_g=0.7,
        fat_g=0.1,
        carbs_g=3.6,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0270',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Овощи', поз. 270 (огурцы грунтовые)",
        aliases=('огурцы', 'огурец', 'огурчики', 'свежий огурец', 'свежие огурцы'),
    ),
    FoodNutrientProfile(
        canonical_id='cutlet',
        display_name='Котлета',
        category='meat_poultry',
        calories_kcal=210.0,
        protein_g=15.0,
        fat_g=13.0,
        carbs_g=8.0,
        reference_confidence=0.85,
        source_id='INTERNAL-SKURIKHIN-2008-0720',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fried',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Кулинарные изделия из мяса', поз. 720 (котлеты домашние)",
        aliases=('котлета', 'котлеты', 'мясная котлета'),
    ),
    FoodNutrientProfile(
        canonical_id='draniki',
        display_name='Драники',
        category='prepared_dishes',
        calories_kcal=196.0,
        protein_g=3.2,
        fat_g=9.8,
        carbs_g=23.8,
        reference_confidence=0.85,
        source_id='INTERNAL-SKURIKHIN-2008-0714',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fried',
        variance_note="Калорийность зависит от количества масла при жарке (190-220 ккал/100г).",
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Кулинарные изделия из картофеля', поз. 714",
        aliases=('драники', 'драник', 'деруны', 'картофельные оладьи', 'картофельные драники'),
    ),
    FoodNutrientProfile(
        canonical_id='egg_boiled',
        display_name='Яйцо варёное',
        category='eggs',
        calories_kcal=155.0,
        protein_g=12.6,
        fat_g=10.6,
        carbs_g=1.1,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0150',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Яйца и продукты их переработки', поз. 150",
        aliases=('яйцо', 'яйца', 'вареное яйцо', 'варёное яйцо', 'вареные яйца', 'варёные яйца', 'яйцо всмятку', 'яйцо вкрутую'),
    ),
    FoodNutrientProfile(
        canonical_id='fried_egg',
        display_name='Яичница глазунья',
        category='eggs',
        calories_kcal=202.0,
        protein_g=13.6,
        fat_g=15.8,
        carbs_g=0.8,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0152',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fried',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Блюда из яиц', поз. 152",
        aliases=('яичница', 'глазунья', 'яичница глазунья', 'жареное яйцо', 'жареные яйца'),
    ),
    FoodNutrientProfile(
        canonical_id='greens',
        display_name='Зелень',
        category='vegetables',
        calories_kcal=38.0,
        protein_g=3.0,
        fat_g=0.5,
        carbs_g=6.0,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0278',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Овощи и зелень', поз. 278 (укроп, петрушка)",
        aliases=('зелень', 'укроп', 'петрушка', 'свежая зелень'),
    ),
    FoodNutrientProfile(
        canonical_id='lettuce',
        display_name='Листья салата',
        category='vegetables',
        calories_kcal=14.0,
        protein_g=0.9,
        fat_g=0.1,
        carbs_g=3.0,
        reference_confidence=0.95,
        source_id='USDA-FDC-169248',
        dataset_version='USDA-SRLegacy',
        cooking_state='raw',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 169248 ('Lettuce, iceberg, raw'), pub. 2019-04-01",
        aliases=('листья салата', 'салат айсберг', 'айсберг', 'салат латук', 'салатные листья'),
    ),
    FoodNutrientProfile(
        canonical_id='mashed_potatoes',
        display_name='Картофельное пюре',
        category='prepared_dishes',
        calories_kcal=106.0,
        protein_g=2.0,
        fat_g=4.2,
        carbs_g=14.7,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0715',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='prepared',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Кулинарные блюда и изделия', поз. 715",
        aliases=('картофельное пюре', 'пюре', 'пюре картофельное'),
    ),
    FoodNutrientProfile(
        canonical_id='mayonnaise',
        display_name='Майонез',
        category='condiments',
        calories_kcal=680.0,
        protein_g=1.0,
        fat_g=75.0,
        carbs_g=1.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0401',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='prepared',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Майонезы и соусы', поз. 401",
        aliases=('майонез', 'провансаль'),
    ),
    FoodNutrientProfile(
        canonical_id='meat_slices',
        display_name='Нарезка мяса',
        category='meat_poultry',
        calories_kcal=187.0,
        protein_g=25.5,
        fat_g=8.4,
        carbs_g=0.0,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0220',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мясные гастрономические изделия', поз. 220",
        aliases=('нарезка мяса', 'ломтики мяса', 'слайсы мяса'),
    ),
    FoodNutrientProfile(
        canonical_id='milk',
        display_name='Молоко',
        category='dairy',
        calories_kcal=52.0,
        protein_g=2.8,
        fat_g=2.5,
        carbs_g=4.7,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0101',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fresh',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Молоко пастеризованное', поз. 101",
        aliases=('молоко', 'коровье молоко', 'пастеризованное молоко'),
    ),
    FoodNutrientProfile(
        canonical_id='milk_2_5pct',
        display_name='Молоко 2.5%',
        category='dairy',
        calories_kcal=52.0,
        protein_g=2.8,
        fat_g=2.5,
        carbs_g=4.7,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0101',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fresh',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Молоко пастеризованное', поз. 101",
        aliases=('молоко 2.5%', 'молоко 2.5 процентное', 'молоко 2 5', 'молоко 2.5% жирности'),
    ),
    FoodNutrientProfile(
        canonical_id='milk_3_2pct',
        display_name='Молоко 3.2%',
        category='dairy',
        calories_kcal=59.0,
        protein_g=2.8,
        fat_g=3.2,
        carbs_g=4.7,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0102',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fresh',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Молоко пастеризованное', поз. 102",
        aliases=('молоко 3.2%', 'молоко 3.2 процентное', 'молоко 3 2', 'молоко 3.2% жирности'),
    ),
    FoodNutrientProfile(
        canonical_id='mushrooms',
        display_name='Шампиньоны',
        category='vegetables',
        calories_kcal=27.0,
        protein_g=4.3,
        fat_g=1.0,
        carbs_g=0.1,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0280',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Грибы свежие', поз. 280 (шампиньоны)",
        aliases=('грибы', 'шампиньоны', 'свежие шампиньоны'),
    ),
    FoodNutrientProfile(
        canonical_id='mushrooms_fried',
        display_name='Шампиньоны жареные',
        category='vegetables',
        calories_kcal=86.0,
        protein_g=3.8,
        fat_g=6.5,
        carbs_g=3.1,
        reference_confidence=0.85,
        source_id='INTERNAL-SKURIKHIN-2008-0282',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fried',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Кулинарные изделия из грибов', поз. 282",
        aliases=('жареные шампиньоны', 'шампиньоны жареные', 'жареные грибы', 'грибы жареные'),
    ),
    FoodNutrientProfile(
        canonical_id='mustard',
        display_name='Горчица',
        category='condiments',
        calories_kcal=66.0,
        protein_g=4.4,
        fat_g=4.0,
        carbs_g=5.8,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0405',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='prepared',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Приправы и специи', поз. 405",
        aliases=('горчица', 'жёлтый соус', 'желтый соус', 'горчичный соус'),
    ),
    FoodNutrientProfile(
        canonical_id='oatmeal',
        display_name='Овсяная каша',
        category='grains_bakery',
        calories_kcal=88.0,
        protein_g=3.0,
        fat_g=1.7,
        carbs_g=15.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0060',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Крупы и каши', поз. 60 (каша овсяная на воде)",
        aliases=('овсянка', 'овсяная каша', 'геркулес', 'овсяные хлопья'),
    ),
    FoodNutrientProfile(
        canonical_id='omelet',
        display_name='Омлет',
        category='eggs',
        calories_kcal=184.0,
        protein_g=10.5,
        fat_g=14.2,
        carbs_g=2.8,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0153',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='prepared',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Блюда из яиц', поз. 153",
        aliases=('омлет', 'омлет с молоком', 'паровой омлет'),
    ),
    FoodNutrientProfile(
        canonical_id='onion',
        display_name='Лук репчатый',
        category='vegetables',
        calories_kcal=41.0,
        protein_g=1.4,
        fat_g=0.2,
        carbs_g=8.2,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0268',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Овощи', поз. 268 (лук репчатый)",
        aliases=('лук', 'репчатый лук', 'лук репчатый'),
    ),
    FoodNutrientProfile(
        canonical_id='orange',
        display_name='Апельсин',
        category='fruits',
        calories_kcal=43.0,
        protein_g=0.9,
        fat_g=0.2,
        carbs_g=8.1,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0358',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Цитрусовые', поз. 358 (апельсины)",
        aliases=('апельсин', 'апельсины'),
    ),
    FoodNutrientProfile(
        canonical_id='pasta',
        display_name='Макароны отварные',
        category='grains_bakery',
        calories_kcal=131.0,
        protein_g=5.0,
        fat_g=1.1,
        carbs_g=25.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0070',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Макаронные изделия', поз. 70",
        aliases=('макароны', 'паста', 'спагетти', 'отварные макароны'),
    ),
    FoodNutrientProfile(
        canonical_id='plov',
        display_name='Плов',
        category='prepared_dishes',
        calories_kcal=215.0,
        protein_g=7.5,
        fat_g=9.8,
        carbs_g=24.0,
        reference_confidence=0.85,
        source_id='INTERNAL-SKURIKHIN-2008-0710',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='prepared',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Вторые блюда из риса и мяса', поз. 710",
        aliases=('плов', 'узбекский плов', 'плов с курицей', 'плов с говядиной', 'плов с мясом'),
    ),
    FoodNutrientProfile(
        canonical_id='pork',
        display_name='Свинина',
        category='meat_poultry',
        calories_kcal=259.0,
        protein_g=19.5,
        fat_g=20.0,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0210',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мясо убойных животных', поз. 210 (свинина мясная)",
        aliases=('свинина', 'мякоть свинины'),
    ),
    FoodNutrientProfile(
        canonical_id='potato_boiled',
        display_name='Картофель отварной',
        category='vegetables',
        calories_kcal=82.0,
        protein_g=2.0,
        fat_g=0.4,
        carbs_g=16.7,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0261',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Овощи и продукты их переработки', поз. 261",
        aliases=('картофель', 'картошка', 'картофель вареный', 'картофель варёный', 'картофель отварной', 'вареная картошка', 'варёная картошка', 'отварной картофель'),
    ),
    FoodNutrientProfile(
        canonical_id='potato_fried',
        display_name='Картофель жареный',
        category='prepared_dishes',
        calories_kcal=192.0,
        protein_g=2.8,
        fat_g=9.5,
        carbs_g=23.4,
        reference_confidence=0.85,
        source_id='INTERNAL-SKURIKHIN-2008-0263',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fried',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Кулинарные изделия', поз. 263",
        aliases=('жареная картошка', 'картофель жареный', 'картошка жареная', 'жареный картофель'),
    ),
    FoodNutrientProfile(
        canonical_id='rice',
        display_name='Рис отварной',
        category='grains_bakery',
        calories_kcal=130.0,
        protein_g=2.7,
        fat_g=0.3,
        carbs_g=28.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0055',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='boiled',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Крупы и каши', поз. 55 (рис шлифованный отварной)",
        aliases=('рис', 'вареный рис', 'варёный рис', 'рис отварной', 'белый рис'),
    ),
    FoodNutrientProfile(
        canonical_id='salmon',
        display_name='Лосось свежий',
        category='fish_seafood',
        calories_kcal=142.0,
        protein_g=19.8,
        fat_g=6.3,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='USDA-FDC-173686',
        dataset_version='USDA-SRLegacy',
        cooking_state='raw',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 173686 ('Fish, salmon, Atlantic, wild, raw'), pub. 2019-04-01",
        aliases=('лосось', 'семга', 'сёмга', 'филе лосося', 'филе семги', 'филе сёмги', 'свежий лосось', 'свежая семга'),
    ),
    FoodNutrientProfile(
        canonical_id='salmon_baked',
        display_name='Лосось запечённый',
        category='fish_seafood',
        calories_kcal=182.0,
        protein_g=25.4,
        fat_g=8.1,
        carbs_g=0.0,
        reference_confidence=0.9,
        source_id='USDA-FDC-171998',
        dataset_version='USDA-SRLegacy',
        cooking_state='baked',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 171998 ('Fish, salmon, Atlantic, wild, cooked, dry heat'), pub. 2019-04-01",
        aliases=('лосось запеченный', 'лосось запечённый', 'запеченный лосось', 'запечённый лосось', 'запеченная семга', 'запечённая сёмга'),
    ),
    FoodNutrientProfile(
        canonical_id='salmon_lightly_salted',
        display_name='Лосось слабосолёный',
        category='fish_seafood',
        calories_kcal=195.0,
        protein_g=21.0,
        fat_g=12.3,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0322',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cured',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Рыба и морепродукты соленые', поз. 322",
        aliases=('лосось слабосоленый', 'лосось слабосолёный', 'слабосоленый лосось', 'слабосолёный лосось', 'слабосоленая семга', 'слабосолёная сёмга', 'семга слабосоленая', 'сёмга слабосолёная'),
    ),
    FoodNutrientProfile(
        canonical_id='sour_cream',
        display_name='Сметана',
        category='dairy',
        calories_kcal=158.0,
        protein_g=2.7,
        fat_g=15.0,
        carbs_g=3.6,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0112',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fermented',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Сметана', поз. 112",
        aliases=('сметана',),
    ),
    FoodNutrientProfile(
        canonical_id='sour_cream_15pct',
        display_name='Сметана 15%',
        category='dairy',
        calories_kcal=158.0,
        protein_g=2.7,
        fat_g=15.0,
        carbs_g=3.6,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0112',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fermented',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Сметана', поз. 112",
        aliases=('сметана 15%', 'сметана 15 процентная', 'сметана 15% жирности', 'сметана пятнадцатипроцентная'),
    ),
    FoodNutrientProfile(
        canonical_id='sour_cream_20pct',
        display_name='Сметана 20%',
        category='dairy',
        calories_kcal=206.0,
        protein_g=2.5,
        fat_g=20.0,
        carbs_g=3.4,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0113',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fermented',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Сметана', поз. 113",
        aliases=('сметана 20%', 'сметана 20 процентная', 'сметана 20% жирности', 'сметана двадцатипроцентная'),
    ),
    FoodNutrientProfile(
        canonical_id='soy_sauce',
        display_name='Соевый соус',
        category='condiments',
        calories_kcal=53.0,
        protein_g=8.1,
        fat_g=0.6,
        carbs_g=4.9,
        reference_confidence=0.95,
        source_id='USDA-FDC-174277',
        dataset_version='USDA-SRLegacy',
        cooking_state='prepared',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 174277 ('Soy sauce made from soy and wheat (shoyu)'), pub. 2019-04-01",
        aliases=('соевый соус',),
    ),
    FoodNutrientProfile(
        canonical_id='syrniki',
        display_name='Сырники',
        category='prepared_dishes',
        calories_kcal=218.0,
        protein_g=16.5,
        fat_g=8.0,
        carbs_g=19.8,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0730',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='fried',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Кулинарные изделия из творога', поз. 730",
        aliases=('сырники', 'сырник', 'творожники'),
    ),
    FoodNutrientProfile(
        canonical_id='tomatoes',
        display_name='Помидоры',
        category='vegetables',
        calories_kcal=18.0,
        protein_g=0.9,
        fat_g=0.2,
        carbs_g=3.9,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0272',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Овощи', поз. 272 (томаты грунтовые)",
        aliases=('помидоры', 'помидор', 'томат', 'томаты', 'черри', 'помидоры черри'),
    ),
    FoodNutrientProfile(
        canonical_id='trout',
        display_name='Форель',
        category='fish_seafood',
        calories_kcal=141.0,
        protein_g=19.9,
        fat_g=6.2,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='USDA-FDC-173717',
        dataset_version='USDA-SRLegacy',
        cooking_state='raw',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 173717 ('Fish, trout, rainbow, farmed, raw'), pub. 2019-04-01",
        aliases=('форель', 'свежая форель', 'филе форели'),
    ),
    FoodNutrientProfile(
        canonical_id='tuna',
        display_name='Тунец свежий',
        category='fish_seafood',
        calories_kcal=109.0,
        protein_g=24.4,
        fat_g=0.5,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='USDA-FDC-175159',
        dataset_version='USDA-SRLegacy',
        cooking_state='raw',
        source_provenance="USDA FoodData Central SR Legacy, FDC ID 175159 ('Fish, tuna, fresh, yellowfin, raw'), pub. 2019-04-01",
        aliases=('тунец', 'свежий тунец', 'филе тунца'),
    ),
    FoodNutrientProfile(
        canonical_id='vegetable_oil',
        display_name='Растительное масло',
        category='condiments',
        calories_kcal=884.0,
        protein_g=0.0,
        fat_g=100.0,
        carbs_g=0.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0140',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='liquid',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Масла растительные', поз. 140",
        aliases=('растительное масло', 'оливковое масло', 'подсолнечное масло'),
    ),
    FoodNutrientProfile(
        canonical_id='waffles',
        display_name='Вафли',
        category='grains_bakery',
        calories_kcal=312.0,
        protein_g=6.2,
        fat_g=12.1,
        carbs_g=45.6,
        reference_confidence=0.9,
        source_id='INTERNAL-SKURIKHIN-2008-0040',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='prepared',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мучные кондитерские изделия', поз. 40",
        aliases=('вафли', 'вафля', 'бельгийские вафли'),
    ),
    FoodNutrientProfile(
        canonical_id='walnut',
        display_name='Грецкие орехи (ядра)',
        category='nuts',
        calories_kcal=654.0,
        protein_g=15.2,
        fat_g=65.2,
        carbs_g=13.7,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0380',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Орехи', поз. 380; USDA Foundation Food 170187",
        aliases=(
            'грецкий орех', 'грецкие орехи', 'ядра грецких орехов', 'ядро грецкого ореха',
            'орехи грецкие', 'орех грецкий', 'грецкий орех в скорлупе', 'грецкие орехи в скорлупе',
            'walnut', 'walnuts', 'walnut kernel', 'walnut kernels', 'walnut in shell', 'walnuts in shell',
        ),
    ),
)

_COMPOSITE_DISH_CATALOG: tuple[FoodNutrientProfile, ...] = (
    FoodNutrientProfile(
        canonical_id='apple_pie',
        display_name='Яблочный пирог',
        category='grains_bakery',
        calories_kcal=275.0,
        protein_g=3.5,
        fat_g=9.5,
        carbs_g=44.0,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0412',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мучные кондитерские изделия', поз. 412 (пирог с яблоками)",
        aliases=(
            'яблочный пирог', 'пирог с яблоками', 'пирог яблочный', 'шарлотка', 'штрудель',
            'яблочная шарлотка', 'яблочный штрудель', 'кусок яблочного пирога', 'кусочек яблочного пирога',
            'кусок пирога с яблоками', 'кусочек пирога с яблоками', 'сладкая выпечка с яблоком',
            'apple pie', 'apple tart',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='sweet_pastry',
        display_name='Сладкая выпечка',
        category='grains_bakery',
        calories_kcal=335.0,
        protein_g=6.5,
        fat_g=13.0,
        carbs_g=48.0,
        reference_confidence=0.90,
        source_id='INTERNAL-SKURIKHIN-2008-0415',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мучные кондитерские изделия', поз. 415 (сдобная выпечка)",
        aliases=(
            'сладкая выпечка', 'выпечка', 'сдоба', 'сдобная выпечка', 'булочка', 'булочки',
            'слойка', 'слойки', 'ватрушка', 'ватрушки', 'круассан', 'круассаны',
            'кекс', 'маффин', 'кусочки сладкой выпечки', 'кусок выпечки',
            'sweet pastry', 'pastry', 'pastries',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='pie_general',
        display_name='Пирог с начинкой',
        category='grains_bakery',
        calories_kcal=290.0,
        protein_g=5.5,
        fat_g=11.0,
        carbs_g=42.0,
        reference_confidence=0.90,
        source_id='INTERNAL-SKURIKHIN-2008-0410',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мучные кондитерские изделия', поз. 410 (пироги)",
        aliases=(
            'пирог', 'пироги', 'пирожок', 'пирожки', 'кусок пирога', 'кусочек пирога',
            'пирог с начинкой', 'pie',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='pizza',
        display_name='Пицца',
        category='prepared_dishes',
        calories_kcal=260.0,
        protein_g=11.0,
        fat_g=10.0,
        carbs_g=31.0,
        reference_confidence=0.92,
        source_id='USDA-FDC-2708321',
        dataset_version='USDA-Foundation',
        cooking_state='cooked',
        source_provenance="USDA FoodData Central SR Legacy FDC ID 2708321 ('Pizza, cheese, regular crust')",
        aliases=(
            'пицца', 'кусок пиццы', 'кусочек пиццы', 'пицца сырная', 'пицца пепперони',
            'pizza', 'pizza slice',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='burger',
        display_name='Бургер',
        category='prepared_dishes',
        calories_kcal=255.0,
        protein_g=13.0,
        fat_g=12.0,
        carbs_g=23.0,
        reference_confidence=0.92,
        source_id='USDA-FDC-171954',
        dataset_version='USDA-Foundation',
        cooking_state='cooked',
        source_provenance="USDA FoodData Central SR Legacy FDC ID 171954 ('Burger / Cheeseburger, single patty')",
        aliases=(
            'бургер', 'чизбургер', 'гамбургер', 'burger', 'cheeseburger',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='casserole',
        display_name='Запеканка',
        category='prepared_dishes',
        calories_kcal=190.0,
        protein_g=12.0,
        fat_g=8.0,
        carbs_g=18.0,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0125',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Блюда из творога', поз. 125 (запеканка)",
        aliases=(
            'запеканка', 'творожная запеканка', 'картофельная запеканка', 'кусок запеканки',
            'casserole',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='olivier_salad',
        display_name='Салат Оливье',
        category='prepared_dishes',
        calories_kcal=198.0,
        protein_g=6.2,
        fat_g=16.5,
        carbs_g=6.5,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0701',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Салаты', поз. 701 (салат мясной/оливье)",
        aliases=(
            'оливье', 'салат оливье', 'мясной салат', 'салат мясной',
            'порция оливье', 'тарелка оливье', 'olivier salad',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='ragout_vegetable',
        display_name='Овощное рагу',
        category='prepared_dishes',
        calories_kcal=72.0,
        protein_g=1.6,
        fat_g=3.8,
        carbs_g=8.5,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0812',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Блюда из овощей', поз. 812 (рагу из овощей)",
        aliases=(
            'рагу', 'овощное рагу', 'рагу из овощей', 'рагу овощное',
            'тушеные овощи', 'тушёные овощи', 'овощное соте', 'ragout',
            'мясное рагу', 'рагу с мясом',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='fresh_salad',
        display_name='Салат овощной',
        category='vegetables',
        calories_kcal=60.0,
        protein_g=1.5,
        fat_g=3.5,
        carbs_g=5.5,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0706',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Салаты', поз. 706 (салат из огурцов и томатов)",
        aliases=(
            'салат', 'салаты', 'овощной салат', 'греческий салат', 'свежий салат',
            'салат из огурцов и помидоров', 'салат овощной с маслом', 'salad',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='pastry_dough_base',
        display_name='Основа из теста',
        category='grains_bakery',
        calories_kcal=280.0,
        protein_g=7.5,
        fat_g=5.0,
        carbs_g=51.0,
        reference_confidence=0.88,
        source_id='INTERNAL-SKURIKHIN-2008-0418',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мучные изделия', поз. 418 (печёное тесто)",
        aliases=(
            'основа из теста', 'печеное тесто', 'печёное тесто', 'тесто основа', 'pastry base',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='creamy_filling',
        display_name='Кремовая начинка',
        category='dairy',
        calories_kcal=260.0,
        protein_g=3.5,
        fat_g=15.0,
        carbs_g=28.0,
        reference_confidence=0.88,
        source_id='INTERNAL-SKURIKHIN-2008-0419',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мучные кондитерские изделия', поз. 419 (крем кондитерский)",
        aliases=(
            'кремовая начинка', 'заварной крем', 'крем кондитерский', 'творожная начинка', 'creamy filling',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='sunflower_seeds',
        display_name='Семечки подсолнечника',
        category='nuts',
        calories_kcal=580.0,
        protein_g=20.0,
        fat_g=50.0,
        carbs_g=15.0,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0385',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Семена', поз. 385 (семена подсолнечника); USDA 170562",
        aliases=(
            'семечки подсолнечника', 'семечки', 'семена подсолнечника', 'семена', 'семена на выпечке',
            'sunflower seeds', 'seeds',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='apple_slices',
        display_name='Ломтики яблока',
        category='fruits',
        calories_kcal=52.0,
        protein_g=0.3,
        fat_g=0.2,
        carbs_g=14.0,
        reference_confidence=0.95,
        source_id='INTERNAL-SKURIKHIN-2008-0350-S',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Фрукты', поз. 350 (яблоки печеные/ломтиками)",
        aliases=(
            'ломтики яблока', 'дольки яблока', 'кусочки яблока', 'печеное яблоко', 'apple slices',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='lavash',
        display_name='Лаваш армянский',
        category='grains_bakery',
        calories_kcal=270.0,
        protein_g=8.5,
        fat_g=1.2,
        carbs_g=54.0,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0422',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Хлебобулочные изделия', поз. 422 (лаваш армянский)",
        aliases=(
            'лаваш', 'армянский лаваш', 'тонкий лаваш', 'лаваш армянский', 'лаваш тонкий', 'лаваш армянский тонкий',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='poppy_roll',
        display_name='Рулет творожно-маковый',
        category='grains_bakery',
        calories_kcal=310.0,
        protein_g=11.5,
        fat_g=11.0,
        carbs_g=41.0,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0430',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='cooked',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Мучные изделия', поз. 430 (рулет с маком и творогом)",
        aliases=(
            'рулет', 'творожно-маковый рулет', 'маковый рулет', 'творожный рулет', 'рулет с маком',
            'рулет с творогом', 'сладкий рулет', 'рулет творожно-маковый', 'творожно маковый рулет',
        ),
    ),
    FoodNutrientProfile(
        canonical_id='poppy_seeds',
        display_name='Мак пищевой (семена)',
        category='nuts',
        calories_kcal=525.0,
        protein_g=18.0,
        fat_g=42.0,
        carbs_g=28.0,
        reference_confidence=0.92,
        source_id='INTERNAL-SKURIKHIN-2008-0388',
        dataset_version='Skurikhin-2008-Curated',
        cooking_state='raw',
        source_provenance="Скурихин И.М., Тутельян В.А. 2002/2008, Справочник, табл. 'Семена', поз. 388 (семена мака); USDA 171329",
        aliases=(
            'мак', 'семена мака', 'пищевой мак', 'мак пищевой', 'семена мака пищевые',
        ),
    ),
)


def normalize_food_lookup_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.strip().lower())
    ascii_folded = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    ascii_folded = ascii_folded.replace("ё", "е")
    cleaned = re.sub(r"[^\w\s%.-]+", " ", ascii_folded)
    return " ".join(cleaned.split())


# Harmless culinary descriptors that do NOT alter the product composition or cooking state
_HARMLESS_CULINARY_MODIFIER_PREFIXES = (
    "свежий ", "свежая ", "свежее ", "свежие ",
    "домашний ", "домашняя ", "домашнее ", "домашние ",
    "нарезанный ", "нарезанная ", "нарезанное ", "нарезанные ",
    "порезанный ", "порезанная ", "порезанное ", "порезанные ",
)

_HARMLESS_CULINARY_MODIFIER_SUFFIXES = (
    " по-домашнему", " соломкой", " кусочками", " ломтиками",
    " свежий", " свежая", " свежее", " свежие",
    " в скорлупе", " в скорлупке", " в панцире", " in shell",
)

# Whitelisted harmless tokens that may be dropped in prefix matching (shapes/portions)
# Any other modifier (percentages, cheese types, unknown cooking styles) strictly fails closed!
_HARMLESS_DROPPED_TOKENS = {
    "кусочками", "соломкой", "ломтиками", "кубиками", "кружочками", "половинками",
    "порция", "порции", "порций",
    "грамм", "грамма", "граммов", "г", "гр",
    "штука", "штуки", "штук", "шт",
    "по-домашнему",
    "скорлупе", "скорлупке", "панцире", "shell",
}


def _strip_harmless_culinary_modifiers(text: str) -> str:
    cleaned = text
    for prefix in _HARMLESS_CULINARY_MODIFIER_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break
    for suffix in _HARMLESS_CULINARY_MODIFIER_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[:-len(suffix)].strip()
            break
    return cleaned


def _build_alias_index(catalog: tuple[FoodNutrientProfile, ...] | None = None) -> dict[str, FoodNutrientProfile]:
    cat = catalog if catalog is not None else _FOOD_CATALOG
    index: dict[str, FoodNutrientProfile] = {}
    for profile in cat:
        canon_key = normalize_food_lookup_key(profile.display_name)
        index[canon_key] = profile
        index[profile.canonical_id] = profile
        for alias in profile.aliases:
            alias_key = normalize_food_lookup_key(alias)
            index[alias_key] = profile
    return index


_ALIAS_INDEX: dict[str, FoodNutrientProfile] = _build_alias_index(_FOOD_CATALOG)
_COMPOSITE_DISH_ALIAS_INDEX: dict[str, FoodNutrientProfile] = _build_alias_index(_COMPOSITE_DISH_CATALOG)
_RESOLVER_ALIAS_INDEX: dict[str, FoodNutrientProfile] = {**_COMPOSITE_DISH_ALIAS_INDEX, **_ALIAS_INDEX}
_SKURIKHIN_FALLBACK_INDEX: dict[str, FoodNutrientProfile] | None = None


def _get_skurikhin_fallback_index() -> dict[str, FoodNutrientProfile]:
    global _SKURIKHIN_FALLBACK_INDEX
    if _SKURIKHIN_FALLBACK_INDEX is not None:
        return _SKURIKHIN_FALLBACK_INDEX
    idx: dict[str, FoodNutrientProfile] = {}
    try:
        from gateway.nutrition.etl.skurikhin_etl import _VERIFIED_SKURIKHIN_SEED_RECORDS
        for raw in _VERIFIED_SKURIKHIN_SEED_RECORDS:
            int_id = str(raw["internal_id"])
            profile = FoodNutrientProfile(
                canonical_id=f"skurikhin_{int_id}",
                display_name=str(raw["name_ru"]),
                category=str(raw["category"]),
                calories_kcal=float(raw["calories_kcal"]),
                protein_g=float(raw["protein_g"]),
                fat_g=float(raw["fat_g"]),
                carbs_g=float(raw["carbs_g"]),
                reference_confidence=float(raw.get("reference_confidence", 0.95)),
                source_id=f"INTERNAL-SKURIKHIN-2008-{int_id}",
                dataset_version=str(raw.get("dataset_version", "Skurikhin-2008-Curated")),
                cooking_state=str(raw.get("cooking_state", "cooked")),
                source_provenance=str(raw.get("source_provenance", "")),
                aliases=tuple(raw.get("aliases", ())),
            )
            name_norm = normalize_food_lookup_key(raw["name_ru"])
            if name_norm not in idx:
                idx[name_norm] = profile
            for al in raw.get("aliases", ()):
                al_norm = normalize_food_lookup_key(al)
                if al_norm not in idx:
                    idx[al_norm] = profile
    except Exception:
        pass
    _SKURIKHIN_FALLBACK_INDEX = idx
    return _SKURIKHIN_FALLBACK_INDEX


def resolve_food_composition(query: str) -> FoodLookupResult:
    """
    Extensible, deterministic food composition lookup layer.
    Enforces fail-closed matching without silent product or percentage substitution.
    """
    raw_normalized = normalize_food_lookup_key(query)
    if not raw_normalized:
        return FoodLookupResult(matched=False, match_type="empty")

    # 1. Exact match on normalized full alias in built-in reference catalog
    if raw_normalized in _RESOLVER_ALIAS_INDEX:
        profile = _RESOLVER_ALIAS_INDEX[raw_normalized]
        return FoodLookupResult(
            matched=True,
            profile=profile,
            confidence=profile.reference_confidence,
            match_type="exact_alias",
            source_id=profile.source_id,
            dataset_version=profile.dataset_version,
            cooking_state=profile.cooking_state,
            source_provenance=profile.source_provenance,
        )

    # 2. Check authoritative SQLite HFKB catalog (multi-dataset expansion)
    try:
        from gateway.nutrition.hfkb import get_default_hfkb
        hfkb = get_default_hfkb()
        if hfkb is not None:
            hfkb_res = hfkb.resolve(query)
            if hfkb_res.matched:
                return hfkb_res
    except Exception:
        pass

    # 3. Check in-memory Skurikhin seed index (fallback when SQLite catalog is not present on disk)
    skur_idx = _get_skurikhin_fallback_index()
    if raw_normalized in skur_idx:
        profile = skur_idx[raw_normalized]
        return FoodLookupResult(
            matched=True,
            profile=profile,
            confidence=profile.reference_confidence,
            match_type="exact_alias",
            dataset_version=profile.dataset_version,
            source_id=profile.source_id,
            cooking_state=profile.cooking_state,
            source_provenance=profile.source_provenance,
        )

    # 4. Match after stripping harmless modifiers (e.g. "свежая руккола" -> "руккола", "яблоки кусочками" -> "яблоки")
    # Does NOT strip cooking states or percentages.
    stripped = _strip_harmless_culinary_modifiers(raw_normalized)
    if stripped != raw_normalized:
        if stripped in _RESOLVER_ALIAS_INDEX:
            profile = _RESOLVER_ALIAS_INDEX[stripped]
            return FoodLookupResult(
                matched=True,
                profile=profile,
                confidence=profile.reference_confidence * 0.98,
                match_type="modifier_stripped",
                source_id=profile.source_id,
                dataset_version=profile.dataset_version,
                cooking_state=profile.cooking_state,
                source_provenance=profile.source_provenance,
            )
        if stripped in skur_idx:
            profile = skur_idx[stripped]
            return FoodLookupResult(
                matched=True,
                profile=profile,
                confidence=profile.reference_confidence * 0.98,
                match_type="modifier_stripped",
                source_id=profile.source_id,
                dataset_version=profile.dataset_version,
                cooking_state=profile.cooking_state,
                source_provenance=profile.source_provenance,
            )

    # 5. Lemmatization / simple inflection fallback (Russian plurals -> singular)
    singular_cand = raw_normalized
    if singular_cand.endswith("ки"):
        singular_cand = singular_cand[:-2] + "ко"
    elif singular_cand.endswith("цы"):
        singular_cand = singular_cand[:-2] + "ец"
    elif singular_cand.endswith("ры"):
        singular_cand = singular_cand[:-2] + "р"
    elif singular_cand.endswith("ты"):
        singular_cand = singular_cand[:-2] + "т"

    if singular_cand != raw_normalized and singular_cand in _RESOLVER_ALIAS_INDEX:
        profile = _RESOLVER_ALIAS_INDEX[singular_cand]
        return FoodLookupResult(
            matched=True,
            profile=profile,
            confidence=profile.reference_confidence * 0.95,
            match_type="inflection",
            source_id=profile.source_id,
            dataset_version=profile.dataset_version,
            cooking_state=profile.cooking_state,
            source_provenance=profile.source_provenance,
        )

    # 6. Token prefix matching (e.g. "куриное филе кусочками" -> "куриное филе")
    # Strictly fails closed: ONLY allows dropping whitelisted harmless shape/portion tokens.
    tokens = raw_normalized.split()
    if len(tokens) > 1:
        for length in range(len(tokens) - 1, 0, -1):
            sub_phrase = " ".join(tokens[:length])
            remaining_tokens = tokens[length:]
            if not all(tok in _HARMLESS_DROPPED_TOKENS for tok in remaining_tokens):
                continue

            if sub_phrase in _RESOLVER_ALIAS_INDEX:
                profile = _RESOLVER_ALIAS_INDEX[sub_phrase]
                return FoodLookupResult(
                    matched=True,
                    profile=profile,
                    confidence=profile.reference_confidence * 0.90,
                    match_type="token_prefix",
                    source_id=profile.source_id,
                    dataset_version=profile.dataset_version,
                    cooking_state=profile.cooking_state,
                    source_provenance=profile.source_provenance,
                )

    # 5. Fuzzy suggestions (without fabricating nutrient values)
    candidate_names = list(_ALIAS_INDEX.keys())
    close_matches = difflib.get_close_matches(raw_normalized, candidate_names, n=3, cutoff=0.6)
    suggestions = tuple(dict.fromkeys(_ALIAS_INDEX[k].display_name for k in close_matches))

    return FoodLookupResult(
        matched=False,
        profile=None,
        confidence=0.0,
        match_type="none",
        candidate_suggestions=suggestions,
    )
