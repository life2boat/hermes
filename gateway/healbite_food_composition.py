"""
gateway.healbite_food_composition — Deterministic food composition reference engine.

PROVENANCE & REFERENCE BASIS:
- Russian National Food Composition Tables (Skurikhin I.M., Tutelyan V.A.,
  "Tables of Chemical Composition and Caloric Content of Russian Food Products",
  Federal Research Centre of Nutrition, Biotechnology and Food Safety / RAMS)
- USDA FoodData Central (Foundation Foods & SR Legacy, public domain)

SPECIFICATION:
- Energy basis: kilocalories (kcal) per 100 g edible portion.
- Macronutrients: protein (g), fat (g), carbohydrates (g) per 100 g edible portion.
- Version: 2026.1
- Scope: Standardized raw/cooked ingredients, home dishes, staple foods, and common prepared items.
- Reference confidence: Conservatively graded (0.80 - 0.95) depending on culinary variance.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

NUTRITION_REFERENCE_SOURCE = (
    "Russian National Food Composition Tables (Skurikhin/Tutelyan, FANC Nutrition) "
    "& USDA FoodData Central (Foundation/SR Legacy)"
)
NUTRITION_REFERENCE_PROVENANCE = (
    "Russian National Food Composition Tables (Skurikhin/Tutelyan, 2002-2020) & USDA FDC Public Domain; "
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
    variance_note: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FoodLookupResult:
    matched: bool
    profile: FoodNutrientProfile | None = None
    confidence: float = 0.0
    match_type: str = "none"
    candidate_suggestions: tuple[str, ...] = ()


# -----------------------------------------------------------------------------
# Curated, Documented Food Composition Catalog
# -----------------------------------------------------------------------------

_FOOD_CATALOG: tuple[FoodNutrientProfile, ...] = (
    # Potatoes & potato dishes
    FoodNutrientProfile(
        canonical_id="draniki",
        display_name="Драники",
        category="prepared_dishes",
        calories_kcal=205.0,
        protein_g=3.2,
        fat_g=11.5,
        carbs_g=22.0,
        reference_confidence=0.85,
        variance_note="Калорийность зависит от количества масла при жарке (190-220 ккал/100г).",
        aliases=("драники", "драник", "деруны", "картофельные оладьи", "картофельные драники"),
    ),
    FoodNutrientProfile(
        canonical_id="potato_boiled",
        display_name="Картофель отварной",
        category="vegetables",
        calories_kcal=82.0,
        protein_g=2.0,
        fat_g=0.4,
        carbs_g=16.7,
        reference_confidence=0.95,
        aliases=("картофель", "картошка", "картофель вареный", "картофель отварной", "вареная картошка"),
    ),
    FoodNutrientProfile(
        canonical_id="potato_fried",
        display_name="Картофель жареный",
        category="prepared_dishes",
        calories_kcal=192.0,
        protein_g=2.8,
        fat_g=9.5,
        carbs_g=23.4,
        reference_confidence=0.85,
        variance_note="Калорийность зависит от добавления растительного масла.",
        aliases=("жареная картошка", "картофель жареный", "картошка жареная"),
    ),
    FoodNutrientProfile(
        canonical_id="mashed_potatoes",
        display_name="Картофельное пюре",
        category="prepared_dishes",
        calories_kcal=106.0,
        protein_g=2.0,
        fat_g=4.2,
        carbs_g=14.7,
        reference_confidence=0.90,
        variance_note="Калорийность рассчитана со сливочным маслом и молоком.",
        aliases=("картофельное пюре", "пюре", "пюре картофельное"),
    ),

    # Fish & seafood
    FoodNutrientProfile(
        canonical_id="salmon",
        display_name="Лосось",
        category="fish_seafood",
        calories_kcal=142.0,
        protein_g=20.0,
        fat_g=6.3,
        carbs_g=0.0,
        reference_confidence=0.95,
        aliases=("лосось", "семга", "сёмга", "филе лосося", "филе семги", "лосось запеченный", "лосось на пару"),
    ),
    FoodNutrientProfile(
        canonical_id="salmon_lightly_salted",
        display_name="Лосось слабосолёный",
        category="fish_seafood",
        calories_kcal=195.0,
        protein_g=21.0,
        fat_g=12.0,
        carbs_g=0.0,
        reference_confidence=0.95,
        variance_note="Слабосолёная атлантическая сёмга/лосось.",
        aliases=(
            "лосось слабосоленый",
            "лосось слабосолёный",
            "семга слабосоленая",
            "сёмга слабосолёная",
            "слабосоленый лосось",
            "слабосолёный лосось",
            "слабосоленая семга",
            "гравлакс",
        ),
    ),
    FoodNutrientProfile(
        canonical_id="trout",
        display_name="Форель",
        category="fish_seafood",
        calories_kcal=141.0,
        protein_g=19.5,
        fat_g=6.5,
        carbs_g=0.0,
        reference_confidence=0.95,
        aliases=("форель", "филе форели", "форель запеченная"),
    ),
    FoodNutrientProfile(
        canonical_id="cod",
        display_name="Треска",
        category="fish_seafood",
        calories_kcal=78.0,
        protein_g=17.5,
        fat_g=0.7,
        carbs_g=0.0,
        reference_confidence=0.95,
        aliases=("треска", "филе трески"),
    ),
    FoodNutrientProfile(
        canonical_id="tuna",
        display_name="Тунец",
        category="fish_seafood",
        calories_kcal=130.0,
        protein_g=28.0,
        fat_g=1.0,
        carbs_g=0.0,
        reference_confidence=0.95,
        aliases=("тунец", "филе тунца", "тунец в собственном соку"),
    ),
    FoodNutrientProfile(
        canonical_id="shrimp",
        display_name="Креветки",
        category="fish_seafood",
        calories_kcal=95.0,
        protein_g=20.5,
        fat_g=1.2,
        carbs_g=0.5,
        reference_confidence=0.95,
        aliases=("креветки", "креветка", "отварные креветки"),
    ),

    # Dairy & cheeses
    FoodNutrientProfile(
        canonical_id="cream_cheese",
        display_name="Сливочный сыр",
        category="dairy",
        calories_kcal=342.0,
        protein_g=6.0,
        fat_g=34.0,
        carbs_g=4.0,
        reference_confidence=0.95,
        variance_note="Мягкий сливочный крем-сыр (типа филадельфия).",
        aliases=(
            "сливочный сыр",
            "крем-сыр",
            "крем сыр",
            "сыр сливочный",
            "филадельфия",
            "творожный сыр",
            "маскарпоне",
        ),
    ),
    FoodNutrientProfile(
        canonical_id="cheese",
        display_name="Сыр",
        category="dairy",
        calories_kcal=356.0,
        protein_g=24.0,
        fat_g=27.0,
        carbs_g=2.0,
        reference_confidence=0.95,
        aliases=("сыр", "твердый сыр", "твёрдый сыр", "российский сыр", "гауда", "пармезан", "чеддер"),
    ),
    FoodNutrientProfile(
        canonical_id="cottage_cheese",
        display_name="Творог 5%",
        category="dairy",
        calories_kcal=121.0,
        protein_g=16.0,
        fat_g=5.0,
        carbs_g=3.0,
        reference_confidence=0.95,
        aliases=("творог", "творог 5%", "творог 9%", "зерненый творог", "зернёный творог"),
    ),
    FoodNutrientProfile(
        canonical_id="sour_cream",
        display_name="Сметана 15%",
        category="dairy",
        calories_kcal=158.0,
        protein_g=2.7,
        fat_g=15.0,
        carbs_g=3.6,
        reference_confidence=0.95,
        aliases=("сметана", "сметана 15%", "сметана 20%"),
    ),
    FoodNutrientProfile(
        canonical_id="milk",
        display_name="Молоко 2.5%",
        category="dairy",
        calories_kcal=52.0,
        protein_g=2.8,
        fat_g=2.5,
        carbs_g=4.7,
        reference_confidence=0.95,
        aliases=("молоко", "молоко 2.5%", "молоко 3.2%"),
    ),
    FoodNutrientProfile(
        canonical_id="butter",
        display_name="Сливочное масло",
        category="dairy",
        calories_kcal=748.0,
        protein_g=0.7,
        fat_g=82.5,
        carbs_g=0.8,
        reference_confidence=0.95,
        aliases=("сливочное масло", "масло сливочное"),
    ),

    # Vegetables & greens
    FoodNutrientProfile(
        canonical_id="arugula",
        display_name="Руккола",
        category="vegetables",
        calories_kcal=25.0,
        protein_g=2.6,
        fat_g=0.7,
        carbs_g=3.7,
        reference_confidence=0.95,
        aliases=("руккола", "рукола", "свежая руккола", "свежая рукола"),
    ),
    FoodNutrientProfile(
        canonical_id="cucumbers",
        display_name="Огурцы",
        category="vegetables",
        calories_kcal=15.0,
        protein_g=0.7,
        fat_g=0.1,
        carbs_g=3.6,
        reference_confidence=0.95,
        aliases=("огурцы", "огурец", "огурчики", "огурцы соломкой", "свежий огурец", "свежие огурцы"),
    ),
    FoodNutrientProfile(
        canonical_id="tomatoes",
        display_name="Помидоры",
        category="vegetables",
        calories_kcal=18.0,
        protein_g=0.9,
        fat_g=0.2,
        carbs_g=3.9,
        reference_confidence=0.95,
        aliases=("помидоры", "помидор", "томат", "томаты", "черри", "помидоры черри"),
    ),
    FoodNutrientProfile(
        canonical_id="bell_pepper",
        display_name="Болгарский перец",
        category="vegetables",
        calories_kcal=27.0,
        protein_g=1.3,
        fat_g=0.3,
        carbs_g=5.3,
        reference_confidence=0.95,
        aliases=("болгарский перец", "перец", "сладкий перец", "красный перец"),
    ),
    FoodNutrientProfile(
        canonical_id="carrot",
        display_name="Морковь",
        category="vegetables",
        calories_kcal=35.0,
        protein_g=1.3,
        fat_g=0.1,
        carbs_g=6.9,
        reference_confidence=0.95,
        aliases=("морковь", "морковка", "свежая морковь"),
    ),
    FoodNutrientProfile(
        canonical_id="onion",
        display_name="Лук репчатый",
        category="vegetables",
        calories_kcal=41.0,
        protein_g=1.4,
        fat_g=0.2,
        carbs_g=8.2,
        reference_confidence=0.95,
        aliases=("лук", "репчатый лук", "лук репчатый", "зеленый лук", "зелёный лук"),
    ),
    FoodNutrientProfile(
        canonical_id="greens",
        display_name="Зелень",
        category="vegetables",
        calories_kcal=38.0,
        protein_g=3.0,
        fat_g=0.5,
        carbs_g=6.0,
        reference_confidence=0.95,
        aliases=("зелень", "укроп", "петрушка", "кинза", "шпинат"),
    ),
    FoodNutrientProfile(
        canonical_id="lettuce",
        display_name="Листья салата",
        category="vegetables",
        calories_kcal=14.0,
        protein_g=1.2,
        fat_g=0.2,
        carbs_g=2.2,
        reference_confidence=0.95,
        aliases=("листья салата", "салат айсберг", "айсберг", "салат латук", "салатные листья"),
    ),
    FoodNutrientProfile(
        canonical_id="mushrooms",
        display_name="Шампиньоны",
        category="vegetables",
        calories_kcal=27.0,
        protein_g=4.3,
        fat_g=1.0,
        carbs_g=0.1,
        reference_confidence=0.95,
        aliases=("грибы", "шампиньоны", "жареные грибы", "грибы жареные"),
    ),
    FoodNutrientProfile(
        canonical_id="avocado",
        display_name="Авокадо",
        category="fruits",
        calories_kcal=160.0,
        protein_g=2.0,
        fat_g=14.7,
        carbs_g=8.5,
        reference_confidence=0.95,
        aliases=("авокадо",),
    ),

    # Fruits
    FoodNutrientProfile(
        canonical_id="apple",
        display_name="Яблоко",
        category="fruits",
        calories_kcal=52.0,
        protein_g=0.3,
        fat_g=0.2,
        carbs_g=14.0,
        reference_confidence=0.95,
        aliases=("яблоко", "яблоки", "зеленое яблоко", "красное яблоко", "свежее яблоко"),
    ),
    FoodNutrientProfile(
        canonical_id="banana",
        display_name="Банан",
        category="fruits",
        calories_kcal=89.0,
        protein_g=1.1,
        fat_g=0.3,
        carbs_g=21.8,
        reference_confidence=0.95,
        aliases=("банан", "бананы"),
    ),
    FoodNutrientProfile(
        canonical_id="berries",
        display_name="Ягоды",
        category="fruits",
        calories_kcal=45.0,
        protein_g=0.8,
        fat_g=0.4,
        carbs_g=9.5,
        reference_confidence=0.95,
        aliases=("ягоды", "клубника", "малина", "черника", "голубика"),
    ),
    FoodNutrientProfile(
        canonical_id="orange",
        display_name="Апельсин",
        category="fruits",
        calories_kcal=43.0,
        protein_g=0.9,
        fat_g=0.2,
        carbs_g=8.1,
        reference_confidence=0.95,
        aliases=("апельсин", "апельсины", "мандарин", "мандарины"),
    ),

    # Meat & poultry
    FoodNutrientProfile(
        canonical_id="chicken_breast",
        display_name="Куриная грудка",
        category="meat_poultry",
        calories_kcal=165.0,
        protein_g=31.0,
        fat_g=3.6,
        carbs_g=0.0,
        reference_confidence=0.95,
        aliases=(
            "куриная грудка",
            "курица",
            "куриное филе",
            "филе куриное",
            "индейка",
            "филе индейки",
            "грудка индейки",
        ),
    ),
    FoodNutrientProfile(
        canonical_id="meat_slices",
        display_name="Нарезка мяса",
        category="meat_poultry",
        calories_kcal=187.0,
        protein_g=25.5,
        fat_g=8.4,
        carbs_g=0.0,
        reference_confidence=0.90,
        aliases=("нарезка мяса", "мясо", "ломтики мяса", "слайсы мяса", "куриное мясо"),
    ),
    FoodNutrientProfile(
        canonical_id="beef",
        display_name="Говядина",
        category="meat_poultry",
        calories_kcal=218.0,
        protein_g=26.0,
        fat_g=12.0,
        carbs_g=0.0,
        reference_confidence=0.95,
        aliases=("говядина", "отварная говядина", "говядина отварная", "стейк"),
    ),
    FoodNutrientProfile(
        canonical_id="pork",
        display_name="Свинина",
        category="meat_poultry",
        calories_kcal=259.0,
        protein_g=19.5,
        fat_g=20.0,
        carbs_g=0.0,
        reference_confidence=0.95,
        aliases=("свинина", "запеченная свинина"),
    ),
    FoodNutrientProfile(
        canonical_id="cutlet",
        display_name="Котлета",
        category="prepared_dishes",
        calories_kcal=210.0,
        protein_g=15.0,
        fat_g=13.0,
        carbs_g=8.0,
        reference_confidence=0.85,
        variance_note="Калорийность мясной котлеты с добавлением лука и хлеба.",
        aliases=("котлета", "котлеты", "мясная котлета", "куриная котлета"),
    ),

    # Eggs
    FoodNutrientProfile(
        canonical_id="egg",
        display_name="Яйцо",
        category="eggs",
        calories_kcal=155.0,
        protein_g=12.6,
        fat_g=10.6,
        carbs_g=1.1,
        reference_confidence=0.95,
        aliases=(
            "яйцо",
            "яйца",
            "вареное яйцо",
            "варёное яйцо",
            "вареные яйца",
            "яичница",
            "омлет",
            "глазунья",
        ),
    ),

    # Bakery, cereals & grains
    FoodNutrientProfile(
        canonical_id="bread",
        display_name="Хлеб",
        category="bakery",
        calories_kcal=265.0,
        protein_g=8.8,
        fat_g=3.2,
        carbs_g=49.0,
        reference_confidence=0.95,
        aliases=("хлеб", "белый хлеб", "черный хлеб", "чёрный хлеб", "ржаной хлеб", "батон", "тост"),
    ),
    FoodNutrientProfile(
        canonical_id="waffles",
        display_name="Вафли",
        category="bakery",
        calories_kcal=312.0,
        protein_g=6.2,
        fat_g=12.1,
        carbs_g=45.6,
        reference_confidence=0.90,
        aliases=("вафли", "вафля", "бельгийские вафли"),
    ),
    FoodNutrientProfile(
        canonical_id="buckwheat",
        display_name="Гречневая каша",
        category="grains",
        calories_kcal=110.0,
        protein_g=4.2,
        fat_g=1.1,
        carbs_g=21.3,
        reference_confidence=0.95,
        variance_note="Отварная на воде гречневая крупа.",
        aliases=("гречка", "гречневая каша", "отварная гречка", "гречка отварная"),
    ),
    FoodNutrientProfile(
        canonical_id="rice",
        display_name="Рис отварной",
        category="grains",
        calories_kcal=130.0,
        protein_g=2.7,
        fat_g=0.3,
        carbs_g=28.0,
        reference_confidence=0.95,
        variance_note="Белый отварной рис на воде.",
        aliases=("рис", "вареный рис", "рис отварной", "белый рис"),
    ),
    FoodNutrientProfile(
        canonical_id="oatmeal",
        display_name="Овсяная каша",
        category="grains",
        calories_kcal=88.0,
        protein_g=3.0,
        fat_g=1.7,
        carbs_g=15.0,
        reference_confidence=0.95,
        variance_note="Овсяная каша на воде.",
        aliases=("овсянка", "овсяная каша", "геркулес", "овсяные хлопья"),
    ),
    FoodNutrientProfile(
        canonical_id="pasta",
        display_name="Макароны отварные",
        category="grains",
        calories_kcal=131.0,
        protein_g=5.0,
        fat_g=1.1,
        carbs_g=25.0,
        reference_confidence=0.95,
        aliases=("макароны", "паста", "спагетти", "отварные макароны"),
    ),
    FoodNutrientProfile(
        canonical_id="syrniki",
        display_name="Сырники",
        category="prepared_dishes",
        calories_kcal=218.0,
        protein_g=16.5,
        fat_g=8.0,
        carbs_g=19.8,
        reference_confidence=0.85,
        variance_note="Сырники творожные запечённые или жареные.",
        aliases=("сырники", "сырник", "творожники"),
    ),

    # Soups & composite dishes
    FoodNutrientProfile(
        canonical_id="borscht",
        display_name="Борщ",
        category="prepared_dishes",
        calories_kcal=58.0,
        protein_g=3.5,
        fat_g=2.8,
        carbs_g=4.7,
        reference_confidence=0.85,
        variance_note="Борщ мясной с капустой и свеклой без сметаны.",
        aliases=("борщ", "украинский борщ", "красный борщ", "борщ со сметаной"),
    ),
    FoodNutrientProfile(
        canonical_id="chicken_soup",
        display_name="Куриный суп",
        category="prepared_dishes",
        calories_kcal=45.0,
        protein_g=3.2,
        fat_g=2.0,
        carbs_g=3.5,
        reference_confidence=0.85,
        aliases=("куриный суп", "куриный бульон", "суп куриный", "суп с лапшой"),
    ),
    FoodNutrientProfile(
        canonical_id="lentil_soup",
        display_name="Чечевичный суп с овощами",
        category="prepared_dishes",
        calories_kcal=72.0,
        protein_g=3.8,
        fat_g=1.9,
        carbs_g=10.2,
        reference_confidence=0.90,
        aliases=("чечевичный суп с овощами", "чечевичный суп", "суп из чечевицы"),
    ),
    FoodNutrientProfile(
        canonical_id="plov",
        display_name="Плов",
        category="prepared_dishes",
        calories_kcal=215.0,
        protein_g=7.5,
        fat_g=9.8,
        carbs_g=24.0,
        reference_confidence=0.85,
        variance_note="Плов традиционный с рисом, мясом и морковью.",
        aliases=("плов", "узбекский плов"),
    ),

    # Sauces & condiments
    FoodNutrientProfile(
        canonical_id="mayonnaise",
        display_name="Майонез",
        category="sauces",
        calories_kcal=680.0,
        protein_g=1.0,
        fat_g=75.0,
        carbs_g=1.0,
        reference_confidence=0.95,
        aliases=("майонез", "провансаль"),
    ),
    FoodNutrientProfile(
        canonical_id="mustard",
        display_name="Горчица",
        category="sauces",
        calories_kcal=66.0,
        protein_g=4.4,
        fat_g=4.0,
        carbs_g=5.8,
        reference_confidence=0.95,
        aliases=("горчица", "жёлтый соус", "желтый соус", "горчичный соус"),
    ),
    FoodNutrientProfile(
        canonical_id="vegetable_oil",
        display_name="Растительное масло",
        category="sauces",
        calories_kcal=884.0,
        protein_g=0.0,
        fat_g=100.0,
        carbs_g=0.0,
        reference_confidence=0.95,
        aliases=("растительное масло", "оливковое масло", "подсолнечное масло", "масло"),
    ),
    FoodNutrientProfile(
        canonical_id="soy_sauce",
        display_name="Соевый соус",
        category="sauces",
        calories_kcal=53.0,
        protein_g=8.1,
        fat_g=0.6,
        carbs_g=4.9,
        reference_confidence=0.95,
        aliases=("соевый соус",),
    ),
)


# -----------------------------------------------------------------------------
# Normalization & Indexing
# -----------------------------------------------------------------------------

def normalize_food_lookup_key(value: str) -> str:
    """
    Standard deterministic normalization for Russian food strings:
    - Lowercase NFKD unicode fold
    - Replace 'ё' with 'е'
    - Strip punctuation and extra whitespace
    """
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value).casefold())
    ascii_folded = "".join(char for char in normalized if not unicodedata.combining(char))
    ascii_folded = ascii_folded.replace("ё", "е")
    cleaned = re.sub(r"[^\w\s-]+", " ", ascii_folded)
    return " ".join(cleaned.split())


_CULINARY_MODIFIER_PREFIXES = (
    "свежий ", "свежая ", "свежее ", "свежие ",
    "отварной ", "отварная ", "отварное ", "отварные ",
    "вареный ", "вареная ", "вареное ", "вареные ",
    "жареный ", "жареная ", "жареное ", "жареные ",
    "запеченный ", "запеченная ", "запеченное ", "запеченные ",
    "тушеный ", "тушеная ", "тушеное ", "тушеные ",
    "приготовленный ", "приготовленная ",
    "домашний ", "домашняя ", "домашнее ", "домашние ",
    "нарезанный ", "нарезанная ", "порезанный ",
)

_CULINARY_MODIFIER_SUFFIXES = (
    " на пару", " по-домашнему", " соломкой", " кусочками", " ломтиками",
    " свежий", " свежая", " свежее", " свежие",
    " вареный", " вареная", " вареное", " отварной", " отварная",
    " жареный", " жареная", " запеченный", " запеченная",
)


def _strip_culinary_modifiers(text: str) -> str:
    cleaned = text
    for prefix in _CULINARY_MODIFIER_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break
    for suffix in _CULINARY_MODIFIER_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[:-len(suffix)].strip()
            break
    return cleaned


def _build_alias_index() -> dict[str, FoodNutrientProfile]:
    index: dict[str, FoodNutrientProfile] = {}
    for profile in _FOOD_CATALOG:
        canon_key = normalize_food_lookup_key(profile.display_name)
        index[canon_key] = profile
        index[profile.canonical_id] = profile
        for alias in profile.aliases:
            alias_key = normalize_food_lookup_key(alias)
            index[alias_key] = profile
    return index


_ALIAS_INDEX: dict[str, FoodNutrientProfile] = _build_alias_index()


def resolve_food_composition(query: str) -> FoodLookupResult:
    """
    Extensible, deterministic food composition lookup layer.
    Separates visual recognition from nutritional reference confidence.
    """
    raw_normalized = normalize_food_lookup_key(query)
    if not raw_normalized:
        return FoodLookupResult(matched=False, match_type="empty")

    # 1. Exact match on normalized full alias
    if raw_normalized in _ALIAS_INDEX:
        profile = _ALIAS_INDEX[raw_normalized]
        return FoodLookupResult(
            matched=True,
            profile=profile,
            confidence=profile.reference_confidence,
            match_type="exact_alias",
        )

    # 2. Match after stripping cooking modifiers (e.g. "свежая руккола" -> "руккола")
    stripped = _strip_culinary_modifiers(raw_normalized)
    if stripped != raw_normalized and stripped in _ALIAS_INDEX:
        profile = _ALIAS_INDEX[stripped]
        return FoodLookupResult(
            matched=True,
            profile=profile,
            confidence=profile.reference_confidence * 0.98,
            match_type="modifier_stripped",
        )

    # 3. Lemmatization / simple inflection fallback (Russian plurals -> singular)
    # E.g. яблоки -> яблоко, огурцы -> огурец, помидоры -> помидор
    singular_cand = raw_normalized
    if singular_cand.endswith("ки"):
        singular_cand = singular_cand[:-2] + "ко"
    elif singular_cand.endswith("цы"):
        singular_cand = singular_cand[:-2] + "ец"
    elif singular_cand.endswith("ры"):
        singular_cand = singular_cand[:-2] + "р"
    elif singular_cand.endswith("ты"):
        singular_cand = singular_cand[:-2] + "т"

    if singular_cand in _ALIAS_INDEX:
        profile = _ALIAS_INDEX[singular_cand]
        return FoodLookupResult(
            matched=True,
            profile=profile,
            confidence=profile.reference_confidence * 0.95,
            match_type="inflection",
        )

    # 4. Token subset matching (e.g. "куриное филе на пару" -> "куриное филе")
    tokens = raw_normalized.split()
    if len(tokens) > 1:
        for length in range(len(tokens) - 1, 0, -1):
            sub_phrase = " ".join(tokens[:length])
            if sub_phrase in _ALIAS_INDEX:
                profile = _ALIAS_INDEX[sub_phrase]
                return FoodLookupResult(
                    matched=True,
                    profile=profile,
                    confidence=profile.reference_confidence * 0.90,
                    match_type="token_prefix",
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
