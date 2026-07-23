from __future__ import annotations

from decimal import Decimal, InvalidOperation

from gateway.healbite_weekly_menu_generation_types import (
    WeeklyMenuGeneratedEntry,
    WeeklyMenuGenerationRequest,
    WeeklyMenuGenerationResponse,
    WeeklyMenuIngredient,
    WeeklyMenuMacros,
)


class InventoryMenuContractError(ValueError):
    pass


_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_INVENTORY_UNITS = {"g", "kg", "ml", "l", "piece", "package", "unitless"}


def _strict_decimal(value: object, *, label: str, allow_zero: bool = False) -> str:
    if isinstance(value, bool):
        raise InventoryMenuContractError(f"{label} is invalid")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InventoryMenuContractError(f"{label} is invalid") from exc
    if not decimal_value.is_finite() or decimal_value < 0 or (not allow_zero and decimal_value == 0):
        raise InventoryMenuContractError(f"{label} is invalid")
    canonical = format(decimal_value.normalize(), "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if not canonical or len(canonical) > 32:
        raise InventoryMenuContractError(f"{label} is invalid")
    return canonical


def _restriction_terms(request: WeeklyMenuGenerationRequest) -> tuple[str, ...]:
    terms: set[str] = set()
    for note in request.household_dietary_notes:
        for token in str(note).casefold().replace(";", ",").split(","):
            normalized = " ".join(token.split())
            if len(normalized) >= 3:
                terms.add(normalized)
    return tuple(sorted(terms))


def parse_inventory_menu_response(
    payload: object,
    *,
    request: WeeklyMenuGenerationRequest,
) -> WeeklyMenuGenerationResponse:
    if not request.inventory_only or request.inventory_snapshot_id is None:
        raise InventoryMenuContractError("confirmed inventory is required")
    if not isinstance(payload, dict) or set(payload) != {"days"}:
        raise InventoryMenuContractError("payload shape is invalid")
    days = payload.get("days")
    if not isinstance(days, list) or len(days) != len(request.dates) or len(days) != len(_WEEKDAY_NAMES):
        raise InventoryMenuContractError("days are invalid")
    allowed_slots = set(request.allowed_meal_slots)
    restrictions = _restriction_terms(request)
    entries: list[WeeklyMenuGeneratedEntry] = []
    seen_days: set[str] = set()
    for day in days:
        if not isinstance(day, dict) or set(day) != {"day", "meals"}:
            raise InventoryMenuContractError("day shape is invalid")
        day_name = str(day.get("day") or "").strip().casefold()
        if day_name not in _WEEKDAY_NAMES or day_name in seen_days:
            raise InventoryMenuContractError("day is invalid")
        seen_days.add(day_name)
        meals = day.get("meals")
        if not isinstance(meals, list) or len(meals) != len(allowed_slots):
            raise InventoryMenuContractError("meals are invalid")
        seen_slots: set[str] = set()
        for meal in meals:
            required = {
                "meal_type",
                "title",
                "instructions",
                "servings",
                "estimated_calories_per_serving",
                "macros_per_serving",
                "ingredients",
            }
            if not isinstance(meal, dict) or set(meal) != required:
                raise InventoryMenuContractError("meal shape is invalid")
            meal_type = str(meal["meal_type"] or "").strip()
            if meal_type not in allowed_slots or meal_type in seen_slots:
                raise InventoryMenuContractError("meal type is invalid")
            seen_slots.add(meal_type)
            title = " ".join(str(meal["title"] or "").split())
            if not title or len(title) > 200:
                raise InventoryMenuContractError("meal title is invalid")
            instructions_raw = meal["instructions"]
            if not isinstance(instructions_raw, list) or not instructions_raw or len(instructions_raw) > 12:
                raise InventoryMenuContractError("meal instructions are invalid")
            instructions = tuple(" ".join(str(item).split()) for item in instructions_raw)
            if any(not item or len(item) > 500 for item in instructions):
                raise InventoryMenuContractError("meal instructions are invalid")
            if isinstance(meal["servings"], bool):
                raise InventoryMenuContractError("servings are invalid")
            try:
                servings = int(meal["servings"])
            except (TypeError, ValueError) as exc:
                raise InventoryMenuContractError("servings are invalid") from exc
            if servings <= 0 or servings > 100:
                raise InventoryMenuContractError("servings are invalid")
            if isinstance(meal["estimated_calories_per_serving"], bool):
                raise InventoryMenuContractError("calories are invalid")
            try:
                calories = int(meal["estimated_calories_per_serving"])
            except (TypeError, ValueError) as exc:
                raise InventoryMenuContractError("calories are invalid") from exc
            if calories <= 0 or calories > 5000:
                raise InventoryMenuContractError("calories are invalid")
            macros = meal["macros_per_serving"]
            if not isinstance(macros, dict) or set(macros) != {"protein_g", "carbs_g", "fat_g"}:
                raise InventoryMenuContractError("macros are invalid")
            macros_value = WeeklyMenuMacros(
                protein_g=_strict_decimal(macros["protein_g"], label="protein_g", allow_zero=True),
                carbs_g=_strict_decimal(macros["carbs_g"], label="carbs_g", allow_zero=True),
                fat_g=_strict_decimal(macros["fat_g"], label="fat_g", allow_zero=True),
            )
            ingredients_raw = meal["ingredients"]
            if not isinstance(ingredients_raw, list) or not ingredients_raw or len(ingredients_raw) > 32:
                raise InventoryMenuContractError("ingredients are invalid")
            ingredients: list[WeeklyMenuIngredient] = []
            for ingredient in ingredients_raw:
                if not isinstance(ingredient, dict) or set(ingredient) != {"name", "quantity_value", "unit"}:
                    raise InventoryMenuContractError("ingredient shape is invalid")
                name = " ".join(str(ingredient["name"] or "").split())
                normalized_name = name.casefold()
                if not name or len(name) > 200 or any(term in normalized_name for term in restrictions):
                    raise InventoryMenuContractError("ingredient is forbidden")
                unit = str(ingredient["unit"] or "").strip().casefold()
                if unit not in _INVENTORY_UNITS:
                    raise InventoryMenuContractError("ingredient unit is invalid")
                ingredients.append(
                    WeeklyMenuIngredient(
                        name=name,
                        quantity_value=_strict_decimal(ingredient["quantity_value"], label="ingredient quantity"),
                        unit=unit,
                    )
                )
            date_index = _WEEKDAY_NAMES.index(day_name)
            entries.append(
                WeeklyMenuGeneratedEntry(
                    local_date=request.dates[date_index],
                    meal_slot=meal_type,
                    position=1,
                    title=title,
                    servings=str(servings),
                    instructions=instructions,
                    estimated_calories_per_serving=calories,
                    macros_per_serving=macros_value,
                    ingredients=tuple(ingredients),
                )
            )
    if seen_days != set(_WEEKDAY_NAMES) or len(entries) != request.max_entries:
        raise InventoryMenuContractError("entry count is invalid")
    return WeeklyMenuGenerationResponse(entries=tuple(entries))
