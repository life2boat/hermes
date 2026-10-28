"""
gateway.nutrition.household_measures — Versioned offline dictionary of household food measures.

Supports calibrated conversion of Russian household measures:
- Ложки: столовая ложка (ст. л.), чайная ложка (ч. л.)
- Ёмкости: тарелка (глубокая тарелка, миска), стакан, чашка (кружка)
- Штучные/порционные меры: горсть, кусок (кусочек), ломтик (слайс)

Invariants:
- 0 < grams_min <= grams_center <= grams_max
- Arithmetic center: center = round((grams_min + grams_max) / 2.0, 1)
- Food-specific density/conversions (e.g. spoon of oil != spoon of sour cream != spoon of sugar)
- Always marked as approximate (is_exact=False) with Russian disclaimer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

HOUSEHOLD_MEASURES_VERSION = "2026.10.1"
HOUSEHOLD_DISCLAIMER = "*Примерные порции дают ориентировочные значения КБЖУ"


@dataclass(frozen=True, slots=True)
class HouseholdMeasureEntry:
    measure_type: str
    category: str
    grams_min: float
    grams_center: float
    grams_max: float
    typical_unit: str

    def __post_init__(self) -> None:
        if not (0.0 < self.grams_min <= self.grams_center <= self.grams_max):
            raise ValueError(
                f"Measure entry must satisfy 0 < min <= center <= max, "
                f"got ({self.grams_min}, {self.grams_center}, {self.grams_max})"
            )


@dataclass(frozen=True, slots=True)
class HouseholdMeasureResolution:
    measure_type: str
    food_category: str
    matched_food_name: str
    quantity: float
    grams_min: float
    grams_center: float
    grams_max: float
    typical_unit: str
    uncertainty_text: str
    is_exact: bool = False
    disclaimer: str = HOUSEHOLD_DISCLAIMER


# -----------------------------------------------------------------------------
# Calibrated Table of Household Measures
# All center values strictly equal round((min + max) / 2.0, 1)
# -----------------------------------------------------------------------------

_CALIBRATED_MEASURES: dict[tuple[str, str], HouseholdMeasureEntry] = {
    # Столовая ложка (tablespoon) ~ 15-30g depending on substance density
    ("tablespoon", "oil"): HouseholdMeasureEntry("tablespoon", "oil", 12.0, 15.0, 18.0, "столовая ложка"),
    ("tablespoon", "sour_cream"): HouseholdMeasureEntry("tablespoon", "sour_cream", 20.0, 25.0, 30.0, "столовая ложка"),
    ("tablespoon", "sauce"): HouseholdMeasureEntry("tablespoon", "sauce", 18.0, 23.0, 28.0, "столовая ложка"),
    ("tablespoon", "honey"): HouseholdMeasureEntry("tablespoon", "honey", 25.0, 30.0, 35.0, "столовая ложка"),
    ("tablespoon", "sugar"): HouseholdMeasureEntry("tablespoon", "sugar", 15.0, 20.0, 25.0, "столовая ложка"),
    ("tablespoon", "flour"): HouseholdMeasureEntry("tablespoon", "flour", 12.0, 15.0, 18.0, "столовая ложка"),
    ("tablespoon", "cereal"): HouseholdMeasureEntry("tablespoon", "cereal", 12.0, 15.0, 18.0, "столовая ложка"),
    ("tablespoon", "cottage_cheese"): HouseholdMeasureEntry("tablespoon", "cottage_cheese", 20.0, 25.0, 30.0, "столовая ложка"),
    ("tablespoon", "default"): HouseholdMeasureEntry("tablespoon", "default", 15.0, 20.0, 25.0, "столовая ложка"),

    # Чайная ложка (teaspoon) ~ 5-10g
    ("teaspoon", "oil"): HouseholdMeasureEntry("teaspoon", "oil", 4.0, 5.0, 6.0, "чайная ложка"),
    ("teaspoon", "butter"): HouseholdMeasureEntry("teaspoon", "butter", 4.0, 5.0, 6.0, "чайная ложка"),
    ("teaspoon", "sugar"): HouseholdMeasureEntry("teaspoon", "sugar", 6.0, 8.0, 10.0, "чайная ложка"),
    ("teaspoon", "honey"): HouseholdMeasureEntry("teaspoon", "honey", 8.0, 10.0, 12.0, "чайная ложка"),
    ("teaspoon", "salt"): HouseholdMeasureEntry("teaspoon", "salt", 6.0, 7.0, 8.0, "чайная ложка"),
    ("teaspoon", "sauce"): HouseholdMeasureEntry("teaspoon", "sauce", 6.0, 8.0, 10.0, "чайная ложка"),
    ("teaspoon", "default"): HouseholdMeasureEntry("teaspoon", "default", 4.0, 5.0, 6.0, "чайная ложка"),

    # Горсть (handful)
    ("handful", "nuts"): HouseholdMeasureEntry("handful", "nuts", 25.0, 30.0, 35.0, "горсть"),
    ("handful", "almonds"): HouseholdMeasureEntry("handful", "almonds", 25.0, 30.0, 35.0, "горсть"),
    ("handful", "seeds"): HouseholdMeasureEntry("handful", "seeds", 20.0, 25.0, 30.0, "горсть"),
    ("handful", "berries"): HouseholdMeasureEntry("handful", "berries", 40.0, 50.0, 60.0, "горсть"),
    ("handful", "dried_fruits"): HouseholdMeasureEntry("handful", "dried_fruits", 25.0, 35.0, 45.0, "горсть"),
    ("handful", "default"): HouseholdMeasureEntry("handful", "default", 25.0, 30.0, 35.0, "горсть"),

    # Тарелка (plate / soup bowl / deep plate)
    ("plate", "soup"): HouseholdMeasureEntry("plate", "soup", 250.0, 300.0, 350.0, "тарелка"),
    ("plate", "borscht"): HouseholdMeasureEntry("plate", "borscht", 250.0, 300.0, 350.0, "тарелка"),
    ("plate", "porridge"): HouseholdMeasureEntry("plate", "porridge", 160.0, 200.0, 240.0, "тарелка"),
    ("plate", "pasta"): HouseholdMeasureEntry("plate", "pasta", 140.0, 180.0, 220.0, "тарелка"),
    ("plate", "salad"): HouseholdMeasureEntry("plate", "salad", 130.0, 180.0, 230.0, "тарелка"),
    ("plate", "default"): HouseholdMeasureEntry("plate", "default", 180.0, 250.0, 320.0, "тарелка"),

    # Стакан (glass ~200-250 ml)
    ("glass", "liquid"): HouseholdMeasureEntry("glass", "liquid", 180.0, 200.0, 220.0, "стакан"),
    ("glass", "dairy"): HouseholdMeasureEntry("glass", "dairy", 180.0, 200.0, 220.0, "стакан"),
    ("glass", "berries"): HouseholdMeasureEntry("glass", "berries", 130.0, 150.0, 170.0, "стакан"),
    ("glass", "cereal"): HouseholdMeasureEntry("glass", "cereal", 140.0, 160.0, 180.0, "стакан"),
    ("glass", "flour"): HouseholdMeasureEntry("glass", "flour", 130.0, 150.0, 170.0, "стакан"),
    ("glass", "default"): HouseholdMeasureEntry("glass", "default", 180.0, 200.0, 220.0, "стакан"),

    # Чашка (cup ~250 ml)
    ("cup", "tea"): HouseholdMeasureEntry("cup", "tea", 200.0, 250.0, 300.0, "чашка"),
    ("cup", "coffee"): HouseholdMeasureEntry("cup", "coffee", 150.0, 200.0, 250.0, "чашка"),
    ("cup", "broth"): HouseholdMeasureEntry("cup", "broth", 200.0, 250.0, 300.0, "чашка"),
    ("cup", "default"): HouseholdMeasureEntry("cup", "default", 200.0, 250.0, 300.0, "чашка"),

    # Кусок (piece / slice)
    ("piece", "bread"): HouseholdMeasureEntry("piece", "bread", 25.0, 35.0, 45.0, "кусок"),
    ("piece", "pie"): HouseholdMeasureEntry("piece", "pie", 90.0, 130.0, 170.0, "кусок"),
    ("piece", "cake"): HouseholdMeasureEntry("piece", "cake", 80.0, 120.0, 160.0, "кусок"),
    ("piece", "meat"): HouseholdMeasureEntry("piece", "meat", 100.0, 150.0, 200.0, "кусок"),
    ("piece", "pizza"): HouseholdMeasureEntry("piece", "pizza", 90.0, 125.0, 160.0, "кусок"),
    ("piece", "default"): HouseholdMeasureEntry("piece", "default", 30.0, 50.0, 70.0, "кусок"),

    # Ломтик (slice / thin cut)
    ("slice", "cheese"): HouseholdMeasureEntry("slice", "cheese", 20.0, 30.0, 40.0, "ломтик"),
    ("slice", "sausage"): HouseholdMeasureEntry("slice", "sausage", 15.0, 25.0, 35.0, "ломтик"),
    ("slice", "bread"): HouseholdMeasureEntry("slice", "bread", 20.0, 30.0, 40.0, "ломтик"),
    ("slice", "lemon"): HouseholdMeasureEntry("slice", "lemon", 10.0, 15.0, 20.0, "ломтик"),
    ("slice", "default"): HouseholdMeasureEntry("slice", "default", 15.0, 25.0, 35.0, "ломтик"),
}

# Measure keyword detection patterns
_MEASURE_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tablespoon", re.compile(r"\b(?:ст(?:оловая|\.?\s*л\.?|оловые|оловых|оловую)?\s*(?:ложк[аиеу]|ложек)?|ст\.?\s*л\.?)\b", re.IGNORECASE)),
    ("teaspoon", re.compile(r"\b(?:ч(?:айная|\.?\s*л\.?|айные|айных|айную)?\s*(?:ложк[аиеу]|ложек)?|ч\.?\s*л\.?)\b", re.IGNORECASE)),
    ("handful", re.compile(r"\b(?:горст[ьяейку]|горсточк[аиу])\b", re.IGNORECASE)),
    ("plate", re.compile(r"\b(?:тарелк[аиуе]|глубокая\s+тарелка|миск[аиуе]|пиал[аыу])\b", re.IGNORECASE)),
    ("glass", re.compile(r"\b(?:стакан[аове]?)\b", re.IGNORECASE)),
    ("cup", re.compile(r"\b(?:чашк[аиуе]|кружк[аиуе])\b", re.IGNORECASE)),
    ("slice", re.compile(r"\b(?:ломтик[аове]?|слайс[аове]?)\b", re.IGNORECASE)),
    ("piece", re.compile(r"\b(?:кусо[кч][а-я]*)\b", re.IGNORECASE)),
)

# Food name classification rules for household measures
_FOOD_CATEGORY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Oils & fats
    (re.compile(r"(оливков.*масл|растительн.*масл|подсолнечн.*масл|масло\s+оливковое|масло\s+растительное|масло|маслице)"), "oil"),
    (re.compile(r"(сливочн.*масл|масло\s+сливочное)"), "butter"),
    # Soups & liquid dishes
    (re.compile(r"(борщ|борщик|борща)"), "borscht"),
    (re.compile(r"(суп|щи|бульон|солянк|рассольник|уха|окрошк|харчо)"), "soup"),
    # Dairy
    (re.compile(r"(сметан|соус|майонез|кетчуп|песто|горчиц)"), "sour_cream"),
    (re.compile(r"(творог|маскарпоне|рикотта)"), "cottage_cheese"),
    (re.compile(r"(сыр|брынза|сулугуни|пармезан|моцарелла)"), "cheese"),
    (re.compile(r"(молок|кефир|ряженк|йогурт)"), "dairy"),
    # Sweet / bulk
    (re.compile(r"(мед|мёд|варень|джем|сироп|сгущенк)"), "honey"),
    (re.compile(r"(сахар|сахзам|пудр)"), "sugar"),
    (re.compile(r"(соль)"), "salt"),
    (re.compile(r"(мука|крахмал)"), "flour"),
    # Nuts & seeds
    (re.compile(r"(миндал)"), "almonds"),
    (re.compile(r"(орех|фундук|грецк|кешью|арахис|фисташк)"), "nuts"),
    (re.compile(r"(семечк|семен|кунжут|семена)"), "seeds"),
    # Berries & fruits
    (re.compile(r"(ягод|клубник|малин|черник|голубик|смородин|вишн|черешн)"), "berries"),
    (re.compile(r"(сухофрукт|изюм|кураг|чернослив|финик)"), "dried_fruits"),
    (re.compile(r"(лимон|лайм)"), "lemon"),
    # Bread & bakery
    (re.compile(r"(хлеб|батон|булк|тост|багет|чиабатт)"), "bread"),
    (re.compile(r"(пирог|шарлотк|штрудел|пирожок)"), "pie"),
    (re.compile(r"(торт|пирожн|чизкейк)"), "cake"),
    (re.compile(r"(пицц)"), "pizza"),
    # Meat & cold cuts
    (re.compile(r"(колбас|ветчин|бекон|буженин|карбонад|сосиск)"), "sausage"),
    (re.compile(r"(стейк|мясо|говядин|свинин|индейк|куриц|филе|котлет|рыб)"), "meat"),
    # Porridge & grains
    (re.compile(r"(каш|гречк|рис|овсянк|геркулес|булгур|хлопь)"), "porridge"),
    (re.compile(r"(макарон|паст|спагетти|лапш)"), "pasta"),
    (re.compile(r"(салат|оливье|цезар|винегрет)"), "salad"),
    # Beverages
    (re.compile(r"(чай)"), "tea"),
    (re.compile(r"(кофе|капучино|латте|американо)"), "coffee"),
    (re.compile(r"(вод|сок|компот|морс|лимонад|квас)"), "liquid"),
)


_GENITIVE_TO_NOMINATIVE: dict[str, str] = {
    "борща": "борщ",
    "миндаля": "миндаль",
    "оливкового масла": "оливковое масло",
    "сливочного масла": "сливочное масло",
    "растительного масла": "растительное масло",
    "подсолнечного масла": "подсолнечное масло",
    "масла": "масло",
    "сыра": "сыр",
    "молока": "молоко",
    "сметаны": "сметана",
    "гречки": "гречка",
    "овсянки": "овсянка",
    "риса": "рис",
    "хлеба": "хлеб",
    "курицы": "курица",
    "индейки": "индейка",
    "говядины": "говядина",
    "свинины": "свинина",
    "рыбы": "рыба",
    "ягод": "ягоды",
    "клубники": "клубника",
    "малины": "малина",
    "черники": "черника",
    "сахара": "сахар",
    "мёда": "мёд",
    "меда": "мёд",
    "чая": "чай",
    "воды": "вода",
    "супа": "суп",
    "бульона": "бульон",
    "творога": "творог",
}


def normalize_food_inflection(food: str) -> str:
    cleaned = food.strip().lower()
    if cleaned in _GENITIVE_TO_NOMINATIVE:
        return _GENITIVE_TO_NOMINATIVE[cleaned]
    # Adjective accusative -> nominative: e.g. "запечённую" -> "запечённая"
    if cleaned.endswith("ую"):
        return cleaned[:-2] + "ая"
    if cleaned.endswith("ого"):
        return cleaned[:-3] + "ый"
    # Noun accusative -> nominative: e.g. "говядину" -> "говядина", "свинину" -> "свинина", "рыбу" -> "рыба"
    if cleaned.endswith("у") and len(cleaned) >= 4 and not cleaned.endswith(("муку", "пасту")):
        return cleaned[:-1] + "а"
    if cleaned.endswith(("муку", "пасту")):
        return cleaned[:-1] + "а"
    # Noun genitive feminine -> nominative: e.g. "курицы" -> "курица", "свинины" -> "свинина"
    if (cleaned.endswith("ы") or cleaned.endswith("и")) and len(cleaned) >= 4:
        # e.g. "курицы" -> "курица", "говядины" -> "говядина", "рыбы" -> "рыба"
        return cleaned[:-1] + "а"
    # Noun genitive masculine -> nominative: e.g. "риса" -> "рис", "сыра" -> "сыр", "хлеба" -> "хлеб"
    if cleaned.endswith("а") and len(cleaned) >= 4 and cleaned in ("риса", "сыра", "хлеба", "супа", "борща", "чая", "лука", "чеснока", "перца", "мака"):
        return cleaned[:-1]
    return cleaned


def _categorize_food_for_measure(food_name: str) -> str:
    low = food_name.lower().strip()
    for pattern, cat in _FOOD_CATEGORY_RULES:
        if pattern.search(low):
            return cat
    return "default"


def detect_household_measure_token(text: str) -> tuple[str, str, float] | None:
    """
    Detects if the given text segment contains a recognized household measure keyword.
    Returns (measure_type, matched_token, quantity) or None.
    Example:
      "1 столовая ложка" -> ("tablespoon", "столовая ложка", 1.0)
      "горсть" -> ("handful", "горсть", 1.0)
      "2 стакана" -> ("glass", "стакана", 2.0)
    """
    cleaned = text.strip()

    # Look for leading quantity: e.g. "2 столовые ложки", "1.5 стакана", "1/2 чашки"
    qty = 1.0
    qty_match = re.match(r"^(\d+(?:[.,/]\d+)?)\s*", cleaned)
    if qty_match:
        raw_qty = qty_match.group(1).replace(",", ".")
        if "/" in raw_qty:
            parts = raw_qty.split("/")
            try:
                qty = float(parts[0]) / float(parts[1]) if float(parts[1]) != 0 else 1.0
            except ValueError:
                qty = 1.0
        else:
            try:
                qty = float(raw_qty)
            except ValueError:
                qty = 1.0
        # If unreasonable count (e.g. 160 ложек), reject as uncalibrated
        if qty <= 0.0 or qty > 20.0:
            return None

    # Match measure keyword
    for m_type, pat in _MEASURE_TOKEN_PATTERNS:
        m = pat.search(cleaned)
        if m:
            matched_str = m.group(0)
            return m_type, matched_str, qty

    return None


def resolve_household_measure(
    measure_type_or_token: str,
    food_name: str,
    *,
    quantity: float = 1.0,
) -> HouseholdMeasureResolution | None:
    """
    Resolves calibrated grams for a household measure and food item.
    Returns HouseholdMeasureResolution or None if unsupported or uncalibrated.
    """
    if quantity <= 0.0 or quantity > 20.0:
        return None

    m_type = measure_type_or_token
    # If a full token was passed, map it to measure_type
    if m_type not in ("tablespoon", "teaspoon", "handful", "plate", "glass", "cup", "piece", "slice"):
        detected = detect_household_measure_token(m_type)
        if detected:
            m_type = detected[0]
            if quantity == 1.0:
                quantity = detected[2]
        else:
            return None

    food_cat = _categorize_food_for_measure(food_name)

    # Lookup in calibrated table
    entry = _CALIBRATED_MEASURES.get((m_type, food_cat))
    if entry is None:
        entry = _CALIBRATED_MEASURES.get((m_type, "default"))
    if entry is None:
        return None

    scaled_min = round(entry.grams_min * quantity, 1)
    scaled_center = round(entry.grams_center * quantity, 1)
    scaled_max = round(entry.grams_max * quantity, 1)

    # Format human-readable uncertainty description
    qty_str = f"{quantity:g} " if quantity != 1.0 else ""
    unit_str = entry.typical_unit
    if quantity > 1.0:
        if m_type == "tablespoon": unit_str = "ст. ложки"
        elif m_type == "teaspoon": unit_str = "ч. ложки"
        elif m_type == "handful": unit_str = "горсти"
        elif m_type == "plate": unit_str = "тарелки"
        elif m_type == "glass": unit_str = "стакана"
        elif m_type == "cup": unit_str = "чашки"
        elif m_type == "piece": unit_str = "куска"
        elif m_type == "slice": unit_str = "ломтика"

    uncertainty_text = f"~{scaled_center} г ({qty_str}{unit_str})"

    return HouseholdMeasureResolution(
        measure_type=m_type,
        food_category=food_cat,
        matched_food_name=food_name.strip(),
        quantity=quantity,
        grams_min=scaled_min,
        grams_center=scaled_center,
        grams_max=scaled_max,
        typical_unit=entry.typical_unit,
        uncertainty_text=uncertainty_text,
        is_exact=False,
    )


def extract_household_measure_from_phrase(phrase: str) -> tuple[HouseholdMeasureResolution, str] | None:
    """
    Extracts a household measure and remaining food name from natural Russian text.
    Examples:
      "тарелка борща" -> (Resolution(plate, borscht, 300g), "борщ")
      "горсть миндаля" -> (Resolution(handful, almonds, 30g), "миндаль")
      "столовая ложка оливкового масла" -> (Resolution(tablespoon, oil, 15g), "оливковое масло")
      "2 ломтика сыра" -> (Resolution(slice, cheese, 60g), "сыр")
      "стакан молока" -> (Resolution(glass, dairy, 200g), "молоко")
    """
    cleaned = phrase.strip()
    # Strip leading bullets or list numbering (e.g. "1. " or "• ")
    cleaned = re.sub(r"^[•\-\*–—\s]+|^\d+[\.\)]\s*", "", cleaned).strip()

    # Pattern: [qty] [measure_name] (из|от|с)? [food_name]
    # e.g. "столовая ложка оливкового масла"
    # or "оливковое масло 1 столовая ложка"
    detected = detect_household_measure_token(cleaned)
    if not detected:
        return None

    m_type, matched_token, qty = detected

    # Remove matched measure token and any leading quantity or preposition from food name
    # Case 1: Measure is at the beginning: e.g. "тарелка борща", "2 ст. л. масла"
    food_cand = re.sub(rf"^(?:\d+(?:[.,/]\d+)?\s*)?{re.escape(matched_token)}\s*(?:\b(?:из|от|с|со|без)\b\s*)?", "", cleaned, flags=re.IGNORECASE).strip()
    if not food_cand or len(food_cand) < 2:
        # Case 2: Measure is at the end: e.g. "борщ 1 тарелка", "масло 1 ст л"
        food_cand = re.sub(rf"\s*(?:\b(?:из|от|с|со|без)\b\s*)?(?:\d+(?:[.,/]\d+)?\s*)?{re.escape(matched_token)}\s*$", "", cleaned, flags=re.IGNORECASE).strip()

    # Clean prepositions and punctuation
    food_cand = re.sub(r"^[,:\-\s]+|[,:\-\s]+$", "", food_cand).strip()
    if not food_cand or len(food_cand) < 2:
        return None

    # Check if food_cand is just a number or nonsense
    if food_cand.isdigit() or len(food_cand) < 2:
        return None

    norm_food = normalize_food_inflection(food_cand)
    res = resolve_household_measure(m_type, norm_food, quantity=qty)
    if res is None:
        return None

    return res, norm_food
