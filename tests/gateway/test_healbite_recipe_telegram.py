from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from gateway.healbite_recipe_catalog_domain import (
    MealType,
    Recipe,
    RecipeAuthor,
    RecipeIngredient,
    RecipeInstruction,
    RecipeSource,
    RightsStatus,
    SourceType,
    VerificationStatus,
)
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore
from gateway.healbite_recipe_fixtures import build_test_recipe_catalog
from gateway.healbite_weekly_menu_telegram import (
    HealBiteWeeklyMenuTelegramController,
    parse_weekly_menu_callback,
    render_author_selection_screen,
    render_recipe_detail_screen,
    render_source_detail_screen,
)


def test_telegram_callback_parsing() -> None:
    cb1 = parse_weekly_menu_callback("weekly_menu:v1:rcp")
    assert cb1 is not None
    assert cb1.action == "rcp"

    cb2 = parse_weekly_menu_callback("weekly_menu:v1:rcp_gen")
    assert cb2 is not None
    assert cb2.action == "rcp_gen"
    assert cb2.target_id is None

    cb3 = parse_weekly_menu_callback("weekly_menu:v1:rcp_gen:pokhlebkin")
    assert cb3 is not None
    assert cb3.action == "rcp_gen"
    assert cb3.target_id == "pokhlebkin"

    cb4 = parse_weekly_menu_callback("weekly_menu:v1:rcp_v:123e4567-e89b-12d3-a456-426614174000")
    assert cb4 is not None
    assert cb4.action == "rcp_v"
    assert cb4.target_id == "123e4567-e89b-12d3-a456-426614174000"

    cb5 = parse_weekly_menu_callback("weekly_menu:v1:rcp_s:SRC_POKHLEBKIN")
    assert cb5 is not None
    assert cb5.action == "rcp_s"
    assert cb5.target_id == "SRC_POKHLEBKIN"


def test_render_screens_with_real_domain_objects() -> None:
    authors = [{"id": "a1", "name": "Шеф А"}]
    screen_authors = render_author_selection_screen(authors)
    assert "Шеф А" in str(screen_authors.rows)

    author = RecipeAuthor(
        author_id="AUTHOR_TEST",
        display_name="Шеф-повар Иван",
        bio="Известный кулинар",
        created_at="2026-09-21T00:00:00Z",
    )

    ing1 = RecipeIngredient(
        id="ing:1",
        recipe_id="rcp-1",
        ingredient_id="POTATO",
        display_name="Картофель",
        quantity=2,
        unit="шт",
        position=1,
    )
    ing2 = RecipeIngredient(
        id="ing:2",
        recipe_id="rcp-1",
        ingredient_id="BEETROOT",
        display_name="Свёкла",
        quantity=1,
        unit="шт",
        position=2,
    )

    real_recipe = Recipe(
        recipe_id="rcp-1",
        recipe_version=1,
        author_id="AUTHOR_TEST",
        source_id="SRC_TEST",
        title="Тестовый борщ",
        normalized_title="тестовый борщ",
        meal_types=(MealType.LUNCH,),
        cuisine="Русская",
        servings=2,
        prep_minutes=15,
        cook_minutes=45,
        total_minutes=60,
        difficulty="medium",
        ingredients=(ing1, ing2),
        instructions=(),
        tags=(),
        source_locator="Стр. 42",
        source_content_hash="hash",
        rights_status=RightsStatus.PUBLIC_DOMAIN,
        verified=True,
        verification_status=VerificationStatus.VERIFIED,
        created_at="2026-09-21T00:00:00Z",
    )

    real_source = RecipeSource(
        source_id="SRC_TEST",
        author_id="AUTHOR_TEST",
        title="Кулинарная книга",
        source_type=SourceType.BOOK,
        source_locator="М., 1950",
        publication_year=1950,
        language="ru",
        rights_status=RightsStatus.PUBLIC_DOMAIN,
        source_content_hash="hash",
        created_at="2026-09-21T00:00:00Z",
    )

    screen_recipe = render_recipe_detail_screen(real_recipe, real_source, author=author)
    assert "Тестовый борщ" in screen_recipe.text
    assert "Шеф-повар Иван" in screen_recipe.text
    assert "Стр. 42" in screen_recipe.text
    assert "подготовка 15 мин" in screen_recipe.text
    assert "готовка 45 мин" in screen_recipe.text
    assert "Порций в базе:" in screen_recipe.text
    assert "2" in screen_recipe.text
    assert "Картофель — 2 шт" in screen_recipe.text
    assert "Свёкла — 1 шт" in screen_recipe.text

    screen_source = render_source_detail_screen(real_source, author=author)
    assert "Кулинарная книга" in screen_source.text
    assert "Шеф-повар Иван" in screen_source.text
    assert "Год издания:" in screen_source.text
    assert "1950" in screen_source.text
    assert "М., 1950" in screen_source.text
    assert "PUBLIC_DOMAIN" in screen_source.text


