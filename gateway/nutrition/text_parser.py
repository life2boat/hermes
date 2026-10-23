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
