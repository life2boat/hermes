"""
gateway.nutrition.text_parser — Text & caption food recipe parser.

Extracts structured dish titles, ingredients, quantities, fractions (e.g. 1/2 яйца),
and fat percentages from natural Russian user input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence


# Standard reference weights in grams for items counted in pieces (шт / штуки)
_PIECE_WEIGHT_REFERENCE: dict[str, float] = {
    "яйцо": 50.0,
    "яйца": 50.0,
    "яиц": 50.0,
    "яйцо куриное": 50.0,
    "яблоко": 150.0,
    "яблока": 150.0,
    "яблок": 150.0,
    "банан": 120.0,
    "банана": 120.0,
    "бананов": 120.0,
    "помидор": 100.0,
    "помидора": 100.0,
    "помидоров": 100.0,
    "томат": 100.0,
    "огурец": 100.0,
    "огурца": 100.0,
    "огурцов": 100.0,
    "апельсин": 150.0,
    "апельсина": 150.0,
    "груша": 150.0,
    "груши": 150.0,
    "вафля": 40.0,
    "вафли": 40.0,
    "кусок": 100.0,
    "ломтик": 30.0,
}

# Non-food prompt phrases / command triggers that should not be parsed as food
_NON_FOOD_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:посчитай|рассчитай|оцени|посчитать)\s+(?:кбжу|калории|калорийность|порцию)?$", re.IGNORECASE),
    re.compile(r"^(?:кбжу|калории|калорийность|бжу)$", re.IGNORECASE),
    re.compile(r"^(?:count|calculate|estimate)\s*(?:calories|macros|kbzhu)?$", re.IGNORECASE),
    re.compile(r"^(?:calories|calorie\s+count|macros)$", re.IGNORECASE),
    re.compile(r"^(?:что\s+это|что\s+в\s+тарелке|что\s+на\s+фото|определи\s+еду)$", re.IGNORECASE),
    re.compile(r"^(?:фото|photo|еда|блюдо|порция|завтрак|обед|ужин|перекус)$", re.IGNORECASE),
)

_META_INSTRUCTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:это\s+)?(?:блюдо|порция|еда|тарелка|всё|всего)\s+(?:весит|вес|масса|здесь|тут)?\s*\d+(?:[.,]\d+)?\s*(?:г|гр|грам|грамм|граммов|кг|г\.)\b", re.IGNORECASE),
    re.compile(r"^(?:весит|вес|масса|общий\s+вес|вес\s+блюда|вес\s+порции)\s*(?:составляет|:)?\s*\d+(?:[.,]\d+)?\s*(?:г|гр|грам|грамм|граммов|кг|г\.)\b", re.IGNORECASE),
    re.compile(r"^(?:без|не\s+добавлять|убрать|исключить)\s+[а-яa-z\s]+$", re.IGNORECASE),
)


@dataclass(slots=True)
class TextMealConstraint:
    total_weight_g: float | None = None
    excluded_ingredients: list[str] = field(default_factory=list)
    partial_item_weights: dict[str, float] = field(default_factory=dict)
    has_constraints: bool = False


@dataclass(slots=True)
class ParsedIngredient:
    raw_text: str
    name: str
    normalized_name: str
    grams: float
    quantity: float = 1.0
    unit: str = "грамм"
    fat_percent: float | None = None
    uncertainty: str = ""
    is_piece: bool = False


@dataclass(slots=True)
class ParsedFoodText:
    dish_title: str
    ingredients: list[ParsedIngredient] = field(default_factory=list)
    raw_text: str = ""
    is_structured: bool = False
    has_food_intent: bool = False
    total_grams: float = 0.0


def _parse_fraction(token: str) -> float | None:
    token = token.strip()
    if "/" in token:
        parts = token.split("/")
        if len(parts) == 2:
            try:
                num = float(parts[0])
                denom = float(parts[1])
                if denom != 0:
                    return num / denom
            except ValueError:
                return None
    try:
        return float(token.replace(",", "."))
    except ValueError:
        return None


def parse_food_text_description(text: str) -> ParsedFoodText | None:
    """
    Parses a Russian natural language meal description or recipe.
    Returns ParsedFoodText if text contains valid food descriptions, or None.
    """
    if not text or not text.strip():
        return None

    cleaned = text.strip()
    # Reject prompt commands or generic triggers
    for pat in _NON_FOOD_COMMAND_PATTERNS:
        if pat.match(cleaned):
            return None

    # Reject pure meta-instructions (e.g. "Это блюдо весит 350 г" or "Без сметаны")
    for pat in _META_INSTRUCTION_PATTERNS:
        if pat.match(cleaned):
            return None

    # Normalize common quotes, em-dashes
    cleaned = cleaned.replace("«", "").replace("»", "").replace('"', "").replace("—", "-")

    dish_title = ""
    recipe_body = cleaned

    # Check for prefix like "Это <название>, состав: ..." or "<название>: ..."
    title_match = re.match(
        r"^(?:это\s+)?([^,:\n]+?)(?:,\s*состав|\s*состав|\s*ингредиенты)?\s*[:]\s*(.+)$",
        cleaned,
        re.IGNORECASE | re.DOTALL,
    )
    if title_match:
        dish_title = title_match.group(1).strip()
        recipe_body = title_match.group(2).strip()
    else:
        # Check "состав:" anywhere
        comp_split = re.split(r"\bсостав\s*:\s*|\bингредиенты\s*:\s*", cleaned, flags=re.IGNORECASE)
        if len(comp_split) == 2:
            dish_title = comp_split[0].strip()
            # Clean "Это " from dish_title
            dish_title = re.sub(r"^это\s+", "", dish_title, flags=re.IGNORECASE).strip()
            recipe_body = comp_split[1].strip()

    # Split recipe body into items
    # Delimiters: newline, semicolon, comma (except comma inside numbers or "творог, 100г")
    # First protect decimal commas in numbers like "1,5" or "0,5"
    protected_body = re.sub(r"(\d+),(\d+)", r"\1.\2", recipe_body)
    raw_lines = re.split(r"[\n;\u2022\*\+]|(?<=\D),(?=\s*[а-яa-z0-9])", protected_body, flags=re.IGNORECASE)

    ingredients: list[ParsedIngredient] = []

    for raw_part in raw_lines:
        part = raw_part.strip()
        if not part:
            continue
        # Strip leading dashes / bullets
        part = re.sub(r"^[-–—\s*]+", "", part).strip()
        if not part:
            continue

        # Skip exclusions or meta-weight fragments
        if re.match(r"^(?:без|не\s+добавлять|убрать|исключить)\b", part, re.IGNORECASE):
            continue
        if re.match(r"^(?:это\s+)?(?:блюдо|порция|еда|тарелка|всё|всего)\s+(?:весит|вес|масса|здесь|тут)?\s*\d", part, re.IGNORECASE):
            continue

        # Check for fraction piece format, e.g. "1/2 яйца", "2 яйца", "1.5 яблока"
        piece_match = re.match(
            r"^(\d+(?:[./]\d+)?)\s*(?:шт(?:ук[аи]?)?\.?\s*)?([а-яa-z\s]+)$",
            part,
            re.IGNORECASE,
        )
        fraction_parsed = False
        if piece_match:
            qty_str = piece_match.group(1)
            item_name = piece_match.group(2).strip().lower()
            qty = _parse_fraction(qty_str)
            if qty is not None and any(pk in item_name for pk in _PIECE_WEIGHT_REFERENCE):
                matched_pk = next(pk for pk in _PIECE_WEIGHT_REFERENCE if pk in item_name)
                unit_weight = _PIECE_WEIGHT_REFERENCE[matched_pk]
                calc_grams = round(qty * unit_weight, 1)
                ingredients.append(
                    ParsedIngredient(
                        raw_text=part,
                        name=item_name.capitalize(),
                        normalized_name=matched_pk,
                        grams=calc_grams,
                        quantity=qty,
                        unit="шт",
                        uncertainty=f"{qty_str} шт (~{calc_grams} г)",
                        is_piece=True,
                    )
                )
                fraction_parsed = True

        if fraction_parsed:
            continue

        # Look for grams / kilograms / ml / liters
        # Examples: "армянский лаваш 38 грамм", "творог 2% 100 грамм", "мак 30 грам", "молоко 200мл"
        weight_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:[xх]\s*(\d+(?:\.\d+)?))?\s*(г|гр|грам|грамм|граммов|кг|килограмм|мл|миллилитров|л|литр|г\.)\b",
            part,
            re.IGNORECASE,
        )
        if weight_match:
            num1 = float(weight_match.group(1))
            num2 = float(weight_match.group(2)) if weight_match.group(2) else 1.0
            unit_raw = weight_match.group(3).lower()
            val = num1 * num2
            if unit_raw in {"кг", "килограмм", "л", "литр"}:
                grams = round(val * 1000.0, 1)
            else:
                grams = round(val, 1)

            # Name is the part without the weight match
            name_part = part[:weight_match.start()] + part[weight_match.end():]
            name_part = re.sub(r"[,:\-\s]+$", "", name_part).strip()
            name_part = re.sub(r"^[,:\-\s]+", "", name_part).strip()

            # Check fat percentage: e.g. "творог 2%"
            fat_match = re.search(r"(\d+(?:\.\d+)?)\s*%", name_part)
            fat_percent = float(fat_match.group(1)) if fat_match else None
            clean_name = re.sub(r"\d+(?:\.\d+)?\s*%", "", name_part).strip()
            clean_name = re.sub(r"\s+", " ", clean_name).strip()

            if clean_name:
                if re.match(r"^(?:это\s+)?(?:блюдо|порция|еда|тарелка|всё|всего)?\s*(?:весит|вес|масса|здесь|тут)?$", clean_name, re.IGNORECASE):
                    continue
                ingredients.append(
                    ParsedIngredient(
                        raw_text=part,
                        name=name_part.capitalize() if name_part else clean_name.capitalize(),
                        normalized_name=clean_name.lower(),
                        grams=grams,
                        quantity=1.0,
                        unit="грамм",
                        fat_percent=fat_percent,
                        uncertainty=f"{grams} г" + (f" ({fat_percent}%)" if fat_percent is not None else ""),
                    )
                )
                continue

        # If no explicit weight found, check if it's a known food ingredient or dish
        clean_text = part.strip()
        if len(clean_text) >= 2:
            if re.match(r"^(?:без|убрать|не\s+добавлять|исключить)\b", clean_text, re.IGNORECASE):
                continue
            if re.match(r"^(?:это\s+)?(?:блюдо|порция|еда|тарелка|всё|всего)?\s*(?:весит|вес|масса|здесь|тут)?$", clean_text, re.IGNORECASE):
                continue
            ingredients.append(
                ParsedIngredient(
                    raw_text=part,
                    name=clean_text.capitalize(),
                    normalized_name=clean_text.lower(),
                    grams=100.0,  # Default provisional portion
                    quantity=1.0,
                    unit="порция",
                    uncertainty="Вес не указан (~100 г)",
                )
            )

    if not ingredients and not dish_title:
        # Check single food item without recipe structure
        single_cand = cleaned.strip()
        if len(single_cand) >= 3 and not single_cand.startswith("/"):
            if re.match(r"^(?:без|убрать|не\s+добавлять|исключить)\b", single_cand, re.IGNORECASE):
                return None
            if re.match(r"^(?:это\s+)?(?:блюдо|порция|еда|тарелка|всё|всего)?\s*(?:весит|вес|масса|здесь|тут)?$", single_cand, re.IGNORECASE):
                return None
            return ParsedFoodText(
                dish_title=single_cand.capitalize(),
                ingredients=[
                    ParsedIngredient(
                        raw_text=single_cand,
                        name=single_cand.capitalize(),
                        normalized_name=single_cand.lower(),
                        grams=150.0,
                        quantity=1.0,
                        unit="порция",
                        uncertainty="Ориентировочная порция ~150 г",
                    )
                ],
                raw_text=cleaned,
                is_structured=False,
                has_food_intent=True,
                total_grams=150.0,
            )
        return None

    if not ingredients:
        return None

    total_grams = round(sum(it.grams for it in ingredients), 1)
    is_structured = any(it.grams != 100.0 or it.is_piece or it.fat_percent is not None for it in ingredients) or len(ingredients) > 1

    final_title = dish_title.capitalize() if dish_title else (ingredients[0].name if len(ingredients) == 1 else "Приём пищи")
    # Clean "Это " from final_title
    final_title = re.sub(r"^это\s+", "", final_title, flags=re.IGNORECASE).strip().capitalize()

    return ParsedFoodText(
        dish_title=final_title,
        ingredients=ingredients,
        raw_text=cleaned,
        is_structured=is_structured,
        has_food_intent=len(ingredients) > 0 or bool(dish_title),
        total_grams=total_grams,
    )


def parse_text_meal_constraints(text: str) -> TextMealConstraint | None:
    """
    Parses user constraints and annotations from caption or text turn.
    Extracts:
      - total_weight_g: e.g. "Это блюдо весит 350 г", "Вес 350г", "Общий вес 400 грамм"
      - excluded_ingredients: e.g. "Без сметаны", "убрать лук", "не добавлять майонез"
      - partial_item_weights: e.g. "Курицы здесь 120 г", "Здесь 120 г курицы"
    """
    if not text or not text.strip():
        return None

    cleaned = text.strip()
    total_weight: float | None = None
    excluded: list[str] = []
    partial_weights: dict[str, float] = {}

    # 1. Total dish weight constraint
    total_weight_patterns = [
        r"(?:это\s+)?(?:блюдо|порция|еда|тарелка|всё|всего)\s+(?:весит|вес|масса|здесь|тут)?\s*(\d+(?:[.,]\d+)?)\s*(?:г|гр|грам|грамм|граммов|кг|килограмм|г\.)\b",
        r"\b(?:общий\s+вес|вес\s+блюда|вес\s+порции|масса\s+блюда|масса\s+порции)\s*(?:составляет|:)?\s*(\d+(?:[.,]\d+)?)\s*(?:г|гр|грам|грамм|граммов|кг|килограмм|г\.)\b",
        r"\b(?:весит|вес|масса)\s+(\d+(?:[.,]\d+)?)\s*(?:г|гр|грам|грамм|граммов|кг|килограмм|г\.)\b",
        r"^(?:здесь|тут|всего)\s+(\d+(?:[.,]\d+)?)\s*(?:г|гр|грам|грамм|граммов|кг|килограмм|г\.)\b",
    ]
    for pat in total_weight_patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                unit_matched = m.group(0).lower()
                if "кг" in unit_matched or "килограмм" in unit_matched:
                    val *= 1000.0
                total_weight = round(val, 1)
                break
            except ValueError:
                pass

    # 2. Excluded ingredients ("без ...", "убрать ...", "не добавлять ...")
    for m in re.finditer(r"\b(?:без|не\s+добавлять|убрать|исключить)\s+([^,.;\n]+)", cleaned, re.IGNORECASE):
        phrase = m.group(1).strip()
        parts = re.split(r"\b(?:и|или)\b|,", phrase, flags=re.IGNORECASE)
        for p in parts:
            clean = p.strip().lower()
            clean = re.sub(r"\b(?:пожалуйста|совсем|вообще)\b", "", clean).strip()
            if clean and len(clean) >= 2:
                excluded.append(clean)

    # 3. Partial item weights with explicit context indicators
    # e.g. "курицы здесь 120 г", "курица здесь 120г", "здесь 120г курицы"
    for m in re.finditer(
        r"\b([а-яa-z]+(?:\s+[а-яa-z]+)?)\s+(?:здесь|тут)\s*(?:весит|вес)?\s*(\d+(?:[.,]\d+)?)\s*(?:г|гр|грам|грамм|г\.)\b",
        cleaned,
        re.IGNORECASE,
    ):
        food_name = m.group(1).strip().lower()
        if any(w in food_name for w in ["это блюдо", "блюдо", "порция", "еда", "тарелка", "всё", "всего", "общий вес"]):
            continue
        try:
            val = float(m.group(2).replace(",", "."))
            food_name = re.sub(r"\b(?:здесь|тут|примерно|около)\b", "", food_name).strip()
            if food_name and len(food_name) >= 3:
                partial_weights[food_name] = round(val, 1)
        except ValueError:
            pass

    for m in re.finditer(
        r"(?:здесь|тут)\s+(\d+(?:[.,]\d+)?)\s*(?:г|гр|грам|грамм|г\.)\s+([а-яa-z]+(?:\s+[а-яa-z]+)?)\b",
        cleaned,
        re.IGNORECASE,
    ):
        food_name = m.group(2).strip().lower()
        if any(w in food_name for w in ["это блюдо", "блюдо", "порция", "еда", "тарелка", "всё", "всего", "общий вес"]):
            continue
        try:
            val = float(m.group(1).replace(",", "."))
            food_name = re.sub(r"\b(?:здесь|тут|примерно|около)\b", "", food_name).strip()
            if food_name and len(food_name) >= 3:
                partial_weights[food_name] = round(val, 1)
        except ValueError:
            pass

    has_constraints = bool(total_weight or excluded or partial_weights)
    if not has_constraints:
        return None

    return TextMealConstraint(
        total_weight_g=total_weight,
        excluded_ingredients=excluded,
        partial_item_weights=partial_weights,
        has_constraints=has_constraints,
    )


def is_full_food_description(text_or_parsed: str | ParsedFoodText | None) -> bool:
    """
    Returns True if the text or parsed text contains a complete, self-contained food
    description with explicit mass/quantity for all items, allowing Vision to be bypassed.
    """
    if not text_or_parsed:
        return False

    parsed: ParsedFoodText | None
    if isinstance(text_or_parsed, str):
        parsed = parse_food_text_description(text_or_parsed)
        raw_text = text_or_parsed
    else:
        parsed = text_or_parsed
        raw_text = parsed.raw_text

    if not parsed or not parsed.has_food_intent or not parsed.ingredients:
        return False

    low_text = raw_text.lower()
    # Explicit pointer to image (e.g. "здесь 120г" or "на фото курица 120г")
    if re.search(r"\b(?:здесь|тут|на\s+фото|в\s+тарелке)\b", low_text):
        return False

    # Standalone exclusion without food ingredients
    if re.match(r"^(?:без|убрать|не\s+добавлять|исключить)\b", low_text):
        return False

    # Every ingredient must have an explicit weight or piece count
    for ing in parsed.ingredients:
        if ing.unit == "порция" or "Вес не указан" in ing.uncertainty:
            return False
        if not ing.grams or ing.grams <= 0:
            return False
        if len(ing.normalized_name.strip()) < 2:
            return False
        if any(meta in ing.normalized_name for meta in ["блюдо весит", "весит", "общий вес"]):
            return False

    return True
