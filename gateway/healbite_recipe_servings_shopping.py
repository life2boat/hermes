from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from gateway.healbite_recipe_catalog_domain import RecipeIngredient
from gateway.healbite_recipe_grounding_validator import ValidatedMealEntry


@dataclass(frozen=True, slots=True)
class ScaledIngredientRequirement:
    ingredient_id: str
    display_name: str
    total_required: Decimal | None
    unit: str
    contributing_recipe_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DerivedShoppingItem:
    ingredient_id: str
    display_name: str
    required_quantity: Decimal | None
    inventory_quantity_used: Decimal
    remaining_quantity: Decimal | None
    unit: str
    contributing_recipe_ids: tuple[str, ...]
    origin: str = "recipe_grounded"


def scale_quantity(
    original_quantity: Decimal | None,
    source_servings: Decimal,
    target_servings: Decimal,
) -> Decimal | None:
    """Deterministic serving scaling: scaled = original * target / source."""
    if original_quantity is None:
        return None
    if source_servings <= Decimal("0"):
        return original_quantity
    scale_factor = target_servings / source_servings
    scaled = original_quantity * scale_factor
    # Round to 2 decimal places if fractional
    return scaled.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()


def convert_unit_quantity(
    quantity: Decimal,
    from_unit: str,
    to_unit: str,
) -> Decimal | None:
    """Convert compatible metric units deterministically. None if incompatible."""
    if from_unit == to_unit:
        return quantity
    # Weight
    if from_unit == "kg" and to_unit == "g":
        return quantity * Decimal("1000")
    if from_unit == "g" and to_unit == "kg":
        return quantity / Decimal("1000")
    # Volume
    if from_unit == "l" and to_unit == "ml":
        return quantity * Decimal("1000")
    if from_unit == "ml" and to_unit == "l":
        return quantity / Decimal("1000")
    return None


def aggregate_recipe_ingredients(
    meals: Sequence[ValidatedMealEntry],
) -> list[ScaledIngredientRequirement]:
    """Aggregate scaled ingredients across all 21 meals in the weekly plan."""
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}

    for meal in meals:
        source_servings = meal.recipe.servings
        target_servings = meal.target_servings
        for ing in meal.recipe.ingredients:
            scaled = scale_quantity(ing.quantity, source_servings, target_servings)
            key = (ing.ingredient_id, ing.unit)

            if key not in aggregated:
                aggregated[key] = {
                    "ingredient_id": ing.ingredient_id,
                    "display_name": ing.display_name,
                    "total_quantity": scaled,
                    "unit": ing.unit,
                    "recipes": {meal.recipe.recipe_id},
                }
            else:
                entry = aggregated[key]
                entry["recipes"].add(meal.recipe.recipe_id)
                if scaled is not None and entry["total_quantity"] is not None:
                    entry["total_quantity"] += scaled
                elif scaled is not None and entry["total_quantity"] is None:
                    entry["total_quantity"] = scaled

    results = []
    for entry in aggregated.values():
        results.append(
            ScaledIngredientRequirement(
                ingredient_id=entry["ingredient_id"],
                display_name=entry["display_name"],
                total_required=entry["total_quantity"],
                unit=entry["unit"],
                contributing_recipe_ids=tuple(sorted(entry["recipes"])),
            )
        )
    return results


def derive_shopping_list_from_recipes(
    meals: Sequence[ValidatedMealEntry],
    confirmed_inventory: Mapping[str, tuple[Decimal, str]] | None = None,
) -> list[DerivedShoppingItem]:
    """
    Subtracts confirmed inventory from aggregated recipe requirements deterministically.
    Conservative on incompatible or unknown units (no invented conversion).
    """
    aggregated = aggregate_recipe_ingredients(meals)
    inv = confirmed_inventory or {}
    shopping_items: list[DerivedShoppingItem] = []

    for req in aggregated:
        ing_id = req.ingredient_id
        required = req.total_required
        unit = req.unit

        if ing_id in inv and required is not None and required > Decimal("0"):
            avail_qty, avail_unit = inv[ing_id]
            # Convert available quantity to required unit if compatible
            converted_avail = convert_unit_quantity(avail_qty, avail_unit, unit)
            if converted_avail is not None:
                used = min(converted_avail, required)
                remaining = max(Decimal("0"), required - converted_avail)
                if remaining > Decimal("0"):
                    shopping_items.append(
                        DerivedShoppingItem(
                            ingredient_id=ing_id,
                            display_name=req.display_name,
                            required_quantity=required,
                            inventory_quantity_used=used,
                            remaining_quantity=remaining,
                            unit=unit,
                            contributing_recipe_ids=req.contributing_recipe_ids,
                        )
                    )
                continue

        # Incompatible unit, missing from inventory, or unknown quantity
        shopping_items.append(
            DerivedShoppingItem(
                ingredient_id=ing_id,
                display_name=req.display_name,
                required_quantity=required,
                inventory_quantity_used=Decimal("0"),
                remaining_quantity=required,
                unit=unit,
                contributing_recipe_ids=req.contributing_recipe_ids,
            )
        )

    return shopping_items


def build_recipe_grounded_shopping_idempotency_key(
    household_id: str,
    weekly_menu_revision_id: str,
    catalog_build_id: str,
) -> str:
    raw = f"{household_id}:{weekly_menu_revision_id}:{catalog_build_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