def test_telegram_controller_recipe_gating(tmp_path: Path) -> None:
    db_path = tmp_path / "telegram_test.db"
    cat_path = tmp_path / "catalog.db"
    build_test_recipe_catalog(cat_path)

    # Controller with gate DISABLED
    env_disabled = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_RECIPE_GROUNDED_MENU_ENABLED": "false",
        "HEALBITE_RECIPE_CATALOG_PATH": str(cat_path),
    }

    mock_runtime = MagicMock()
    mock_runtime.get_availability.return_value = MagicMock(ready=True)
    mock_runtime.get_active_published_weekly_menu_for_week.return_value = None
    mock_runtime.get_weekly_menu_for_week.return_value = None

    ctrl_disabled = HealBiteWeeklyMenuTelegramController(
        runtime_factory=lambda: mock_runtime,
        db_path=db_path,
        env=env_disabled,
        catalog_store_factory=lambda: HealBiteRecipeCatalogStore(cat_path, read_only=True),
    )

    res_disabled = ctrl_disabled.home(101)
    all_callbacks_disabled = [b[1] for row in res_disabled.screen.rows for b in row]
    assert not any("rcp" in cb for cb in all_callbacks_disabled)

    # Controller with gate ENABLED
    env_enabled = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_RECIPE_GROUNDED_MENU_ENABLED": "true",
        "HEALBITE_RECIPE_GROUNDED_MENU_ALLOWLIST": "101",
        "HEALBITE_RECIPE_CATALOG_PATH": str(cat_path),
    }
    ctrl_enabled = HealBiteWeeklyMenuTelegramController(
        runtime_factory=lambda: mock_runtime,
        db_path=db_path,
        env=env_enabled,
        catalog_store_factory=lambda: HealBiteRecipeCatalogStore(cat_path, read_only=True),
    )

    res_enabled = ctrl_enabled.home(101)
    all_callbacks_enabled = [b[1] for row in res_enabled.screen.rows for b in row]
    assert any("rcp" in cb for cb in all_callbacks_enabled)


def test_new_code_old_schema_backward_compatibility(tmp_path: Path) -> None:
    """Verify that NEW_CODE + OLD_SCHEMA + RECIPE_FEATURE_OFF functions normally without schema error."""
    import sqlite3
    from gateway.healbite_weekly_menu_schema import (
        WeeklyMenuSchemaState,
        detect_weekly_menu_schema_state,
        has_weekly_menu_recipe_refs_schema,
    )
    from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore

    db_path = tmp_path / "old_schema.db"
    store = HealBiteWeeklyMenuStore(db_path=db_path)
    store.initialize_schema()

    conn = sqlite3.connect(str(db_path))
    # Assert old schema does NOT have recipe_refs table initially
    assert not has_weekly_menu_recipe_refs_schema(conn)
    assert detect_weekly_menu_schema_state(conn) is WeeklyMenuSchemaState.CANONICAL
    conn.close()

    # Reading recipe refs on old schema safely returns empty dict without errors
    assert store.get_revision_recipe_refs("any-rev-id") == {}
    assert store.get_entry_recipe_ref("any-entry-id") is None


def test_stale_recipe_callback_gating(tmp_path: Path) -> None:
    """Verify that all recipe callbacks (rcp, rcp_gen, rcp_v, rcp_s, rcp_t, rcp_x) fail closed when feature is OFF."""
    db_path = tmp_path / "telegram_stale_test.db"
    cat_path = tmp_path / "catalog.db"
    build_test_recipe_catalog(cat_path)

    env_disabled = {
        "HEALBITE_WEEKLY_MENU_ENABLED": "true",
        "HEALBITE_RECIPE_GROUNDED_MENU_ENABLED": "false",
        "HEALBITE_RECIPE_CATALOG_PATH": str(cat_path),
    }

    mock_runtime = MagicMock()
    mock_runtime.get_availability.return_value = MagicMock(ready=True)

    mock_catalog = MagicMock()

    ctrl = HealBiteWeeklyMenuTelegramController(
        runtime_factory=lambda: mock_runtime,
        db_path=db_path,
        env=env_disabled,
        catalog_store_factory=lambda: mock_catalog,
    )

    actor = 101

    for cb in (
        "weekly_menu:v1:rcp",
        "weekly_menu:v1:rcp_gen",
        "weekly_menu:v1:rcp_gen:pokhlebkin",
        "weekly_menu:v1:rcp_v:test-recipe-id",
        "weekly_menu:v1:rcp_s:test-source-id",
        "weekly_menu:v1:rcp_t:test-target",
        "weekly_menu:v1:rcp_x:test-target",
    ):
        res = ctrl.handle_callback(actor, cb)
        assert res.state == "disabled", f"Callback {cb} did not fail closed! Got state: {res.state}"
        assert res.screen.text == "В разработке" or "недоступ" in res.screen.text.lower()

    # Zero catalog or mutation calls
    mock_catalog.get_recipe.assert_not_called()
    mock_catalog.get_source.assert_not_called()
    mock_catalog.list_authors.assert_not_called()
