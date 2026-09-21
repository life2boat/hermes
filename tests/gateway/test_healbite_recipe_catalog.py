from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
import pytest

from gateway.healbite_recipe_catalog_domain import (
    MealType,
    RecipeAuthor,
    RecipeSource,
    RightsStatus,
    SourceType,
    VerificationStatus,
    generate_recipe_id,
    normalize_ingredient_id,
    normalize_title,
    normalize_unit,
)
from gateway.healbite_recipe_catalog_store import (
    CatalogIntegrityError,
    CatalogReadOnlyError,
    HealBiteRecipeCatalogStore,
)
from gateway.healbite_recipe_fixtures import (
    PRODUCTION_MANIFESTS,
    TEST_AUTHORS,
    TEST_AUTHOR_A,
    build_test_recipe_catalog,
)
from gateway.healbite_recipe_ingestion import (
    IngestionValidationError,
    RecipeIngestionPipeline,
    RightsPolicyViolationError,
)


def test_uuidv5_deterministic_generation() -> None:
    id1 = generate_recipe_id("author_1", "source_1", "p. 10", "борщ классический")
    id2 = generate_recipe_id("author_1", "source_1", "p. 10", "борщ классический")
    id3 = generate_recipe_id("author_1", "source_1", "p. 10", "щи суточные")

    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 36


def test_normalization_helpers() -> None:
    assert normalize_title("  Борщ  по-деревенски! ") == "борщ по-деревенски"
    assert normalize_unit("кг") == "kg"
    assert normalize_unit("ст. ложка") == "tbsp"
    assert normalize_unit("неизвестно") == "unknown"
    assert normalize_ingredient_id("картофель молодой") == "POTATO"
    assert normalize_ingredient_id("куриное филе") == "CHICKEN_BREAST"


def test_catalog_ingestion_and_content_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog.db"
    content_hash = build_test_recipe_catalog(db_path, build_id="test_run_1")

    assert len(content_hash) == 64

    # Open store with hash validation
    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    assert store.get_content_hash() == content_hash
    assert store.count_recipes() == 27
    authors = store.list_authors()
    # 3 test authors + 3 production manifests
    assert len(authors) == 6

    # Query recipe by author
    a_recipes = store.list_recipes_by_author(TEST_AUTHOR_A)
    assert len(a_recipes) == 9

    # Query recipe by ID
    sample_recipe = a_recipes[0]
    retrieved = store.get_recipe(sample_recipe.recipe_id)
    assert retrieved is not None
    assert retrieved.title == sample_recipe.title
    assert len(retrieved.ingredients) == len(sample_recipe.ingredients)

    # Query source
    source = store.get_source(sample_recipe.source_id)
    assert source is not None
    assert source.author_id == sample_recipe.author_id


def test_catalog_tamper_detection(tmp_path: Path) -> None:
    db_path = tmp_path / "tampered.db"
    build_test_recipe_catalog(db_path, build_id="test_run_2")

    # Directly tamper with SQLite data behind the scenes
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE recipes SET title = 'Tampered Title' WHERE rowid = 1")
    conn.commit()
    conn.close()

    # Opening with validate_hash=True should fail closed
    with pytest.raises(CatalogIntegrityError):
        HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)


def test_catalog_read_only_enforcement(tmp_path: Path) -> None:
    db_path = tmp_path / "readonly.db"
    build_test_recipe_catalog(db_path, build_id="test_run_3")

    store = HealBiteRecipeCatalogStore(db_path, read_only=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with store._connection() as conn:
            conn.execute("DELETE FROM recipes")


def test_rights_policy_rejection(tmp_path: Path) -> None:
    db_path = tmp_path / "rights_fail.db"
    pipeline = RecipeIngestionPipeline(db_path)

    # Source with UNKNOWN rights status must be rejected
    bad_source = RecipeSource(
        source_id="pirated_book",
        author_id="pirate",
        title="Illegal Scan",
        publication_year=2024,
        source_locator="Darknet",
        rights_status=RightsStatus.UNKNOWN,
    )
    with pytest.raises(RightsPolicyViolationError, match="UNKNOWN rights status"):
        pipeline.ingest_source(bad_source)

    # Recipe with UNKNOWN rights status must be rejected
    bad_recipe_data = {
        "author_id": TEST_AUTHOR_A,
        "source_id": f"SRC_{TEST_AUTHOR_A}",
        "title": "Unsafe Soup",
        "meal_types": ["lunch"],
        "servings": 4,
        "rights_status": RightsStatus.UNKNOWN.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [
            {"display_name": "Вода", "quantity": 1, "unit": "l"}
        ],
        "instructions": ["Кипятить"],
    }
    with pytest.raises(RightsPolicyViolationError, match="rights status is UNKNOWN"):
        pipeline.ingest_recipe(bad_recipe_data)

    pipeline.close()


def test_unverified_recipe_rejection(tmp_path: Path) -> None:
    db_path = tmp_path / "unverified_fail.db"
    pipeline = RecipeIngestionPipeline(db_path)

    unverified_recipe_data = {
        "author_id": TEST_AUTHOR_A,
        "source_id": f"SRC_{TEST_AUTHOR_A}",
        "title": "Unverified Meal",
        "meal_types": ["breakfast"],
        "servings": 2,
        "rights_status": RightsStatus.USER_PROVIDED_AUTHORIZED.value,
        "verified": False,
        "verification_status": VerificationStatus.REVIEW_REQUIRED.value,
        "ingredients": [
            {"display_name": "Овсянка", "quantity": 100, "unit": "g"}
        ],
        "instructions": ["Варить"],
    }
    with pytest.raises(IngestionValidationError, match="verified"):
        pipeline.ingest_recipe(unverified_recipe_data)

    pipeline.close()


def test_production_manifests_are_metadata_only() -> None:
    for manifest in PRODUCTION_MANIFESTS:
        author = manifest["author"]
        assert author.display_name
        assert author.author_id in ("AUTHOR_POKHLEBKIN", "AUTHOR_ESCOFFIER", "AUTHOR_JAMIE_OLIVER")
        sources = manifest["sources"]
        assert len(sources) >= 1
        for src in sources:
            assert src.rights_status in (RightsStatus.PUBLIC_DOMAIN, RightsStatus.LINK_ONLY)
            assert "meta_only" in src.source_content_hash
