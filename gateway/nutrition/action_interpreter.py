"""
gateway.nutrition.action_interpreter — Intelligent natural language interpreter
for pending nutrition inventory editing.

Combines a fast deterministic parser (Tier 1) for natural language editing patterns,
KBZHU overrides, decimal commas, conversational phrasing, and batch actions with
an LLM semantic fallback (Tier 2) when complex phrasing is ambiguous.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Sequence

from gateway.nutrition.user_override import (
    ActionOperation,
    NutritionBasis,
    StructuredAction,
    StructuredActionPlan,
    UserNutritionOverride,
)
from gateway.nutrition.household_measures import (
    detect_household_measure_token,
    extract_household_measure_from_phrase,
    normalize_food_inflection,
)

logger = logging.getLogger("gateway.nutrition.action_interpreter")

_SQL_CODE_INJECTION_RE = re.compile(
    r"\b(?:select\s+.*\s+from|insert\s+into|delete\s+from|drop\s+table|update\s+.*\s+set|union\s+select|exec\s*\(|eval\s*\(|__import__)\b",
    re.IGNORECASE,
)
_PROMPT_INJECTION_RE = re.compile(
    r"\b(?:ignore\s+(?:all\s+)?previous\s+instructions|system\s+prompt|bypass\s+(?:all\s+)?rules|disregard\s+prior)\b",
    re.IGNORECASE,
)

_NUM_WORD_TO_INDEX: dict[str, int] = {
    "первое": 1,
    "первую": 1,
    "первый": 1,
    "1-е": 1,
    "1-й": 1,
    "второе": 2,
    "вторую": 2,
    "второй": 2,
    "2-е": 2,
    "2-й": 2,
    "третье": 3,
    "третью": 3,
    "третий": 3,
    "3-е": 3,
    "3-й": 3,
    "четвертое": 4,
    "четвёртое": 4,
    "четвертую": 4,
    "четвёртую": 4,
    "четвертый": 4,
    "четвёртый": 4,
    "пятое": 5,
    "пятую": 5,
    "пятый": 5,
    "шестое": 6,
    "шестую": 6,
    "шестой": 6,
    "седьмое": 7,
    "седьмую": 7,
    "седьмой": 7,
    "восьмое": 8,
    "восьмую": 8,
    "восьмой": 8,
    "девятое": 9,
    "девятую": 9,
    "девятый": 9,
    "десятое": 10,
    "десятую": 10,
    "десятый": 10,
}

_CALORIE_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ккал|калорий|ккалорий|kcal)\b", re.IGNORECASE)
_PROTEIN_TOKEN_RE = re.compile(r"\b(?:б|белк[аов]*)(?:\s+(?:там|в\s+н[её]м|в\s+ней))?\s*[:=\-—]?\s*(\d+(?:\.\d+)?)\s*(?:г|грамм[а-я]*)?\b", re.IGNORECASE)
_FAT_TOKEN_RE = re.compile(r"\b(?:ж|жир[аов]*)(?:\s+(?:там|в\s+н[её]м|в\s+ней))?\s*[:=\-—]?\s*(\d+(?:\.\d+)?)\s*(?:г|грамм[а-я]*)?\b", re.IGNORECASE)
_CARBS_TOKEN_RE = re.compile(r"\b(?:у|углев[а-я]*)(?:\s+(?:там|в\s+н[её]м|в\s+ней))?\s*[:=\-—]?\s*(\d+(?:\.\d+)?)\s*(?:г|грамм[а-я]*)?\b", re.IGNORECASE)
_GRAMS_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:г|гр|грамм(?:а|ов)?)\b", re.IGNORECASE)

_SCALE_HALF_RE = re.compile(r"\b(?:съел[аио]?|оставил[аио]?|половин[ау]|вдвое\s+меньше|наполовину)\b", re.IGNORECASE)
_SCALE_THIRD_RE = re.compile(r"\b(?:треть|одну\s+треть)\b", re.IGNORECASE)
_SCALE_QUARTER_RE = re.compile(r"\b(?:четверть|одну\s+четверть)\b", re.IGNORECASE)

_LEAVE_REST_RE = re.compile(r"\b(?:остальн(?:ое|ые)\s+оставь|остальное\s+без\s+изменений|больше\s+ничего\s+не\s+меняй|остальное\s+как\s+было)\b", re.IGNORECASE)
_SAVE_COMMAND_RE = re.compile(r"\b(?:сохрани(?:ть)?|запиши(?:ть)?|сохраняй|зафиксируй)\b", re.IGNORECASE)


def _to_float(val: str | None) -> float | None:
    if not val:
        return None
    try:
        return float(val.replace(",", ".").strip())
    except ValueError:
        return None


def parse_deterministic_action_plan(
    text: str,
    items: Sequence[Any],
    *,
    user_id: int = 0,
    inventory_id: str = "",
    inventory_version: int = 1,
) -> StructuredActionPlan | None:
    """
    Tier 1: Deterministic natural language parser for multi-action inventory editing,
    custom KBZHU overrides, portion scaling, deletions, and additions.
    Returns StructuredActionPlan or None if no recognized actions found.
    """
    if not text or not text.strip():
        return None

    raw = text.strip()

    # 1. Security check
    if _SQL_CODE_INJECTION_RE.search(raw) or _PROMPT_INJECTION_RE.search(raw):
        logger.warning("[ActionInterpreter] Security violation detected in user text: %s", raw[:60])
        return StructuredActionPlan(
            inventory_id=inventory_id,
            inventory_version=inventory_version,
            user_id=user_id,
            actions=[],
            confidence=0.0,
            requires_clarification=True,
            clarification_question="Запрос содержит недопустимые команды или символы. Пожалуйста, укажите изменения обычным текстом.",
            raw_user_message=raw,
        )

    # 2. Check for general non-food casual greetings or queries
    casual_greetings = {"привет", "здравствуй", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "как дела", "погода", "кто ты", "помощь"}
    lowered_simple = re.sub(r"[^\w\s]", "", raw.lower()).strip()
    if lowered_simple in casual_greetings:
        return None

    # Check bare number commands: "добавь 200", "поставь 200", "200"
    if re.search(r"^(?:поставь|сделай|измени|исправь|добавь|установи)\s+(\d{1,4})$", lowered_simple, re.IGNORECASE) or lowered_simple.isdigit():
        return StructuredActionPlan(
            inventory_id=inventory_id,
            inventory_version=inventory_version,
            user_id=user_id,
            actions=[],
            confidence=0.5,
            requires_clarification=True,
            clarification_question="Уточните, что означает число: граммы, калории или номер позиции?",
            raw_user_message=raw,
        )

    # Check for ambiguous command: e.g. "Замени это на то"
    if re.search(r"\bзамени\s+(?:это\s+на\s+то|продукт|блюдо)\b", raw, re.IGNORECASE):
        return StructuredActionPlan(
            inventory_id=inventory_id,
            inventory_version=inventory_version,
            user_id=user_id,
            actions=[],
            confidence=0.5,
            requires_clarification=True,
            clarification_question="Уточните, какую именно позицию нужно заменить и на что.",
            raw_user_message=raw,
        )

    # Check wholesale composition rewrite: e.g. "Исправь состав: рис 150 г, курица 120 г, масло 10 г"
    recipe_replace_match = re.search(
        r"^(?:(?:пожалуйста\s+)?(?:исправь|замени|измени|новый|полный)?\s*состав\s*[:]\s*(.+)|состав\s*[:]\s*(.+))$",
        raw,
        re.IGNORECASE,
    )
    if recipe_replace_match:
        body = (recipe_replace_match.group(1) or recipe_replace_match.group(2)).strip()
        protected_body = re.sub(r"(\d+),(\d+)", r"\1.\2", body)
        recipe_tokens = re.split(r"[\n;,]+", protected_body)
        parsed_actions: list[StructuredAction] = []
        for itm in items:
            parsed_actions.append(StructuredAction(
                operation=ActionOperation.REMOVE_ITEM,
                target_index=itm.index,
            ))
        for tok in recipe_tokens:
            t = tok.strip()
            if not t:
                continue
            hm_res = extract_household_measure_from_phrase(t)
            if hm_res:
                resolution, food_name = hm_res
                parsed_actions.append(StructuredAction(
                    operation=ActionOperation.ADD_ITEM,
                    name=food_name.capitalize(),
                    grams=resolution.grams_center,
                ))
                continue
            g_m = _GRAMS_TOKEN_RE.search(t)
            g_val = _to_float(g_m.group(1)) if g_m else None
            n_part = t[:g_m.start()] + t[g_m.end():] if g_m else t
            n_clean = re.sub(r"[,:\-\s]+", " ", n_part).strip()
            if n_clean:
                norm_name = normalize_food_inflection(n_clean)
                parsed_actions.append(StructuredAction(
                    operation=ActionOperation.ADD_ITEM,
                    name=norm_name.capitalize(),
                    grams=g_val if g_val is not None else 100.0,
                ))
        if parsed_actions:
            return StructuredActionPlan(
                inventory_id=inventory_id,
                inventory_version=inventory_version,
                user_id=user_id,
                actions=parsed_actions,
                confidence=1.0,
                requires_clarification=False,
                raw_user_message=raw,
            )

    # Check for ambiguous units: e.g. "160 ложек", "90 баррелей", but allow calibrated household measures
    if re.search(r"\d+\s*(?:ложек|ложки|стаканов|стакана|литров|баррелей|ведр[ао])\b", raw, re.IGNORECASE):
        if not detect_household_measure_token(raw):
            return StructuredActionPlan(
                inventory_id=inventory_id,
                inventory_version=inventory_version,
                user_id=user_id,
                actions=[],
                confidence=0.5,
                requires_clarification=True,
                clarification_question="Уточните вес продукта в граммах или штуках.",
                raw_user_message=raw,
            )

    actions: list[StructuredAction] = []
    request_save = bool(_SAVE_COMMAND_RE.search(raw))

    # Protect decimal commas in numbers (e.g. 16,8 -> 16.8)
    protected = re.sub(r"(\d+),(\d+)", r"\1.\2", raw)

    # Split into lines, distinct sentences, or comma/conjunction-separated actions
    action_trigger_re = re.compile(
        r"(?:,\s*|\s+(?:а|и|но)\s+)(?=(?:добавь(?:те)?|добавить|убери(?:те)?|удали(?:ть)?|исключи(?:ть)?|вычеркни|замени(?:ть)?|исправь(?:ть)?|вместо|переименуй(?:те)?|назови|вес\s+(?:позиции\s+|строки\s+|номера\s+)?\d+\s*[:\.\-—]|вес\s*[:]\s*\d+)\b)",
        re.IGNORECASE,
    )
    major_clauses = re.split(r"[\n;]+|(?<=\w)[.!?]+(?=\s+|$)", protected)
    clauses: list[str] = []
    for major in major_clauses:
        m_str = major.strip()
        if not m_str:
            continue
        sub_parts = action_trigger_re.split(m_str)
        for sp in sub_parts:
            sp_clean = sp.strip()
            if sp_clean:
                clauses.append(sp_clean)

    last_target_index: int | None = None

    for clause in clauses:
        c = clause.strip()
        if not c:
            continue

        c_clean = re.sub(r"^[-–—•*\s]+", "", c).strip()
        if not c_clean:
            continue

        # A0. Check control clauses
        if _LEAVE_REST_RE.search(c_clean):
            continue
        if re.match(r"^(?:сохрани(?:ть)?|запиши(?:ть)?|ок|подтверждаю|готово)[\.\!\s]*$", c_clean, re.IGNORECASE):
            request_save = True
            continue

        # A. Check scale portion: "съела половину"
        if _SCALE_HALF_RE.search(c_clean):
            actions.append(StructuredAction(operation=ActionOperation.SCALE_PORTION, scale_factor=0.5))
            continue
        if _SCALE_THIRD_RE.search(c_clean):
            actions.append(StructuredAction(operation=ActionOperation.SCALE_PORTION, scale_factor=1.0 / 3.0))
            continue
        if _SCALE_QUARTER_RE.search(c_clean):
            actions.append(StructuredAction(operation=ActionOperation.SCALE_PORTION, scale_factor=0.25))
            continue

        # B. Check removal: "Удалить 6", "Удали апельсин", "Удали последнюю позицию", "Убери лук"
        remove_match = re.search(
            r"^(?:удали(?:ть)?|убери|исключи|вычеркни)\s+(?:позицию\s+|номер\s+|строку\s+)?(\d{1,2}|последн[юя][юя](?:\s+позицию)?|.+?)(?:\s+из\s+списка|\s+из\s+блюда)?$",
            c_clean,
            re.IGNORECASE,
        )
        if remove_match:
            target_str = remove_match.group(1).strip()
            target_str = re.sub(r"[,:\.\s]+$", "", target_str).strip()
            if target_str.isdigit():
                idx = int(target_str)
                actions.append(StructuredAction(operation=ActionOperation.REMOVE_ITEM, target_index=idx))
            elif "последн" in target_str.lower():
                actions.append(StructuredAction(operation=ActionOperation.REMOVE_ITEM, target_index=-1))
            else:
                norm_target = normalize_food_inflection(target_str)
                actions.append(StructuredAction(operation=ActionOperation.REMOVE_ITEM, target_name=norm_target))
            continue

        # C. Check addition: "Добавить: сыр, 30 г", "Добавь 30 г сыра", "Добавь столовую ложку масла"
        add_match = re.search(
            r"^(?:добавь(?:те)?|добавить)\s*[:]?\s*(.+)$",
            c_clean,
            re.IGNORECASE,
        )
        if add_match:
            body = add_match.group(1).strip()
            hm_res = extract_household_measure_from_phrase(body)
            if hm_res:
                resolution, food_name = hm_res
                actions.append(StructuredAction(
                    operation=ActionOperation.ADD_ITEM,
                    name=food_name.capitalize(),
                    grams=resolution.grams_center,
                ))
                continue
            g_match = _GRAMS_TOKEN_RE.search(body)
            grams_val = _to_float(g_match.group(1)) if g_match else 100.0
            name_part = body[:g_match.start()] + body[g_match.end():] if g_match else body
            name_clean = re.sub(r"[,:\-\s]+$", "", name_part).strip()
            name_clean = re.sub(r"^[,:\-\s]+", "", name_clean).strip()
            if name_clean:
                if name_clean.isdigit():
                    return StructuredActionPlan(
                        inventory_id=inventory_id,
                        inventory_version=inventory_version,
                        user_id=user_id,
                        actions=[],
                        confidence=0.5,
                        requires_clarification=True,
                        clarification_question="Уточните, что именно вы хотите добавить и в каком количестве.",
                        raw_user_message=raw,
                    )
                norm_name = normalize_food_inflection(name_clean)
                actions.append(StructuredAction(
                    operation=ActionOperation.ADD_ITEM,
                    name=norm_name.capitalize(),
                    grams=grams_val,
                ))
                continue

        # D1. Check explicit rename: "Переименуй 1 в Бельгийские вафли"
        rename_match = re.search(
            r"^(?:переименуй(?:те)?|назови|измени\s+название)\s+(?:позици[юи]\s+|строк[ую]\s+|номер\s+)?(\d{1,2}|перв[уюоея][юя]?\s*(?:позицию)?|втор[уюоея][юя]?\s*(?:позицию)?|трет[ьюея][юя]?\s*(?:позицию)?|четверт[уюоея][юя]?\s*(?:позицию)?|пят[уюоея][юя]?\s*(?:позицию)?|шест[уюоея][юя]?\s*(?:позицию)?|последн[юяе][юя]?\s*(?:позицию)?|[а-яa-z\s]+?)\s+(?:в|на)\s+(.+)$",
            c_clean,
            re.IGNORECASE,
        )
        if rename_match:
            t_str = rename_match.group(1).strip()
            new_title = rename_match.group(2).strip()
            r_idx = int(t_str) if t_str.isdigit() else None
            r_name = t_str if not t_str.isdigit() else None
            if r_idx is None and r_name:
                t_low = r_name.lower()
                for kw, kw_idx in _NUM_WORD_TO_INDEX.items():
                    if kw in t_low:
                        r_idx = kw_idx
                        r_name = None
                        break
            actions.append(StructuredAction(
                operation=ActionOperation.REPLACE_ITEM,
                target_index=r_idx,
                target_name=r_name,
                name=new_title,
            ))
            continue

        # D2. Check "Это не курица, а слабосолёный лосось 90 грамм"
        not_this_match = re.search(
            r"^(?:это\s+)?не\s+(.+?),\s*а\s+(.+)$",
            c_clean,
            re.IGNORECASE,
        )
        if not_this_match:
            old_name = not_this_match.group(1).strip()
            new_val = not_this_match.group(2).strip()
            g_m = _GRAMS_TOKEN_RE.search(new_val)
            g_val = _to_float(g_m.group(1)) if g_m else None
            n_part = new_val[:g_m.start()] + new_val[g_m.end():] if g_m else new_val
            n_clean = re.sub(r"[,:\-\s]+", " ", n_part).strip()
            actions.append(StructuredAction(
                operation=ActionOperation.REPLACE_ITEM,
                target_name=old_name,
                name=n_clean.strip() if n_clean else new_val.strip(),
                grams=g_val,
            ))
            continue

        # D3. Check "Замени 2 на рыбу" / "Замени свинину на говядину, вес мяса 150 г"
        replace_named_match = re.search(
            r"^(?:замени(?:ть)?|исправь(?:ть)?)\s+(?:позицию\s+|строку\s+|номер\s+)?(\d{1,2}|перв[уюоея][юя]?\s*(?:позицию)?|втор[уюоея][юя]?\s*(?:позицию)?|трет[ьюея][юя]?\s*(?:позицию)?|четверт[уюоея][юя]?\s*(?:позицию)?|пят[уюоея][юя]?\s*(?:позицию)?|шест[уюоея][юя]?\s*(?:позицию)?|последн[юяе][юя]?\s*(?:позицию)?|[а-яa-z\s]+?)\s+на\s+(.+)$",
            c_clean,
            re.IGNORECASE,
        )
        if replace_named_match:
            target_str = replace_named_match.group(1).strip()
            new_val = replace_named_match.group(2).strip()
            idx = int(target_str) if target_str.isdigit() else None
            t_name = target_str if not target_str.isdigit() else None
            if idx is None and t_name:
                t_lower = t_name.lower()
                for kw, kw_idx in _NUM_WORD_TO_INDEX.items():
                    if kw in t_lower:
                        idx = kw_idx
                        t_name = None
                        break
            g_m = _GRAMS_TOKEN_RE.search(new_val)
            g_val = _to_float(g_m.group(1)) if g_m else None
            n_part = new_val[:g_m.start()] + new_val[g_m.end():] if g_m else new_val
            n_part = re.sub(r"\b(?:вес\s*(?:мяса|продукта|позиции|блюда)?|масса)\b", "", n_part, flags=re.IGNORECASE)
            n_clean = re.sub(r"[,:\-\s]+", " ", n_part).strip()
            hm_res = extract_household_measure_from_phrase(new_val)
            if hm_res and g_val is None:
                resolution, food_name = hm_res
                n_clean = food_name
                g_val = resolution.grams_center
            if n_clean:
                n_clean = normalize_food_inflection(n_clean)
            if t_name:
                t_name = normalize_food_inflection(t_name)
            actions.append(StructuredAction(
                operation=ActionOperation.REPLACE_ITEM,
                target_index=idx,
                target_name=t_name,
                name=n_clean.capitalize() if n_clean else None,
                grams=g_val,
            ))
            if idx:
                last_target_index = idx
            continue

        # D4. Check "Вместо жареной курицы используй запечённую, вес 180 г"
        instead_match = re.search(
            r"^вместо\s+(.+?)\s+(?:используй(?:те)?|возьми(?:те)?|поставь(?:те)?|сделай(?:те)?|напиши(?:те)?|выбери(?:те)?)\s+(.+)$",
            c_clean,
            re.IGNORECASE,
        )
        if instead_match:
            old_name = instead_match.group(1).strip()
            new_val = instead_match.group(2).strip()
            g_m = _GRAMS_TOKEN_RE.search(new_val)
            g_val = _to_float(g_m.group(1)) if g_m else None
            n_part = new_val[:g_m.start()] + new_val[g_m.end():] if g_m else new_val
            n_part = re.sub(r"\b(?:вес\s*(?:мяса|продукта|позиции|блюда)?|масса)\b", "", n_part, flags=re.IGNORECASE)
            n_clean = re.sub(r"[,:\-\s]+", " ", n_part).strip()
            hm_res = extract_household_measure_from_phrase(new_val)
            if hm_res and g_val is None:
                resolution, food_name = hm_res
                n_clean = food_name
                g_val = resolution.grams_center
            if n_clean:
                n_clean = normalize_food_inflection(n_clean)
            if old_name:
                old_name = normalize_food_inflection(old_name)
            if n_clean.endswith(("ую", "ый", "ая", "ое", "ые", "ой")) and any(noun in old_name.lower() for noun in ("куриц", "индейк", "говядин", "свинин", "рыб", "мяс")):
                for noun in ("курица", "индейка", "говядина", "свинина", "рыба", "мясо"):
                    if noun[:4] in old_name.lower():
                        n_clean = f"{noun} {n_clean}"
                        break
            actions.append(StructuredAction(
                operation=ActionOperation.REPLACE_ITEM,
                target_name=old_name,
                name=n_clean.capitalize() if n_clean else new_val.strip(),
                grams=g_val,
            ))
            continue

        # E. Check conversational weight update: "Вафель было не 125, а 160 граммов"
        not_g_match = re.search(
            r"^(.+?)\s+было\s+не\s+\d+,\s*а\s+(\d+(?:\.\d+)?)\s*(?:грамм(?:ов)?|г)\b",
            c_clean,
            re.IGNORECASE,
        )
        if not_g_match:
            item_n = not_g_match.group(1).strip()
            new_g = _to_float(not_g_match.group(2))
            actions.append(StructuredAction(
                operation=ActionOperation.UPDATE_WEIGHT,
                target_name=item_n,
                grams=new_g,
            ))
            continue

        # F. Check conversational property updates:
        in_item_match = re.search(
            r"^(?:в|во)\s+(первом|втором|третьем|четвертом|четвёртом|пятом|шестом|\d{1,2}[-\s]*(?:м|ом)?)\s+(?:продукте|позиции|строке)\s+(.+)$",
            c_clean,
            re.IGNORECASE,
        )
        if in_item_match:
            pos_token = in_item_match.group(1).lower()
            rest = in_item_match.group(2).strip()
            idx = None
            for kw, kw_idx in _NUM_WORD_TO_INDEX.items():
                if kw in pos_token or pos_token.startswith(kw[:4]):
                    idx = kw_idx
                    break
            if idx is None and pos_token[0].isdigit():
                idx = int(re.sub(r"\D", "", pos_token))

            if idx is not None:
                last_target_index = idx
                cal_m = _CALORIE_TOKEN_RE.search(rest)
                if cal_m:
                    cal_v = _to_float(cal_m.group(1))
                    actions.append(StructuredAction(
                        operation=ActionOperation.SET_USER_NUTRITION,
                        target_index=idx,
                        user_nutrition=UserNutritionOverride(
                            calories_kcal=cal_v,
                            nutrition_basis=NutritionBasis.TOTAL_PORTION.value,
                        ),
                    ))
                    continue
                g_m = _GRAMS_TOKEN_RE.search(rest)
                if g_m:
                    g_v = _to_float(g_m.group(1))
                    actions.append(StructuredAction(
                        operation=ActionOperation.UPDATE_WEIGHT,
                        target_index=idx,
                        grams=g_v,
                    ))
                    continue

        # Check "Белка там 16.8 грамма, жира 12.4, углеводов 17.2"
        macro_continuation = re.match(r"^(?:а\s+)?(?:там\s+)?(?:белк|жир|углев)", c_clean, re.IGNORECASE)
        if macro_continuation and last_target_index is not None:
            p_m = _PROTEIN_TOKEN_RE.search(c_clean)
            f_m = _FAT_TOKEN_RE.search(c_clean)
            c_m = _CARBS_TOKEN_RE.search(c_clean)
            prev_nutr = None
            for act in reversed(actions):
                if act.target_index == last_target_index and act.user_nutrition:
                    prev_nutr = act.user_nutrition
                    break
            if prev_nutr is not None:
                if p_m: prev_nutr.protein_g = _to_float(p_m.group(1))
                if f_m: prev_nutr.fat_g = _to_float(f_m.group(1))
                if c_m: prev_nutr.carbs_g = _to_float(c_m.group(1))
            else:
                actions.append(StructuredAction(
                    operation=ActionOperation.SET_USER_NUTRITION,
                    target_index=last_target_index,
                    user_nutrition=UserNutritionOverride(
                        protein_g=_to_float(p_m.group(1)) if p_m else None,
                        fat_g=_to_float(f_m.group(1)) if f_m else None,
                        carbs_g=_to_float(c_m.group(1)) if c_m else None,
                        nutrition_basis=NutritionBasis.TOTAL_PORTION.value,
                    ),
                ))
            continue

        # Check piece count update: "Исправить 1: 7 шт" / "1: 7 шт" / "7 шт"
        count_match = re.match(
            r"^(?:(?:исправить|строка|позиция|номер)\s+)?(\d{1,2})\s*[:\.\-—]\s*(\d+)\s*(?:шт(?:ук[а-я]*)?)\s*$",
            c_clean,
            re.IGNORECASE,
        )
        if count_match:
            c_idx = int(count_match.group(1))
            c_cnt = int(count_match.group(2))
            actions.append(StructuredAction(
                operation=ActionOperation.UPDATE_WEIGHT,
                target_index=c_idx,
                quantity=c_cnt,
            ))
            continue

        # G. Compound item replace / edit line
        compound_idx: int | None = None
        compound_name: str | None = None

        prefix_m = re.match(r"^(?:(?:исправить|позиция|строка|номер|вес)\s+)?(\d{1,2})\s*[:\.\-—]\s*(.*)$", c_clean, re.IGNORECASE)
        body_to_parse = c_clean
        if prefix_m:
            compound_idx = int(prefix_m.group(1))
            body_to_parse = prefix_m.group(2).strip()
            # If multiple weights in a single item edit without nutrition macros: ambiguous/invalid
            if len(_GRAMS_TOKEN_RE.findall(body_to_parse)) > 1 and not (_CALORIE_TOKEN_RE.search(body_to_parse) or _PROTEIN_TOKEN_RE.search(body_to_parse)):
                return StructuredActionPlan(
                    inventory_id=inventory_id,
                    inventory_version=inventory_version,
                    user_id=user_id,
                    actions=[],
                    confidence=0.5,
                    requires_clarification=True,
                    clarification_question="Не понял команду. В одной команде можно указать только один компонент, например: Исправить 2: Сливочный сыр, 50 г или Вес 6: 200 г.",
                    raw_user_message=raw,
                )

        # Nutrition basis detection: "на 100 г" vs "в порции"
        basis = NutritionBasis.PER_100G.value if re.search(r"на\s+100\s*(?:г|грамм)\b", c_clean, re.IGNORECASE) else NutritionBasis.TOTAL_PORTION.value

        cal_m = _CALORIE_TOKEN_RE.search(body_to_parse)
        prot_m = _PROTEIN_TOKEN_RE.search(body_to_parse)
        fat_m = _FAT_TOKEN_RE.search(body_to_parse)
        carbs_m = _CARBS_TOKEN_RE.search(body_to_parse)
        grams_m = _GRAMS_TOKEN_RE.search(body_to_parse)

        has_nutrition = bool(cal_m or prot_m or fat_m or carbs_m)
        grams_val = _to_float(grams_m.group(1)) if grams_m else None
        if re.search(r"на\s+100\s*(?:г|грамм)\b", c_clean, re.IGNORECASE) and grams_m and re.search(r"на\s+100\s*(?:г|грамм)\b", body_to_parse[max(0, grams_m.start()-4):grams_m.end()], re.IGNORECASE):
            grams_val = None
        cal_val = _to_float(cal_m.group(1)) if cal_m else None
        prot_val = _to_float(prot_m.group(1)) if prot_m else None
        fat_val = _to_float(fat_m.group(1)) if fat_m else None
        carbs_val = _to_float(carbs_m.group(1)) if carbs_m else None

        earliest_pos = len(body_to_parse)
        for m in (grams_m, cal_m, prot_m, fat_m, carbs_m):
            if m and m.start() < earliest_pos:
                earliest_pos = m.start()

        name_part = body_to_parse[:earliest_pos].strip()
        name_clean = re.sub(r"[,:\-—]+$", "", name_part).strip()
        name_clean = re.sub(r"^[,:\-—]+", "", name_clean).strip()
        name_clean = re.sub(r"^(?:в\s+порции|на\s+100\s*(?:г|грамм(?:а|ов)?))\s*", "", name_clean, flags=re.IGNORECASE).strip()
        if name_clean.lower() in ("на", "в", "порция", "порции", ""):
            compound_name = None
        elif name_clean:
            compound_name = name_clean.strip()

        if compound_idx is None and compound_name:
            c_name_folded = compound_name.casefold()
            for itm in items:
                v_name = getattr(itm, "visible_name", "").casefold()
                n_name = getattr(itm, "normalized_name", "").casefold()
                if c_name_folded in v_name or c_name_folded in n_name or v_name in c_name_folded:
                    compound_idx = itm.index
                    break

        # Single item default if only 1 item in inventory
        if compound_idx is None and len(items) == 1 and (has_nutrition or grams_val is not None):
            compound_idx = 1

        is_explicit_action = bool(
            prefix_m
            or has_nutrition
            or (grams_val is not None and compound_name)
            or re.search(r"^(?:исправить|замени|вес|строка|позиция|в\s+порции|на\s+100)\b", c_clean, re.IGNORECASE)
        )

        if compound_idx is not None or (compound_name and is_explicit_action):
            target_idx = compound_idx
            if target_idx:
                last_target_index = target_idx

            if grams_val is not None and not has_nutrition and not compound_name:
                actions.append(StructuredAction(
                    operation=ActionOperation.UPDATE_WEIGHT,
                    target_index=target_idx,
                    grams=grams_val,
                ))
            elif compound_name or (grams_val is not None and not has_nutrition):
                actions.append(StructuredAction(
                    operation=ActionOperation.REPLACE_ITEM,
                    target_index=target_idx,
                    target_name=compound_name if target_idx is None else None,
                    name=compound_name,
                    grams=grams_val,
                ))

            if has_nutrition:
                actions.append(StructuredAction(
                    operation=ActionOperation.SET_USER_NUTRITION,
                    target_index=target_idx,
                    target_name=compound_name if target_idx is None else None,
                    user_nutrition=UserNutritionOverride(
                        grams=grams_val,
                        calories_kcal=cal_val,
                        protein_g=prot_val,
                        fat_g=fat_val,
                        carbs_g=carbs_val,
                        nutrition_basis=basis,
                    ),
                ))
            continue

    if request_save:
        actions.append(StructuredAction(operation=ActionOperation.REQUEST_SAVE))

    if not actions:
        return None

    return StructuredActionPlan(
        inventory_id=inventory_id,
        inventory_version=inventory_version,
        user_id=user_id,
        actions=actions,
        confidence=1.0,
        requires_clarification=False,
        raw_user_message=raw,
    )


def interpret_user_inventory_edit(
    text: str,
    items: Sequence[Any],
    *,
    user_id: int = 0,
    inventory_id: str = "",
    inventory_version: int = 1,
    llm_caller: Callable[[str], str] | None = None,
) -> StructuredActionPlan:
    """
    Main entry point: Attempts Tier 1 deterministic interpretation.
    Falls back to Tier 2 LLM semantic fallback if Tier 1 yields None and text is non-empty.
    """
    plan = parse_deterministic_action_plan(
        text,
        items,
        user_id=user_id,
        inventory_id=inventory_id,
        inventory_version=inventory_version,
    )
    if plan is not None and (plan.actions or plan.requires_clarification):
        return plan

    caller = llm_caller
    if caller is None:
        caller = _default_sync_llm_caller

    try:
        prompt = _build_llm_prompt(text, items)
        raw_resp = caller(prompt)
        semantic_plan = _parse_llm_json_response(
            raw_resp,
            user_id=user_id,
            inventory_id=inventory_id,
            inventory_version=inventory_version,
            text=text,
        )
        if semantic_plan is not None:
            return semantic_plan
    except Exception as e:
        logger.warning("[ActionInterpreter] Semantic fallback failed: %s", e)

    return plan or StructuredActionPlan(
        inventory_id=inventory_id,
        inventory_version=inventory_version,
        user_id=user_id,
        actions=[],
        confidence=0.0,
        requires_clarification=False,
        raw_user_message=text,
    )


def _build_llm_prompt(text: str, items: Sequence[Any]) -> str:
    items_desc = "\n".join(
        f"Item {idx}: {getattr(itm, 'visible_name', '')} (current grams: {getattr(itm, 'selected_grams', '')})"
        for idx, itm in enumerate(items, start=1)
    )
    return (
        "You are an assistant that translates natural language corrections to food inventory into a structured JSON action plan.\n"
        "Current inventory:\n"
        f"{items_desc}\n\n"
        f"User correction message:\n\"{text}\"\n\n"
        "Output ONLY a valid JSON object matching this schema:\n"
        "{\n"
        '  "actions": [\n'
        "    {\n"
        '      "operation": "REPLACE_ITEM" | "UPDATE_WEIGHT" | "SET_USER_NUTRITION" | "ADD_ITEM" | "REMOVE_ITEM" | "SCALE_PORTION" | "REQUEST_SAVE",\n'
        '      "target_index": int (1-based index) or null,\n'
        '      "target_name": string or null,\n'
        '      "name": string or null,\n'
        '      "grams": float or null,\n'
        '      "calories_kcal": float or null,\n'
        '      "protein_g": float or null,\n'
        '      "fat_g": float or null,\n'
        '      "carbs_g": float or null,\n'
        '      "nutrition_basis": "TOTAL_PORTION" | "PER_100G",\n'
        '      "scale_factor": float or null\n'
        "    }\n"
        "  ],\n"
        '  "confidence": float (0.0 to 1.0),\n'
        '  "requires_clarification": bool,\n'
        '  "clarification_question": string or null\n'
        "}"
    )


def _default_sync_llm_caller(prompt: str) -> str:
    try:
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning
        response = call_llm(
            task="nutrition_interpretation",
            messages=[
                {"role": "system", "content": "You are a precise nutrition inventory action plan parser. Output valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=600,
            timeout=15.0,
        )
        return extract_content_or_reasoning(response)
    except Exception as exc:
        logger.warning("[ActionInterpreter] Default sync LLM call failed: %s", exc)
        return ""


async def _default_async_llm_caller(prompt: str) -> str:
    try:
        from agent.auxiliary_client import safe_async_call_llm, extract_content_or_reasoning
        response = await safe_async_call_llm(
            task="nutrition_interpretation",
            messages=[
                {"role": "system", "content": "You are a precise nutrition inventory action plan parser. Output valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=600,
            timeout=15.0,
        )
        return extract_content_or_reasoning(response)
    except Exception as exc:
        logger.warning("[ActionInterpreter] Default async LLM call failed: %s", exc)
        return ""


def _parse_llm_json_response(
    raw_resp: str,
    *,
    user_id: int,
    inventory_id: str,
    inventory_version: int,
    text: str,
) -> StructuredActionPlan | None:
    if not raw_resp:
        return None

    try:
        clean_resp = re.sub(r"^```(?:json)?\s*", "", raw_resp.strip(), flags=re.MULTILINE)
        clean_resp = re.sub(r"\s*```$", "", clean_resp.strip(), flags=re.MULTILINE)
        data = json.loads(clean_resp)
        if not isinstance(data, dict):
            return None

        raw_actions = data.get("actions")
        actions: list[StructuredAction] = []
        if isinstance(raw_actions, list):
            for raw_act in raw_actions:
                if not isinstance(raw_act, dict):
                    continue
                op_str = raw_act.get("operation")
                try:
                    op = ActionOperation(op_str)
                except (ValueError, TypeError):
                    continue

                user_nutr = None
                if any(k in raw_act and raw_act[k] is not None for k in ("calories_kcal", "protein_g", "fat_g", "carbs_g")):
                    user_nutr = UserNutritionOverride(
                        grams=_to_float(str(raw_act.get("grams")) if raw_act.get("grams") is not None else None),
                        calories_kcal=_to_float(str(raw_act.get("calories_kcal")) if raw_act.get("calories_kcal") is not None else None),
                        protein_g=_to_float(str(raw_act.get("protein_g")) if raw_act.get("protein_g") is not None else None),
                        fat_g=_to_float(str(raw_act.get("fat_g")) if raw_act.get("fat_g") is not None else None),
                        carbs_g=_to_float(str(raw_act.get("carbs_g")) if raw_act.get("carbs_g") is not None else None),
                        nutrition_basis=str(raw_act.get("nutrition_basis") or "TOTAL_PORTION"),
                    )

                raw_target_idx = raw_act.get("target_index")
                target_idx = None
                if raw_target_idx is not None:
                    try:
                        target_idx = int(raw_target_idx)
                    except (ValueError, TypeError):
                        target_idx = None

                actions.append(StructuredAction(
                    operation=op,
                    target_index=target_idx,
                    target_name=str(raw_act.get("target_name")) if raw_act.get("target_name") is not None else None,
                    name=str(raw_act.get("name")) if raw_act.get("name") is not None else None,
                    grams=_to_float(str(raw_act.get("grams")) if raw_act.get("grams") is not None else None),
                    scale_factor=_to_float(str(raw_act.get("scale_factor")) if raw_act.get("scale_factor") is not None else None),
                    user_nutrition=user_nutr,
                ))

        confidence_val = data.get("confidence", 0.9)
        try:
            confidence = float(confidence_val)
        except (ValueError, TypeError):
            confidence = 0.5

        req_clarify = bool(data.get("requires_clarification", False))
        clarify_q = data.get("clarification_question")
        if clarify_q is not None:
            clarify_q = str(clarify_q)

        return StructuredActionPlan(
            inventory_id=inventory_id,
            inventory_version=inventory_version,
            user_id=user_id,
            actions=actions,
            confidence=confidence,
            requires_clarification=req_clarify,
            clarification_question=clarify_q,
            raw_user_message=text,
        )
    except Exception as ex:
        logger.warning("[ActionInterpreter] Failed to parse LLM JSON: %s", ex)
        return None


def _call_llm_interpreter(
    text: str,
    items: Sequence[Any],
    *,
    user_id: int,
    inventory_id: str,
    inventory_version: int,
    llm_caller: Callable[[str], str],
) -> StructuredActionPlan | None:
    prompt = _build_llm_prompt(text, items)
    raw_resp = llm_caller(prompt)
    return _parse_llm_json_response(
        raw_resp,
        user_id=user_id,
        inventory_id=inventory_id,
        inventory_version=inventory_version,
        text=text,
    )


async def interpret_user_inventory_edit_async(
    text: str,
    items: Sequence[Any],
    *,
    user_id: int = 0,
    inventory_id: str = "",
    inventory_version: int = 1,
    llm_caller: Any = None,
) -> StructuredActionPlan:
    # 1. Tier 1: Fast deterministic parser
    plan = parse_deterministic_action_plan(
        text,
        items,
        user_id=user_id,
        inventory_id=inventory_id,
        inventory_version=inventory_version,
    )
    if plan is not None and (plan.actions or plan.requires_clarification):
        return plan

    # 2. Tier 2: LLM semantic fallback (async)
    caller = llm_caller
    prompt = _build_llm_prompt(text, items)
    try:
        if caller is not None:
            import inspect
            if inspect.iscoroutinefunction(caller):
                raw_resp = await caller(prompt)
            else:
                raw_resp = caller(prompt)
        else:
            raw_resp = await _default_async_llm_caller(prompt)

        semantic_plan = _parse_llm_json_response(
            raw_resp,
            user_id=user_id,
            inventory_id=inventory_id,
            inventory_version=inventory_version,
            text=text,
        )
        if semantic_plan is not None:
            return semantic_plan
    except Exception as e:
        logger.warning("[ActionInterpreter] Async semantic fallback failed: %s", e)

    return plan or StructuredActionPlan(
        inventory_id=inventory_id,
        inventory_version=inventory_version,
        user_id=user_id,
        actions=[],
        confidence=0.0,
        requires_clarification=False,
        raw_user_message=text,
    )
