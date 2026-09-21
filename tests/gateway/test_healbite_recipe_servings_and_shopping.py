from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import pytest

from gateway.healbite_recipe_catalog_domain import MealType, Recipe, RecipeIngredient
from gateway.healbite_recipe_grounding_validator import ValidatedMealEntry
from gateway.healbite_recipe_servings_shopping import (
    aggregate_recipe_ingredients,
    build_recipe_grounded_shopping_idempotency_key,
    convert_unit_quantity,
    derive_shopping_list_from_recipes,
    scale_quantity,
)


def test_scale_quantity_deterministic() -> None:
    # 2 servings -> 4 servings (factor 2)
    assert scale_quantity(Decimal("150"), Decimal("2"), Decimal("4")) == Decimal("300")

    # 4 servings -> 2 servings (factor 0.5)
    assert scale_quantity(Decimal("200"), Decimal("4"), Decimal("2")) == Decimal("100")

    # Fractional scaling
    scaled = scale_quantity(Decimal("100"), Decimal("3"), Decimal("2"))
    assert scaled == Decimal("66.67")

    # None quantity remains None
    assert scale_quantity(None, Decimal("2"), Decimal("4")) is None


def test_convert_unit_quantity() -> None:
    assert convert_unit_quantity(Decimal("1.5"), "kg", "g") == Decimal("1500")
    assert convert_unit_quantity(Decimal("500"), "g", "kg") == Decimal("0.5")
    assert convert_unit_quantity(Decimal("1"), "l", "ml") == Decimal("1000")
    assert convert_unit_quantity(Decimal("250"), "ml", "l") == Decimal("0.25")

    # Incompatible unit conversions return None without inventing conversions
    assert convert_unit_quantity(Decimal("2"), "piece", "g") is None
    assert convert_unit_quantity(Decimal("100"), "g", "ml") is None


def test_derive_shopping_list_with_inventory_subtraction() -> None:
    # Create mock validated meal with ingredients
    ing1 = RecipeIngredient(
        id="ing:1",
        recipe_id="rcp:1",
        ingredient_id="CARROT",
        display_name="Морковь",
        quantity=Decimal("300"),
        unit="g",
        position=1,
    )
    ing2 = RecipeIngredient(
        id="ing:2",
        recipe_id="rcp:1",
        ingredient_id="POTATO",
        display_name="Картофель",
        quantity=Decimal("500"),
        unit="g",
        position=2,
    )
    ing3 = RecipeIngredient(
        id="ing:3",
        recipe_id="rcp:1",
        ingredient_id="SALT",
        display_name="Соль",
        quantity=Decimal("10"),
        unit="g",
        position=3,
    )

    from unittest.mock import MagicMock
    mock_recipe = MagicMock()
    mock_recipe.recipe_id = "rcp:1"
    mock_recipe.servings = Decimal("2")
    mock_recipe.ingredients = (ing1, ing2, ing3)

    meal = ValidatedMealEntry(
        date="2026-09-21",
        slot=MealType.LUNCH,
        position=1,
        recipe=mock_recipe,
        target_servings=Decimal("2"),  # 1x scale
        author_display_name="Тест",
        source_title="Тестовый сборник",
        source_locator=None,
    )

    # Confirmed inventory:
    # CARROT: 100g available (needs 300g => remaining 200g)
    # POTATO: 1 kg available (needs 500g => remaining 0g, fulfilled!)
    # SALT: not in inventory (needs 10g => remaining 10g)
    confirmed_inv = {
        "CARROT": (Decimal("100"), "g"),
        "POTATO": (Decimal("1"), "kg"),
    }

    shopping_items = derive_shopping_list_from_recipes([meal], confirmed_inventory=confirmed_inv)

    items_by_id = {item.ingredient_id: item for item in shopping_items}

    # CARROT should need 200g
    assert "CARROT" in items_by_id
    assert items_by_id["CARROT"].required_quantity == Decimal("300")
    assert items_by_id["CARROT"].inventory_quantity_used == Decimal("100")
    assert items_by_id["CARROT"].remaining_quantity == Decimal("200")
    assert items_by_id["CARROT"].origin == "recipe_grounded"

    # POTATO is fully covered by 1kg inventory, so not needed in shopping list!
    assert "POTATO" not in items_by_id

    # SALT has no inventory, so all 10g is needed
    assert "SALT" in items_by_id
    assert items_by_id["SALT"].remaining_quantity == Decimal("10")
    assert items_by_id["SALT"].inventory_quantity_used == Decimal("0")


def test_shopping_idempotency_key_deterministic() -> None:
    k1 = build_recipe_grounded_shopping_idempotency_key("hh-1", "rev-1", "build-v1")
    k2 = build_recipe_grounded_shopping_idempotency_key("hh-1", "rev-1", "build-v1")
    k3 = build_recipe_grounded_shopping_idempotency_key("hh-1", "rev-2", "build-v1")

    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64
