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
    "хлебцы": 30.0,
    "хлебец": 15.0,
    "сырник": 50.0,
    "сырники": 120.0,
    "блин": 40.0,
    "блины": 120.0,
    "котлета": 100.0,
    "котлеты": 150.0,
}

_RUSSIAN_WORD_NUMBERS: dict[str, float] = {
    "один": 1.0,
    "одна": 1.0,
    "одно": 1.0,
    "одну": 1.0,
    "два": 2.0,
    "две": 2.0,
    "двух": 2.0,
    "три": 3.0,
    "трёх": 3.0,
    "трех": 3.0,
    "четыре": 4.0,
    "четырёх": 4.0,
    "четырех": 4.0,
    "пять": 5.0,
    "пяти": 5.0,
    "шесть": 6.0,
    "шести": 6.0,
    "семь": 7.0,
    "семи": 7.0,
    "восемь": 8.0,
    "восьми": 8.0,
    "девять": 9.0,
    "девяти": 9.0,
    "десять": 10.0,
    "десяти": 10.0,
    "пол": 0.5,
    "половина": 0.5,
    "половинку": 0.5,
    "половинка": 0.5,
    "половину": 0.5,
    "полтора": 1.5,
    "полторы": 1.5,
    "полутора": 1.5,
}

_CONVERSATIONAL_PREAMBLE_RE: re.Pattern[str] = re.compile(
    r"^(?:(?:я\s+)?(?:сегодня\s+)?(?:на\s+)?(завтрак|обед|ужин|перекус|полдник)\s+(?:съел[а-яё]*|поел[а-яё]*|скушал[а-яё]*|выпил[а-яё]*|употребил[а-яё]*|завтракал[а-яё]*|обедал[а-яё]*|ужинал[а-яё]*)\s+|(?:я\s+)?(?:сегодня\s+)?(?:съел[а-яё]*|поел[а-яё]*|скушал[а-яё]*|выпил[а-яё]*|употребил[а-яё]*)\s+|(?:сегодня\s+)?(?:на\s+(завтрак|обед|ужин|перекус|полдник))\s*[:,-]?\s*)",
    re.IGNORECASE,
)

_PREPARATION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:запеч[её]нн?[а-яё]*|в\s+духовке)\b", re.IGNORECASE), "запечённый"),
    (re.compile(r"\b(?:отварн[а-яё]*|вар[её]нн?[а-яё]*|сваренн?[а-яё]*)\b", re.IGNORECASE), "отварной"),
    (re.compile(r"\b(?:жаренн?[а-яё]*|поджаренн?[а-яё]*)\b", re.IGNORECASE), "жареный"),
    (re.compile(r"\b(?:туш[её]нн?[а-яё]*)\b", re.IGNORECASE), "тушёный"),
    (re.compile(r"\b(?:на\s+пару|паров[а-яё]*)\b", re.IGNORECASE), "на пару"),
    (re.compile(r"\b(?:на\s+гриле|гриль)\b", re.IGNORECASE), "на гриле"),
    (re.compile(r"\b(?:свеж[а-яё]*|сыр[а-яё]*)\b", re.IGNORECASE), "сырой"),
    (re.compile(r"\b(?:слабосол[её]нн?[а-яё]*|солен[а-яё]*|сол[её]нн?[а-яё]*)\b", re.IGNORECASE), "слабосолёный"),
)


def _extract_preparation(text: str) -> str:
    for pat, prep in _PREPARATION_RULES:
        if pat.search(text):
            return prep
    return ""

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
    re.compile(r"^(?:без|не\s+добавлять|убрать|исключить)\s+[а-яёa-z\s]+$", re.IGNORECASE),
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
    is_exact: bool = True
    grams_min: float | None = None
    grams_max: float | None = None
    preparation: str = ""
    household_measure: str | None = None


