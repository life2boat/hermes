"""Provider-free success contract shared by generation and publication."""

from __future__ import annotations

from typing import cast

from gateway.healbite_inventory_menu_contract import (
    INVENTORY_MENU_RESPONSE_CONTRACT,
    InventoryMenuContractError,
    _strict_decimal,
)
from gateway.healbite_weekly_menu_generation_types import (
    WeeklyMenuGeneratedEntry,
    WeeklyMenuGenerationResponse,
    WeeklyMenuIngredient,
)
from gateway.healbite_weekly_menu_schema import require_monday_week_start, week_dates

WEEKLY_DRAFT_MEAL_SLOTS = ("breakfast", "lunch", "dinner")
WEEKLY_DRAFT_MEAL_COUNT = 21


class WeeklyMenuDraftValidationError(ValueError):
    """Safe contract failure; never includes provider content."""


def parse_weekly_menu_ingredients(payload: object) -> tuple[WeeklyMenuIngredient, ...]:
    """Use the existing inventory/weekly ingredient shape and decimal rules."""
    if not isinstance(payload, (list, tuple)) or not 1 <= len(payload) <= 32:
        raise WeeklyMenuDraftValidationError("ingredients must be non-empty")
    result = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "quantity_value",
            "unit",
        }:
            raise WeeklyMenuDraftValidationError("invalid ingredient shape")
        fields = cast(dict[str, object], item)
        name, unit = fields["name"], fields["unit"]
        if not isinstance(name, str) or not isinstance(unit, str):
            raise WeeklyMenuDraftValidationError("invalid ingredient type")
        name = " ".join(name.split())
        unit = unit.strip().lower()
        if (
            not name
            or len(name) > 200
            or unit
            not in cast(
                list[str], INVENTORY_MENU_RESPONSE_CONTRACT["ingredient"]["unit_values"]
            )
        ):
            raise WeeklyMenuDraftValidationError("invalid ingredient name or unit")
        try:
            quantity = _strict_decimal(
                fields["quantity_value"], label="ingredient quantity"
            )
        except InventoryMenuContractError as exc:
            raise WeeklyMenuDraftValidationError("invalid ingredient quantity") from exc
        result.append(WeeklyMenuIngredient(name, quantity, unit))
    return tuple(result)


def validate_weekly_menu_draft(
    response: WeeklyMenuGenerationResponse, *, week_start: str
) -> None:
    """Require seven canonical days, three unique meals/day and real ingredients.

    Also runs on injected generators and persisted revisions; parser validation
    alone is not an authorization to persist/publish a successful weekly menu.
    """
    expected = {
        (day, slot)
        for day in week_dates(require_monday_week_start(week_start))
        for slot in WEEKLY_DRAFT_MEAL_SLOTS
    }
    if (
        not isinstance(response, WeeklyMenuGenerationResponse)
        or len(response.entries) != WEEKLY_DRAFT_MEAL_COUNT
    ):
        raise WeeklyMenuDraftValidationError("weekly draft requires 21 meals")
    seen = set()
    for entry in response.entries:
        if not isinstance(entry, WeeklyMenuGeneratedEntry):
            raise WeeklyMenuDraftValidationError("invalid meal representation")
        key = (entry.local_date, entry.meal_slot)
        if (
            key not in expected
            or key in seen
            or type(entry.position) is not int
            or entry.position != 1
        ):
            raise WeeklyMenuDraftValidationError("invalid weekly meal structure")
        seen.add(key)
        if (
            not isinstance(entry.title, str)
            or not entry.title.strip()
            or len(entry.title) > 200
        ):
            raise WeeklyMenuDraftValidationError("invalid meal title")
        if not isinstance(entry.ingredients, (list, tuple)) or any(
            not isinstance(item, WeeklyMenuIngredient) for item in entry.ingredients
        ):
            raise WeeklyMenuDraftValidationError("invalid ingredient representation")
        parse_weekly_menu_ingredients([
            {
                "name": item.name,
                "quantity_value": item.quantity_value,
                "unit": item.unit,
            }
            for item in entry.ingredients
        ])
        try:
            _strict_decimal(entry.servings, label="servings")
        except InventoryMenuContractError as exc:
            raise WeeklyMenuDraftValidationError("invalid meal servings") from exc
