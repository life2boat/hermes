from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from agent.auxiliary_client import (
    WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
    extract_content_or_reasoning,
    safe_call_llm,
)
from gateway.healbite_recipe_catalog_domain import MealType
from gateway.healbite_recipe_retrieval import RecipeCandidate

logger = logging.getLogger(__name__)

MAX_PLANNER_REPAIR_ATTEMPTS = 2


class PlannerGroundingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PlannedMealSlot:
    date: str
    slot: MealType
    recipe_id: str
    recipe_version: int
    target_servings: Decimal


@dataclass(frozen=True, slots=True)
class RecipeGroundedPlan:
    week_start: str
    meals: tuple[PlannedMealSlot, ...]


PLANNER_SYSTEM_PROMPT = """Вы — строгий планировщик меню HealBite.
Ваша единственная задача: составить недельный план питания (7 дней × 3 приема пищи: breakfast, lunch, dinner)
ИЗ ПРЕДОСТАВЛЕННОГО СПИСКА ПРОВЕРЕННЫХ РЕЦЕПТОВ (allowed_candidates).

КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:
1. Использовать recipe_id, которых нет в списке кандидатов.
2. Придумывать новые рецепты, ингредиенты или шаги.
3. Повторять один и тот же recipe_id более одного раза в течение недели.
4. Добавлять в вывод названия блюд, авторов или рецептурные тексты.

ФОРМАТ ВЫВОДА — СТРОГО JSON:
{
  "week_start": "YYYY-MM-DD",
  "meals": [
    {
      "date": "YYYY-MM-DD",
      "slot": "breakfast" | "lunch" | "dinner",
      "recipe_id": "<ID из списка кандидатов>",
      "recipe_version": 1,
      "servings": 2
    },
    ... (ровно 21 элемент)
  ]
}
"""


class RecipeGroundedWeeklyPlanner:
    def __init__(
        self,
        *,
        llm_caller: Callable[..., Any] | None = None,
    ) -> None:
        self._llm_caller = llm_caller or safe_call_llm

    def plan(
        self,
        *,
        week_start: str,
        dates: Sequence[str],
        slot_candidates: Mapping[MealType, Sequence[RecipeCandidate]],
        target_servings: Decimal = Decimal("2"),
    ) -> RecipeGroundedPlan:
        # Build allowed candidate registry
        all_candidates: dict[str, RecipeCandidate] = {}
        candidate_catalog_summary = []
        for slot, cands in slot_candidates.items():
            for c in cands:
                all_candidates[c.recipe_id] = c
                candidate_catalog_summary.append({
                    "recipe_id": c.recipe_id,
                    "recipe_version": c.recipe_version,
                    "slot": slot.value,
                    "author_id": c.author_id,
                    "title": c.title,
                    "servings": str(c.servings),
                    "overlap_count": c.ingredient_overlap_count,
                })

        allowed_ids = set(all_candidates.keys())

        user_prompt = json.dumps(
            {
                "week_start": week_start,
                "dates": list(dates),
                "target_servings": str(target_servings),
                "allowed_candidates": candidate_catalog_summary,
            },
            ensure_ascii=False,
            indent=2,
        )

        repair_attempts = 0
        last_error = ""

        while repair_attempts <= MAX_PLANNER_REPAIR_ATTEMPTS:
            prompt_content = user_prompt
            if repair_attempts > 0:
                prompt_content += (
                    f"\n\nВНИМАНИЕ: Предыдущий ответ содержал ошибку: {last_error}. "
                    f"Исправьте ответ и верните строго валидный JSON из ровно 21 приема пищи, "
                    f"используя ТОЛЬКО допустимые recipe_id."
                )

            messages = [
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt_content},
            ]

            try:
                response = self._llm_caller(
                    messages=messages,
                    call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
                )
                if isinstance(response, str):
                    raw_text = response
                elif isinstance(response, dict) and "choices" in response:
                    raw_text = str(response["choices"][0].get("message", {}).get("content", ""))
                else:
                    raw_text = extract_content_or_reasoning(response)
                plan = self._parse_and_validate_response(
                    raw_text,
                    week_start=week_start,
                    dates=dates,
                    allowed_ids=allowed_ids,
                    all_candidates=all_candidates,
                    default_servings=target_servings,
                )
                return plan
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "[RecipePlanner] Attempt %d failed: %s",
                    repair_attempts + 1,
                    last_error,
                )
                repair_attempts += 1

        raise PlannerGroundingError(f"PLANNER_GROUNDING_FAILED: {last_error}")

    @staticmethod
    def _parse_and_validate_response(
        raw_text: str,
        *,
        week_start: str,
        dates: Sequence[str],
        allowed_ids: set[str],
        all_candidates: dict[str, RecipeCandidate],
        default_servings: Decimal,
    ) -> RecipeGroundedPlan:
        cleaned = raw_text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise PlannerGroundingError(f"Model output is not valid JSON: {exc}") from exc

        meals_raw = data.get("meals")
        if not isinstance(meals_raw, list):
            raise PlannerGroundingError("Model output missing 'meals' array")

        if len(meals_raw) != 21:
            raise PlannerGroundingError(f"Model returned {len(meals_raw)} meals (expected exactly 21)")

        parsed_meals: list[PlannedMealSlot] = []
        seen_slots: set[tuple[str, str]] = set()
        seen_recipes: set[str] = set()

        for item in meals_raw:
            if not isinstance(item, dict):
                raise PlannerGroundingError("Meal item must be a JSON object")

            date_val = str(item.get("date", "")).strip()
            if date_val not in dates:
                raise PlannerGroundingError(f"Invalid date '{date_val}' in plan")

            slot_raw = str(item.get("slot", "")).strip().lower()
            try:
                slot_enum = MealType(slot_raw)
            except ValueError:
                raise PlannerGroundingError(f"Invalid meal slot '{slot_raw}'")

            slot_key = (date_val, slot_raw)
            if slot_key in seen_slots:
                raise PlannerGroundingError(f"Duplicate meal slot for date {date_val} and slot {slot_raw}")
            seen_slots.add(slot_key)

            recipe_id = str(item.get("recipe_id", "")).strip()
            if not recipe_id:
                raise PlannerGroundingError("Missing recipe_id in meal item")

            if recipe_id not in allowed_ids:
                raise PlannerGroundingError(f"Unknown or unallowed recipe_id '{recipe_id}'")

            if recipe_id in seen_recipes:
                raise PlannerGroundingError(f"Duplicate recipe_id '{recipe_id}' planned in same week")
            seen_recipes.add(recipe_id)

            cand = all_candidates[recipe_id]
            servings_raw = item.get("servings")
            servings = Decimal(str(servings_raw)) if servings_raw is not None else default_servings

            parsed_meals.append(
                PlannedMealSlot(
                    date=date_val,
                    slot=slot_enum,
                    recipe_id=recipe_id,
                    recipe_version=cand.recipe_version,
                    target_servings=servings,
                )
            )

        return RecipeGroundedPlan(
            week_start=week_start,
            meals=tuple(parsed_meals),
        )
