from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4

from agent.auxiliary_client import (
    LLMServiceUnavailableError,
    WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
    extract_content_or_reasoning,
    safe_call_llm,
)
from gateway.healbite_fridge_menu_schema import (
    PLANNED_INGREDIENTS_TABLE,
    PLANNED_MEALS_TABLE,
    USER_INVENTORY_TABLE,
    WEEKLY_MENU_PLANS_TABLE,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_weekly_menu_runtime import build_fridge_weekly_menu_prompts

FRIDGE_MENU_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
FRIDGE_MENU_MEAL_TYPES = ("breakfast", "lunch", "dinner")
FRIDGE_MENU_UNITS = frozenset(
    {"g", "kg", "ml", "l", "piece", "package", "unitless", "unknown"}
)


class FridgeMenuContractError(ValueError):
    pass


class FridgeMenuGenerationUnavailableError(RuntimeError):
    pass


class FridgeMenuStorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FridgeMenuIngredient:
    name: str
    quantity: str
    unit: str
    is_in_inventory: bool


@dataclass(frozen=True, slots=True)
class FridgeMenuMeal:
    meal_type: str
    title: str
    ingredients: tuple[FridgeMenuIngredient, ...]


@dataclass(frozen=True, slots=True)
class FridgeMenuDay:
    day: str
    meals: tuple[FridgeMenuMeal, ...]


@dataclass(frozen=True, slots=True)
class FridgeMenuMissingIngredient:
    name: str
    quantity: str
    unit: str


@dataclass(frozen=True, slots=True)
class FridgeMenuPlan:
    days: tuple[FridgeMenuDay, ...]
    missing_ingredients_to_buy: tuple[FridgeMenuMissingIngredient, ...]


def _normalized_text(value: object, *, label: str, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise FridgeMenuContractError(f"{label} must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise FridgeMenuContractError(f"invalid {label}")
    return normalized


def _normalized_quantity(value: object, *, label: str) -> str:
    if isinstance(value, bool):
        raise FridgeMenuContractError(f"invalid {label}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FridgeMenuContractError(f"invalid {label}") from exc
    if not parsed.is_finite() or parsed <= 0 or parsed > Decimal("1000000"):
        raise FridgeMenuContractError(f"invalid {label}")
    normalized = format(parsed.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if not normalized or len(normalized) > 32:
        raise FridgeMenuContractError(f"invalid {label}")
    return normalized


def _normalized_unit(value: object) -> str:
    if not isinstance(value, str):
        raise FridgeMenuContractError("unit must be text")
    unit = value.strip().casefold()
    if unit not in FRIDGE_MENU_UNITS:
        raise FridgeMenuContractError("invalid unit")
    return unit


def _inventory_keys(values: Sequence[str] | None) -> set[str] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise FridgeMenuContractError("inventory must be a sequence")
    return {
        _normalized_text(value, label="inventory item").casefold()
        for value in values
    }


def parse_fridge_menu_response(
    payload: object,
    *,
    inventory_ingredients: Sequence[str] | None = None,
) -> FridgeMenuPlan:
    """Validate the exact fridge-menu LLM response contract."""

    if not isinstance(payload, dict) or set(payload) != {
        "days",
        "missing_ingredients_to_buy",
    }:
        raise FridgeMenuContractError("invalid top-level shape")
    days_raw = payload["days"]
    missing_raw = payload["missing_ingredients_to_buy"]
    if not isinstance(days_raw, list) or len(days_raw) != len(FRIDGE_MENU_WEEKDAYS):
        raise FridgeMenuContractError("invalid days")
    if not isinstance(missing_raw, list) or len(missing_raw) > 200:
        raise FridgeMenuContractError("invalid shopping list")

    known_inventory = _inventory_keys(inventory_ingredients)
    days: list[FridgeMenuDay] = []
    false_inventory_names: set[str] = set()
    true_inventory_names: set[str] = set()
    for expected_day, day_raw in zip(FRIDGE_MENU_WEEKDAYS, days_raw, strict=True):
        if not isinstance(day_raw, dict) or set(day_raw) != {"day", "meals"}:
            raise FridgeMenuContractError("invalid day shape")
        day = _normalized_text(day_raw["day"], label="day").casefold()
        if day != expected_day:
            raise FridgeMenuContractError("days must be canonical and ordered")
        meals_raw = day_raw["meals"]
        if not isinstance(meals_raw, list) or len(meals_raw) != len(
            FRIDGE_MENU_MEAL_TYPES
        ):
            raise FridgeMenuContractError("invalid meals")
        meals: list[FridgeMenuMeal] = []
        for expected_meal_type, meal_raw in zip(
            FRIDGE_MENU_MEAL_TYPES,
            meals_raw,
            strict=True,
        ):
            if not isinstance(meal_raw, dict) or set(meal_raw) != {
                "meal_type",
                "title",
                "ingredients",
            }:
                raise FridgeMenuContractError("invalid meal shape")
            meal_type = _normalized_text(
                meal_raw["meal_type"], label="meal type"
            ).casefold()
            if meal_type != expected_meal_type:
                raise FridgeMenuContractError("meal types must be canonical and ordered")
            title = _normalized_text(meal_raw["title"], label="meal title")
            ingredients_raw = meal_raw["ingredients"]
            if (
                not isinstance(ingredients_raw, list)
                or not ingredients_raw
                or len(ingredients_raw) > 32
            ):
                raise FridgeMenuContractError("invalid meal ingredients")
            ingredients: list[FridgeMenuIngredient] = []
            meal_names: set[str] = set()
            for ingredient_raw in ingredients_raw:
                if not isinstance(ingredient_raw, dict) or set(ingredient_raw) != {
                    "name",
                    "quantity",
                    "unit",
                    "is_in_inventory",
                }:
                    raise FridgeMenuContractError("invalid ingredient shape")
                name = _normalized_text(
                    ingredient_raw["name"], label="ingredient name"
                )
                name_key = name.casefold()
                if name_key in meal_names:
                    raise FridgeMenuContractError("duplicate meal ingredient")
                meal_names.add(name_key)
                is_in_inventory = ingredient_raw["is_in_inventory"]
                if not isinstance(is_in_inventory, bool):
                    raise FridgeMenuContractError("inventory flag must be boolean")
                if known_inventory is not None and is_in_inventory != (
                    name_key in known_inventory
                ):
                    raise FridgeMenuContractError("inventory flag contradicts input")
                if is_in_inventory:
                    true_inventory_names.add(name_key)
                else:
                    false_inventory_names.add(name_key)
                ingredients.append(
                    FridgeMenuIngredient(
                        name=name,
                        quantity=_normalized_quantity(
                            ingredient_raw["quantity"], label="ingredient quantity"
                        ),
                        unit=_normalized_unit(ingredient_raw["unit"]),
                        is_in_inventory=is_in_inventory,
                    )
                )
            meals.append(
                FridgeMenuMeal(
                    meal_type=meal_type,
                    title=title,
                    ingredients=tuple(ingredients),
                )
            )
        days.append(FridgeMenuDay(day=day, meals=tuple(meals)))

    if false_inventory_names & true_inventory_names:
        raise FridgeMenuContractError("ingredient inventory flags conflict")
    missing: list[FridgeMenuMissingIngredient] = []
    missing_names: set[str] = set()
    for item_raw in missing_raw:
        if not isinstance(item_raw, dict) or set(item_raw) != {
            "name",
            "quantity",
            "unit",
        }:
            raise FridgeMenuContractError("invalid shopping item shape")
        name = _normalized_text(item_raw["name"], label="shopping item name")
        name_key = name.casefold()
        if name_key in missing_names:
            raise FridgeMenuContractError("duplicate shopping item")
        missing_names.add(name_key)
        missing.append(
            FridgeMenuMissingIngredient(
                name=name,
                quantity=_normalized_quantity(
                    item_raw["quantity"], label="shopping item quantity"
                ),
                unit=_normalized_unit(item_raw["unit"]),
            )
        )
    if missing_names != false_inventory_names:
        raise FridgeMenuContractError("shopping list does not match missing ingredients")
    return FridgeMenuPlan(
        days=tuple(days),
        missing_ingredients_to_buy=tuple(missing),
    )


class FridgeMenuLLMGenerator:
    def __init__(
        self,
        *,
        call_llm_fn: Callable[..., object] = safe_call_llm,
        timeout: float = 45.0,
    ) -> None:
        self._call_llm_fn = call_llm_fn
        self._timeout = float(timeout)

    def generate(
        self,
        inventory_ingredients: Sequence[str],
        *,
        week_start: str,
        dietary_restrictions: Sequence[str] = (),
        locale: str = "ru-RU",
    ) -> FridgeMenuPlan:
        prompts = build_fridge_weekly_menu_prompts(
            inventory_ingredients,
            week_start=week_start,
            dietary_restrictions=dietary_restrictions,
            locale=locale,
        )
        try:
            response = self._call_llm_fn(
                task="weekly_menu_generation",
                messages=[
                    {"role": "system", "content": prompts.system_prompt},
                    {"role": "user", "content": prompts.user_prompt},
                ],
                temperature=0,
                max_tokens=8000,
                timeout=self._timeout,
                call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
            )
            content = extract_content_or_reasoning(response)
            if not content:
                raise FridgeMenuContractError("empty generator response")
            try:
                payload = json.loads(content, parse_float=Decimal)
            except json.JSONDecodeError as exc:
                raise FridgeMenuContractError("malformed generator JSON") from exc
            return parse_fridge_menu_response(
                payload,
                inventory_ingredients=inventory_ingredients,
            )
        except FridgeMenuContractError:
            raise
        except LLMServiceUnavailableError as exc:
            raise FridgeMenuGenerationUnavailableError(
                "fridge menu generator unavailable"
            ) from exc
        except Exception as exc:
            raise FridgeMenuGenerationUnavailableError(
                "fridge menu generator unavailable"
            ) from exc


class FridgeMenuStore:
    """Persist explicitly saved fridge-menu previews; never migrates schema."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = resolve_healbite_db_path(db_path)

    def save(
        self,
        *,
        user_id: int,
        inventory_ingredients: Sequence[str],
        source_type: str,
        week_start: str,
        plan: FridgeMenuPlan,
    ) -> str:
        if isinstance(user_id, bool) or int(user_id) <= 0:
            raise FridgeMenuStorageError("invalid user")
        if source_type not in {"text", "vision"}:
            raise FridgeMenuStorageError("invalid inventory source")
        try:
            with sqlite3.connect(self._db_path) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    f"DELETE FROM {USER_INVENTORY_TABLE} WHERE user_id = ?",
                    (int(user_id),),
                )
                for ingredient_name in inventory_ingredients:
                    display_name = _normalized_text(
                        ingredient_name, label="inventory item"
                    )
                    normalized_name = display_name.casefold()
                    connection.execute(
                        f"""
                        INSERT INTO {USER_INVENTORY_TABLE}
                            (id, user_id, normalized_name, display_name,
                             quantity_value, quantity_unit, source_type)
                        VALUES (?, ?, ?, ?, NULL, 'unknown', ?)
                        ON CONFLICT(user_id, normalized_name) DO UPDATE SET
                            display_name = excluded.display_name,
                            source_type = excluded.source_type,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            str(uuid4()),
                            int(user_id),
                            normalized_name,
                            display_name,
                            source_type,
                        ),
                    )
                existing = connection.execute(
                    f"""
                    SELECT id FROM {WEEKLY_MENU_PLANS_TABLE}
                    WHERE user_id = ? AND week_start = ?
                    """,
                    (int(user_id), week_start),
                ).fetchone()
                plan_id = str(existing[0]) if existing is not None else str(uuid4())
                if existing is None:
                    connection.execute(
                        f"""
                        INSERT INTO {WEEKLY_MENU_PLANS_TABLE}
                            (id, user_id, week_start, status)
                        VALUES (?, ?, ?, 'generated')
                        """,
                        (plan_id, int(user_id), week_start),
                    )
                else:
                    connection.execute(
                        f"""
                        UPDATE {WEEKLY_MENU_PLANS_TABLE}
                        SET status = 'generated', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND user_id = ?
                        """,
                        (plan_id, int(user_id)),
                    )
                    connection.execute(
                        f"DELETE FROM {PLANNED_MEALS_TABLE} WHERE plan_id = ?",
                        (plan_id,),
                    )
                for day in plan.days:
                    for meal in day.meals:
                        meal_id = str(uuid4())
                        connection.execute(
                            f"""
                            INSERT INTO {PLANNED_MEALS_TABLE}
                                (id, plan_id, user_id, day_of_week, meal_type, title)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                meal_id,
                                plan_id,
                                int(user_id),
                                day.day,
                                meal.meal_type,
                                meal.title,
                            ),
                        )
                        for ingredient in meal.ingredients:
                            connection.execute(
                                f"""
                                INSERT INTO {PLANNED_INGREDIENTS_TABLE}
                                    (id, meal_id, user_id, normalized_name,
                                     display_name, quantity_value, quantity_unit,
                                     is_in_inventory)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    str(uuid4()),
                                    meal_id,
                                    int(user_id),
                                    ingredient.name.casefold(),
                                    ingredient.name,
                                    ingredient.quantity,
                                    ingredient.unit,
                                    int(ingredient.is_in_inventory),
                                ),
                            )
                return plan_id
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise FridgeMenuStorageError("fridge menu storage unavailable") from exc
