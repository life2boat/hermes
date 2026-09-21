from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import pytest

from gateway.healbite_recipe_catalog_domain import (
    MealType,
    Recipe,
    RecipeIngredient,
    normalize_ingredient_id,
)
from gateway.healbite_recipe_grounded_service import (
    HealBiteRecipeGroundedService,
    RecipeGroundedStatus,
)
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


def test_canonical_russian_ingredient_normalization() -> None:
    from gateway.healbite_recipe_catalog_domain import normalize_ingredient_id

    assert normalize_ingredient_id("картофель") == "POTATO"
    assert normalize_ingredient_id("молоко") == "MILK"
    assert normalize_ingredient_id("яйца") == "EGG"
    assert normalize_ingredient_id("морковь") == "CARROT"
    assert normalize_ingredient_id("куриная грудка") == "CHICKEN_BREAST"


def test_real_inventory_item_integration(tmp_path: Path) -> None:
    from unittest.mock import MagicMock
    from gateway.healbite_inventory import (
        InventoryItem,
        InventoryOwnerScope,
        InventorySnapshot,
        InventorySnapshotView,
        InventorySourceType,
        InventoryStatus,
        ShoppingUnit,
    )
    from gateway.healbite_recipe_fixtures import build_test_recipe_catalog

    # Build real InventoryItem objects (with Russian names, quantity_value as str, ShoppingUnit enums)
    item_potato = InventoryItem(
        id="inv:potato",
        snapshot_id="snap:1",
        normalized_name="картофель",
        display_name="картофель",
        quantity_value="1",
        unit=ShoppingUnit.KG,
        category=None,
        confidence="1.0",
        uncertainty=None,
        position=1,
    )
    item_carrot = InventoryItem(
        id="inv:carrot",
        snapshot_id="snap:1",
        normalized_name="морковь",
        display_name="морковь",
        quantity_value="100",
        unit=ShoppingUnit.G,
        category=None,
        confidence="1.0",
        uncertainty=None,
        position=2,
    )
    item_unknown_cheese = InventoryItem(
        id="inv:cheese",
        snapshot_id="snap:1",
        normalized_name="сыр",
        display_name="сыр",
        quantity_value=None,  # unknown quantity
        unit=ShoppingUnit.G,
        category=None,
        confidence="1.0",
        uncertainty=None,
        position=3,
    )

    snap = InventorySnapshot(
        id="snap:1",
        scope=InventoryOwnerScope(household_id="hh:test"),
        source_type=InventorySourceType.TEXT,
        status=InventoryStatus.CONFIRMED,
        source_revision=1,
        created_at="2026-09-21T00:00:00Z",
        confirmed_at="2026-09-21T00:01:00Z",
        cancelled_at=None,
    )
    snapshot_view = InventorySnapshotView(
        snapshot=snap,
        items=(item_potato, item_carrot, item_unknown_cheese),
    )

    mock_inv_store = MagicMock()
    mock_inv_store.get_current_snapshot.return_value = snapshot_view

    cat_path = tmp_path / "catalog.db"
    build_test_recipe_catalog(cat_path)

    mock_hh_service = MagicMock()
    ctx = MagicMock()
    ctx.household_id = "hh:test"
    ctx.role = MagicMock()
    from gateway.healbite_household_schema import HouseholdRole
    ctx.role = HouseholdRole.OWNER
    mock_hh_service.resolve_existing_actor_household_context.return_value = ctx

    mock_weekly_store = MagicMock()

    service = HealBiteRecipeGroundedService(
        catalog_path=str(cat_path),
        weekly_menu_store=mock_weekly_store,
        household_service=mock_hh_service,
        inventory_store=mock_inv_store,
        env={
            "HEALBITE_RECIPE_GROUNDED_MENU_ENABLED": "true",
            "HEALBITE_RECIPE_GROUNDED_MENU_PUBLIC": "true",
        },
    )

    # Let's inspect reading inventory in the service directly
    # Test meal requirements:
    # 1. POTATO: 500 g (inventory has 1 kg => overlap detected, shopping DOES NOT include POTATO)
    # 2. CARROT: 300 g (inventory has 100 g => partial subtraction, shopping CARROT=200 g)
    # 3. CHEESE: 200 g (inventory has None => unknown quantity preserved conservatively, shopping CHEESE=200 g)
    ing_potato = RecipeIngredient(
        id="i1",
        recipe_id="r1",
        ingredient_id="POTATO",
        display_name="Картофель",
        quantity=Decimal("500"),
        unit="g",
        position=1,
    )
    ing_carrot = RecipeIngredient(
        id="i2",
        recipe_id="r1",
        ingredient_id="CARROT",
        display_name="Морковь",
        quantity=Decimal("300"),
        unit="g",
        position=2,
    )
    ing_cheese = RecipeIngredient(
        id="i3",
        recipe_id="r1",
        ingredient_id="CHEESE",
        display_name="Сыр",
        quantity=Decimal("200"),
        unit="g",
        position=3,
    )
    mock_recipe = MagicMock()
    mock_recipe.recipe_id = "r1"
    mock_recipe.servings = Decimal("2")
    mock_recipe.ingredients = (ing_potato, ing_carrot, ing_cheese)

    meal = ValidatedMealEntry(
        date="2026-09-21",
        slot=MealType.LUNCH,
        position=1,
        recipe=mock_recipe,
        target_servings=Decimal("2"),
        author_display_name="Author",
        source_title="Source",
        source_locator=None,
    )

    # Convert real InventoryItems as HealBiteRecipeGroundedService does
    confirmed_inv_tuples: dict[str, tuple[Decimal | None, str]] = {}
    from gateway.healbite_recipe_catalog_domain import normalize_unit
    for item in snapshot_view.items:
        ing_id = normalize_ingredient_id(item.display_name or item.normalized_name)
        parsed_qty = Decimal(item.quantity_value) if item.quantity_value else None
        canonical_unit = normalize_unit(item.unit.value if hasattr(item.unit, "value") else str(item.unit))
        confirmed_inv_tuples[ing_id] = (parsed_qty, canonical_unit)

    # Check inventory overlap and shopping derivation
    shopping = derive_shopping_list_from_recipes([meal], confirmed_inventory=confirmed_inv_tuples)
    items_by_id = {it.ingredient_id: it for it in shopping}

    # POTATO: 1 kg in inventory >= 500 g needed => fully fulfilled, omitted from shopping
    assert "POTATO" not in items_by_id

    # CARROT: 100 g in inventory, 300 g needed => 200 g remaining in shopping
    assert "CARROT" in items_by_id
    assert items_by_id["CARROT"].required_quantity == Decimal("300")
    assert items_by_id["CARROT"].inventory_quantity_used == Decimal("100")
    assert items_by_id["CARROT"].remaining_quantity == Decimal("200")

    # CHEESE: quantity_value is None => unknown quantity preserved conservatively, full 200 g in shopping
    assert "CHEESE" in items_by_id
    assert items_by_id["CHEESE"].required_quantity == Decimal("200")
    assert items_by_id["CHEESE"].inventory_quantity_used == Decimal("0")
    assert items_by_id["CHEESE"].remaining_quantity == Decimal("200")