@dataclass(slots=True)
class ParsedFoodText:
    dish_title: str
    ingredients: list[ParsedIngredient] = field(default_factory=list)
    raw_text: str = ""
    is_structured: bool = False
    has_food_intent: bool = False
    total_grams: float = 0.0
    is_all_exact: bool = False


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

    # Check for conversational preamble, e.g. "На завтрак съел 2 варёных яйца..." or "Я съел 1 яблоко..."
    preamble_match = _CONVERSATIONAL_PREAMBLE_RE.match(recipe_body)
    if preamble_match:
        meal_hint = preamble_match.group(1) or preamble_match.group(2)
        if meal_hint and not dish_title:
            dish_title = meal_hint.capitalize()
        recipe_body = recipe_body[preamble_match.end():].strip()

    # Normalize natural language connectors into standard delimiters
    # 1. Connective verbs with optional "и" (e.g. "и выпил", "и съел", "поел", "запил")
    recipe_body = re.sub(
        r"\s+(?:и\s+)?(?:выпил[а-яё]*|съел[а-яё]*|поел[а-яё]*|скушал[а-яё]*|добавил[а-яё]*|запил[а-яё]*|употребил[а-яё]*)\s+",
        ", ",
        recipe_body,
        flags=re.IGNORECASE,
    )
    # 2. Coordinating conjunctions "а также", "плюс"
    recipe_body = re.sub(r"\s+(?:а\s+также|плюс)\s+", ", ", recipe_body, flags=re.IGNORECASE)
    # 3. Coordinating conjunction "и" before numbers/quantities, measures, or number words
    _num_words_alt = "|".join(_RUSSIAN_WORD_NUMBERS.keys())
    recipe_body = re.sub(
        rf"\s+и\s+(?=(?:\d+(?:[.,]\d+)?\s*(?:[а-яёa-z]+)|{_num_words_alt}\b|стакан|чашк|тарелк|порци|ложд?к))",
        ", ",
        recipe_body,
        flags=re.IGNORECASE,
    )
    # 4. Coordinating conjunction "и" after a completed unit/quantity clause (e.g. "весом 180 г и натуральный йогурт...")
    recipe_body = re.sub(
        r"(\d+(?:\.\d+)?\s*(?:г|гр|грам|грамм|граммов|кг|килограмм|мл|миллилитров|л|литр|г\.|шт|порци[яиею]|штук[аи]?)\.?\)?)\s+и\s+",
        r"\1, ",
        recipe_body,
        flags=re.IGNORECASE,
    )

    # Split recipe body into items
    # Delimiters: newline, semicolon, comma (except comma inside numbers or "творог, 100г")
    # First protect decimal commas in numbers like "1,5" or "0,5"
    protected_body = re.sub(r"(\d+),(\d+)", r"\1.\2", recipe_body)
    raw_lines = re.split(r"[\n;\u2022\*\+]|(?<=\D),(?=\s*[а-яёa-z0-9])", protected_body, flags=re.IGNORECASE)

    ingredients: list[ParsedIngredient] = []

    for raw_part in raw_lines:
        part = raw_part.strip()
        if not part:
            continue
        # Strip leading dashes / bullets
        part = re.sub(r"^[-–—\s*]+", "", part).strip()
        # Strip trailing punctuation (. , ; !)
        part = re.sub(r"[.,;!]+$", "", part).strip()
        # Strip leftover connective verbs at start of part (e.g. "съел 2 яйца", "выпил 200 мл молока")
        part = re.sub(r"^(?:съел[а-яё]*|поел[а-яё]*|скушал[а-яё]*|выпил[а-яё]*|употребил[а-яё]*|добавил[а-яё]*|взял[а-яё]*)\s+", "", part, flags=re.IGNORECASE).strip()
        if not part:
            continue

        # Check exclusions or meta-weight fragments
        if re.match(r"^(?:без|не\s+добавлять|убрать|исключить)\b", part, re.IGNORECASE):
            continue
        if re.match(r"^(?:это\s+)?(?:блюдо|порция|еда|тарелка|всё|всего)\s+(?:весит|вес|масса|здесь|тут)?\s*\d", part, re.IGNORECASE):
            continue

        # Check egg dish pattern, e.g. "яичница из 2 яиц", "омлет из 3 яиц"
        egg_dish_match = re.search(
            r"\b(?:яичниц[аыеу]|омлет[а-яё]*|глазунь[а-яё]*)\s+(?:из|с)?\s*(\d+)\s*(?:яиц|яйца|яйцо)\b",
            part,
            re.IGNORECASE,
        )
        if egg_dish_match:
            cnt = int(egg_dish_match.group(1))
            calc_grams = round(cnt * 55.0, 1)
            prep = "жареный" if any(w in part.lower() for w in ("яичниц", "глазунь")) else "омлет"
            ingredients.append(
                ParsedIngredient(
                    raw_text=part,
                    name=f"Яичница из {cnt} яиц" if "яичниц" in part.lower() else f"Омлет из {cnt} яиц",
                    normalized_name="яйцо куриное",
                    grams=calc_grams,
                    grams_min=round(cnt * 45.0, 1),
                    grams_max=round(cnt * 65.0, 1),
                    quantity=float(cnt),
                    unit="шт",
                    uncertainty=f"{cnt} шт (~{calc_grams} г)",
                    is_piece=True,
                    is_exact=False,
                    preparation=prep,
                )
            )
            continue

        # Check calibrated household measure, e.g. "тарелка борща", "горсть миндаля", "столовая ложка масла"
        from gateway.nutrition.household_measures import extract_household_measure_from_phrase
        hm_res = extract_household_measure_from_phrase(part)
        if hm_res:
            resolution, food_name = hm_res
            prep = _extract_preparation(part)
            ingredients.append(
                ParsedIngredient(
                    raw_text=part,
                    name=food_name.capitalize(),
                    normalized_name=food_name.lower(),
                    grams=resolution.grams_center,
                    grams_min=resolution.grams_min,
                    grams_max=resolution.grams_max,
                    quantity=resolution.quantity,
                    unit=resolution.typical_unit,
                    uncertainty=resolution.uncertainty_text,
                    is_piece=False,
                    is_exact=False,
                    preparation=prep,
                    household_measure=resolution.measure_type,
                )
            )
            continue

        # Check for fraction / piece format, e.g. "1/2 яйца", "2 варёных яйца", "два варёных яйца", "1.5 яблока"
        _num_words_alt = "|".join(_RUSSIAN_WORD_NUMBERS.keys())
        piece_match = re.match(
            rf"^(\d+(?:[./]\d+)?|{_num_words_alt})\s*(?:шт(?:ук[аи]?)?\.?\s*)?([а-яёa-z\s]+)$",
            part,
            re.IGNORECASE,
        )
        if not piece_match:
            postfix_match = re.match(
                rf"^([а-яёa-z\s]+?)\s+(\d+(?:[./]\d+)?|{_num_words_alt})\s*(?:шт(?:ук[аи]?)?\.?)$",
                part,
                re.IGNORECASE,
            )
            if postfix_match:
                qty_token = postfix_match.group(2).strip().lower()
                name_token = postfix_match.group(1).strip().lower()
            else:
                qty_token = ""
                name_token = ""
        else:
            qty_token = piece_match.group(1).strip().lower()
            name_token = piece_match.group(2).strip().lower()

        fraction_parsed = False
        if qty_token and name_token:
            if qty_token in _RUSSIAN_WORD_NUMBERS:
                qty = _RUSSIAN_WORD_NUMBERS[qty_token]
                qty_str = str(int(qty)) if qty.is_integer() else str(qty)
            else:
                qty = _parse_fraction(qty_token)
                qty_str = qty_token
            if qty is not None and any(pk in name_token for pk in _PIECE_WEIGHT_REFERENCE):
                matched_pk = next(pk for pk in _PIECE_WEIGHT_REFERENCE if pk in name_token)
                unit_weight = _PIECE_WEIGHT_REFERENCE[matched_pk]
                calc_grams = round(qty * unit_weight, 1)
                prep = _extract_preparation(part)

                # Format user-friendly name for plural boiled/fried eggs or general inflections
                if "варён" in name_token or "варен" in name_token:
                    disp_name = "Варёные яйца" if qty > 1 else "Варёное яйцо"
                elif "жарен" in name_token or "поджарен" in name_token:
                    disp_name = "Жареные яйца" if qty > 1 else "Жареное яйцо"
                else:
                    from gateway.nutrition.household_measures import normalize_food_inflection
                    norm_cand = normalize_food_inflection(name_token)
                    disp_name = norm_cand.capitalize() if norm_cand else name_token.capitalize()

                ingredients.append(
                    ParsedIngredient(
                        raw_text=part,
                        name=disp_name,
                        normalized_name=matched_pk,
                        grams=calc_grams,
                        grams_min=round(calc_grams * 0.85, 1),
                        grams_max=round(calc_grams * 1.15, 1),
                        quantity=qty,
                        unit="шт",
                        uncertainty=f"{qty_str} шт (~{calc_grams} г)",
                        is_piece=True,
                        is_exact=False,
                        preparation=prep,
                    )
                )
                fraction_parsed = True

        if fraction_parsed:
            continue

        # Look for grams / kilograms / ml / liters
        # Examples: "армянский лаваш 38 грамм", "творог 2% 100 грамм", "мак 30 грам", "молоко 200мл"
        paren_weight = re.search(
            r"\(\s*(?:(?:весом|массой)\s+(?:в\s+)?)?(\d+(?:\.\d+)?)\s*(?:[xх]\s*(\d+(?:\.\d+)?))?\s*(г|гр|грам|грамм|граммов|кг|килограмм|мл|миллилитров|л|литр|г\.)\s*\)",
            part,
            re.IGNORECASE,
        )
        if paren_weight:
            weight_match = paren_weight
            num1 = float(weight_match.group(1))
            num2 = float(weight_match.group(2)) if weight_match.group(2) else 1.0
            unit_raw = weight_match.group(3).lower()
            val = num1 * num2
            grams = round(val * 1000.0, 1) if unit_raw in {"кг", "килограмм", "л", "литр"} else round(val, 1)
            name_part = part[:weight_match.start()] + part[weight_match.end():]
        else:
            weight_match = re.search(
                r"(?:(?:весом|массой)\s+(?:в\s+)?)?(\d+(?:\.\d+)?)\s*(?:[xх]\s*(\d+(?:\.\d+)?))?\s*(г|гр|грам|грамм|граммов|кг|килограмм|мл|миллилитров|л|литр|г\.)\b",
                part,
                re.IGNORECASE,
            )
            if weight_match:
                num1 = float(weight_match.group(1))
                num2 = float(weight_match.group(2)) if weight_match.group(2) else 1.0
                unit_raw = weight_match.group(3).lower()
                val = num1 * num2
                grams = round(val * 1000.0, 1) if unit_raw in {"кг", "килограмм", "л", "литр"} else round(val, 1)
                name_part = part[:weight_match.start()] + part[weight_match.end():]
            else:
                weight_match = None

        if weight_match:
            # Strip leftover empty parentheses, punctuation, and weight words like 'весом'
            name_part = re.sub(r"\(\s*\)", "", name_part).strip()
            name_part = re.sub(r"\b(?:весом|массой)\s*(?:в)?\b", "", name_part, flags=re.IGNORECASE).strip()
            name_part = re.sub(r"[,:\-\s.]+$", "", name_part).strip()
            name_part = re.sub(r"^[,:\-\s.]+", "", name_part).strip()

            # Check if name_part has a leading quantity (e.g. "один банан", "2 варёных яйца")
            _num_words_alt = "|".join(_RUSSIAN_WORD_NUMBERS.keys())
            lead_qty_match = re.match(
                rf"^(\d+(?:[./]\d+)?|{_num_words_alt})\s*(?:шт(?:ук[аи]?)?\.?\s+)?(.+)$",
                name_part,
                re.IGNORECASE,
            )
            item_qty = 1.0
            is_piece_item = False
            if lead_qty_match:
                q_token = lead_qty_match.group(1).lower()
                r_name = lead_qty_match.group(2).strip()
                if q_token in _RUSSIAN_WORD_NUMBERS:
                    item_qty = _RUSSIAN_WORD_NUMBERS[q_token]
                    is_piece_item = True
                    name_part = r_name
                else:
                    parsed_lead_q = _parse_fraction(q_token)
                    if parsed_lead_q is not None:
                        item_qty = parsed_lead_q
                        is_piece_item = True
                        name_part = r_name

            # Check fat percentage: e.g. "творог 2%"
            fat_match = re.search(r"(\d+(?:\.\d+)?)\s*%", name_part)
            fat_percent = float(fat_match.group(1)) if fat_match else None
            clean_name = re.sub(r"\d+(?:\.\d+)?\s*%", "", name_part).strip()
            clean_name = re.sub(r"\(\s*\)", "", clean_name).strip()
            prep = _extract_preparation(part)
            clean_name = re.sub(r"\s+", " ", clean_name).strip()

            if clean_name:
                if re.match(r"^(?:это\s+)?(?:блюдо|порция|еда|тарелка|всё|всего)?\s*(?:весит|вес|масса|здесь|тут)?$", clean_name, re.IGNORECASE):
                    continue
                from gateway.healbite_food_composition import resolve_food_composition
                from gateway.nutrition.household_measures import normalize_food_inflection
                norm_key = clean_name.lower()
                cand = normalize_food_inflection(norm_key)
                if cand and cand != norm_key:
                    cand_res = resolve_food_composition(cand)
                    if cand_res and cand_res.matched:
                        norm_key = cand
                if norm_key == clean_name.lower():
                    direct_match = resolve_food_composition(norm_key)
                    if not (direct_match and direct_match.matched):
                        cand_res = resolve_food_composition(cand) if cand else None
                        if cand_res and cand_res.matched:
                            norm_key = cand
                        else:
                            cut_stripped = re.sub(r"\b(?:стейк\s+(?:из|от)?|шницель\s+(?:из|от)?)\s*", "", clean_name, flags=re.IGNORECASE).strip()
                            cand2 = normalize_food_inflection(cut_stripped.lower()) if cut_stripped else ""
                            if cand2:
                                cand2_res = resolve_food_composition(cand2)
                                if cand2_res and cand2_res.matched:
                                    norm_key = cand2

                # Format display title:
                # If plural boiled/fried eggs:
                if ("варён" in clean_name.lower() or "варен" in clean_name.lower()) and "яйц" in clean_name.lower():
                    disp_title = "Варёные яйца" if item_qty > 1 else "Варёное яйцо"
                elif ("жарен" in clean_name.lower() or "поджарен" in clean_name.lower()) and "яйц" in clean_name.lower():
                    disp_title = "Жареные яйца" if item_qty > 1 else "Жареное яйцо"
                else:
                    norm_display = normalize_food_inflection(clean_name.lower())
                    disp_title = norm_display.capitalize() if norm_display != clean_name.lower() else (name_part.capitalize() if name_part else clean_name.capitalize())

                disp_title = re.sub(r"\(\s*\)", "", disp_title).strip()

                unit_label = "шт" if is_piece_item else ("мл" if unit_raw in {"мл", "миллилитров"} else ("литр" if unit_raw in {"л", "литр"} else "грамм"))
                uncertainty_label = f"{grams} мл" if unit_raw in {"мл", "миллилитров"} else f"{grams} г"
                if is_piece_item:
                    qty_str = str(int(item_qty)) if item_qty.is_integer() else str(item_qty)
                    uncertainty_label += f" ({qty_str} шт)"
                if fat_percent is not None:
                    uncertainty_label += f" ({fat_percent}%)"

                ingredients.append(
                    ParsedIngredient(
                        raw_text=part,
                        name=disp_title,
                        normalized_name=norm_key,
                        grams=grams,
                        grams_min=grams,
                        grams_max=grams,
                        quantity=item_qty,
                        unit=unit_label,
                        fat_percent=fat_percent,
                        uncertainty=uncertainty_label,
                        is_piece=is_piece_item,
                        is_exact=True,
                        preparation=prep,
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
            prep = _extract_preparation(clean_text)
            low_text = clean_text.lower()
            matched_ref = next((pk for pk in _PIECE_WEIGHT_REFERENCE if pk in low_text), None)
            if matched_ref:
                pw = _PIECE_WEIGHT_REFERENCE[matched_ref]
                ingredients.append(
                    ParsedIngredient(
                        raw_text=part,
                        name=clean_text.capitalize(),
                        normalized_name=matched_ref,
                        grams=pw,
                        grams_min=round(pw * 0.85, 1),
                        grams_max=round(pw * 1.15, 1),
                        quantity=1.0,
                        unit="шт" if pw <= 150.0 else "порция",
                        uncertainty=f"~{pw:g} г ({matched_ref})",
                        is_piece=True,
                        is_exact=False,
                        preparation=prep,
                    )
                )
            else:
                ingredients.append(
                    ParsedIngredient(
                        raw_text=part,
                        name=clean_text.capitalize(),
                        normalized_name=clean_text.lower(),
                        grams=100.0,  # Default provisional portion
                        grams_min=80.0,
                        grams_max=120.0,
                        quantity=1.0,
                        unit="порция",
                        uncertainty="Вес не указан (~100 г)",
                        is_piece=False,
                        is_exact=False,
                        preparation=prep,
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
        r"\b([а-яёa-z]+(?:\s+[а-яёa-z]+)?)\s+(?:здесь|тут)\s*(?:весит|вес)?\s*(\d+(?:[.,]\d+)?)\s*(?:г|гр|грам|грамм|г\.)\b",
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
        r"(?:здесь|тут)\s+(\d+(?:[.,]\d+)?)\s*(?:г|гр|грам|грамм|г\.)\s+([а-яёa-z]+(?:\s+[а-яёa-z]+)?)\b",
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