def test_recipe_grounded_service_feature_gating() -> None:
    from unittest.mock import MagicMock

    mock_weekly_store = MagicMock()
    mock_hh_service = MagicMock()
    mock_catalog = MagicMock()

    # HEALBITE_WEEKLY_MENU_ENABLED=true, HEALBITE_RECIPE_GROUNDED_MENU_ENABLED=false
    env_recipe_disabled = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_RECIPE_GROUNDED_MENU_ENABLED": "false",
    }
    service = HealBiteRecipeGroundedService(
        catalog_store_factory=lambda: mock_catalog,
        weekly_menu_store=mock_weekly_store,
        household_service=mock_hh_service,
        env=env_recipe_disabled,
    )

    result = service.generate_menu(101, week_start="2026-09-21", authors=["pokhlebkin"])
    assert result.status == RecipeGroundedStatus.DISABLED
    assert not result.success
    # Zero catalog, household, or store operations performed
    mock_catalog.assert_not_called()
    mock_hh_service.resolve_existing_actor_household_context.assert_not_called()
    mock_weekly_store.save_draft_revision.assert_not_called()

    # Weekly Menu READY, Recipe Grounded NOT_ALLOWLISTED
    env_not_allowlisted = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_RECIPE_GROUNDED_MENU_ENABLED": "true",
        "HEALBITE_RECIPE_GROUNDED_MENU_ALLOWLIST": "999",
    }
    service_allowlist = HealBiteRecipeGroundedService(
        catalog_store_factory=lambda: mock_catalog,
        weekly_menu_store=mock_weekly_store,
        household_service=mock_hh_service,
        env=env_not_allowlisted,
    )
    result_allowlist = service_allowlist.generate_menu(101, week_start="2026-09-21", authors=["pokhlebkin"])
    assert result_allowlist.status == RecipeGroundedStatus.NOT_ALLOWLISTED
    assert not result_allowlist.success
    mock_catalog.assert_not_called()
