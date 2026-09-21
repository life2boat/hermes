from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Sequence

from gateway.healbite_recipe_catalog_domain import (
    CatalogMetadata,
    MealType,
    Recipe,
    RecipeAdaptation,
    RecipeAuthor,
    RecipeIngredient,
    RecipeInstruction,
    RecipeSource,
    RightsStatus,
    SourceType,
    VerificationStatus,
)

CATALOG_SCHEMA_VERSION = 1

CATALOG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_authors (
    author_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    bio TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_sources (
    source_id TEXT PRIMARY KEY,
    author_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_locator TEXT,
    publication_year INTEGER,
    language TEXT NOT NULL DEFAULT 'ru',
    rights_status TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (author_id) REFERENCES recipe_authors(author_id)
);

CREATE TABLE IF NOT EXISTS recipes (
    recipe_id TEXT NOT NULL,
    recipe_version INTEGER NOT NULL DEFAULT 1,
    author_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    meal_types_json TEXT NOT NULL,
    cuisine TEXT,
    servings TEXT NOT NULL,
    prep_minutes INTEGER,
    cook_minutes INTEGER,
    total_minutes INTEGER,
    difficulty TEXT,
    tags_json TEXT NOT NULL,
    source_locator TEXT,
    source_content_hash TEXT NOT NULL,
    rights_status TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    verification_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (recipe_id, recipe_version),
    FOREIGN KEY (author_id) REFERENCES recipe_authors(author_id),
    FOREIGN KEY (source_id) REFERENCES recipe_sources(source_id)
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL,
    recipe_version INTEGER NOT NULL DEFAULT 1,
    ingredient_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    quantity_value TEXT,
    quantity_unit TEXT NOT NULL,
    optional INTEGER NOT NULL DEFAULT 0,
    preparation_note TEXT,
    position INTEGER NOT NULL,
    FOREIGN KEY (recipe_id, recipe_version) REFERENCES recipes(recipe_id, recipe_version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS recipe_instructions (
    id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL,
    recipe_version INTEGER NOT NULL DEFAULT 1,
    step_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    timing_minutes INTEGER,
    FOREIGN KEY (recipe_id, recipe_version) REFERENCES recipes(recipe_id, recipe_version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS recipe_adaptations (
    adaptation_id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL,
    original_ingredient_id TEXT NOT NULL,
    replacement_ingredient_id TEXT NOT NULL,
    ratio TEXT NOT NULL DEFAULT '1.0',
    reason TEXT NOT NULL,
    source_verified INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_recipes_author ON recipes(author_id);
CREATE INDEX IF NOT EXISTS idx_recipes_source ON recipes(source_id);
CREATE INDEX IF NOT EXISTS idx_recipes_verified ON recipes(verified, verification_status);
CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id, recipe_version);
CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_ing_id ON recipe_ingredients(ingredient_id);
CREATE INDEX IF NOT EXISTS idx_recipe_instructions_recipe ON recipe_instructions(recipe_id, recipe_version);
"""

CATALOG_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
    recipe_id UNINDEXED,
    title,
    tags,
    tokenize='unicode61'
);
"""


class RecipeCatalogError(Exception):
    pass


class CatalogNotFoundError(RecipeCatalogError):
    pass


class CatalogIntegrityError(RecipeCatalogError):
    pass


class CatalogSchemaError(RecipeCatalogError):
    pass


class CatalogReadOnlyError(RecipeCatalogError):
    pass


def compute_catalog_content_hash(conn: sqlite3.Connection) -> str:
    """Deterministically compute SHA-256 over all recipes and ingredients."""
    cur = conn.cursor()
    cur.execute(
        "SELECT recipe_id, recipe_version, author_id, source_id, title, "
        "meal_types_json, servings, rights_status, verified, verification_status "
        "FROM recipes ORDER BY recipe_id, recipe_version"
    )
    rows = cur.fetchall()
    hasher = hashlib.sha256()
    for row in rows:
        row_str = "|".join(str(val) for val in row)
        hasher.update(row_str.encode("utf-8"))
        # Hash ingredients
        cur.execute(
            "SELECT ingredient_id, display_name, quantity_value, quantity_unit, optional, position "
            "FROM recipe_ingredients WHERE recipe_id = ? AND recipe_version = ? ORDER BY position",
            (row[0], row[1]),
        )
        for ing in cur.fetchall():
            ing_str = "|".join(str(v) for v in ing)
            hasher.update(ing_str.encode("utf-8"))
    return hasher.hexdigest()


class HealBiteRecipeCatalogStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        read_only: bool = True,
        validate_hash: bool = True,
    ) -> None:
        self._path = Path(db_path)
        self._read_only = read_only
        self._validate_hash = validate_hash
        self._metadata: CatalogMetadata | None = None
        if read_only:
            self._verify_read_only_catalog()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self._path.exists():
            raise CatalogNotFoundError(f"Catalog database not found at {self._path}")
        if self._read_only:
            uri = f"file:{self._path.resolve().as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _verify_read_only_catalog(self) -> None:
        with self._connection() as conn:
            meta = self._load_metadata(conn)
            if meta.schema_version != CATALOG_SCHEMA_VERSION:
                raise CatalogSchemaError(
                    f"Unsupported catalog schema version {meta.schema_version} (expected {CATALOG_SCHEMA_VERSION})"
                )
            if self._validate_hash:
                computed = compute_catalog_content_hash(conn)
                if computed != meta.content_hash:
                    raise CatalogIntegrityError(
                        f"Catalog content hash mismatch: stored={meta.content_hash[:8]}... computed={computed[:8]}..."
                    )
            self._metadata = meta

    def get_metadata(self) -> CatalogMetadata:
        if self._metadata is not None:
            return self._metadata
        with self._connection() as conn:
            self._metadata = self._load_metadata(conn)
            return self._metadata

    def get_content_hash(self) -> str:
        return self.get_metadata().content_hash

    def count_recipes(self) -> int:
        return self.get_metadata().recipe_count

    @staticmethod
    def _load_metadata(conn: sqlite3.Connection) -> CatalogMetadata:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_metadata'")
        if not cur.fetchone():
            raise CatalogSchemaError("Catalog database missing catalog_metadata table")
        cur.execute("SELECT key, value FROM catalog_metadata")
        meta_dict = dict(cur.fetchall())
        try:
            return CatalogMetadata(
                schema_version=int(meta_dict.get("schema_version", 0)),
                build_id=meta_dict.get("build_id", ""),
                content_hash=meta_dict.get("content_hash", ""),
                recipe_count=int(meta_dict.get("recipe_count", 0)),
                verified_recipe_count=int(meta_dict.get("verified_recipe_count", 0)),
                created_at=meta_dict.get("created_at", ""),
            )
        except Exception as exc:
            raise CatalogIntegrityError(f"Invalid catalog metadata: {exc}") from exc

    def get_author(self, author_id: str) -> RecipeAuthor | None:
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT author_id, display_name, bio, created_at FROM recipe_authors WHERE author_id = ?",
                (author_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return RecipeAuthor(
                author_id=row["author_id"],
                display_name=row["display_name"],
                bio=row["bio"],
                created_at=row["created_at"],
            )

    def list_authors(self) -> list[RecipeAuthor]:
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT author_id, display_name, bio, created_at FROM recipe_authors ORDER BY display_name"
            )
            return [
                RecipeAuthor(
                    author_id=row["author_id"],
                    display_name=row["display_name"],
                    bio=row["bio"],
                    created_at=row["created_at"],
                )
                for row in cur.fetchall()
            ]

    def get_source(self, source_id: str) -> RecipeSource | None:
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM recipe_sources WHERE source_id = ?", (source_id,))
            row = cur.fetchone()
            if not row:
                return None
            return RecipeSource(
                source_id=row["source_id"],
                author_id=row["author_id"],
                title=row["title"],
                source_type=SourceType(row["source_type"]),
                source_locator=row["source_locator"],
                publication_year=row["publication_year"],
                language=row["language"],
                rights_status=RightsStatus(row["rights_status"]),
                source_content_hash=row["source_content_hash"],
                created_at=row["created_at"],
            )

    def get_recipe(self, recipe_id: str, version: int | None = None) -> Recipe | None:
        with self._connection() as conn:
            cur = conn.cursor()
            if version is not None:
                cur.execute(
                    "SELECT * FROM recipes WHERE recipe_id = ? AND recipe_version = ?",
                    (recipe_id, version),
                )
            else:
                cur.execute(
                    "SELECT * FROM recipes WHERE recipe_id = ? ORDER BY recipe_version DESC LIMIT 1",
                    (recipe_id,),
                )
            row = cur.fetchone()
            if not row:
                return None
            return self._build_recipe_from_row(conn, row)

    def get_recipes_by_ids(self, recipe_ids: Sequence[str]) -> dict[str, Recipe]:
        if not recipe_ids:
            return {}
        with self._connection() as conn:
            placeholders = ",".join("?" for _ in recipe_ids)
            cur = conn.cursor()
            cur.execute(
                f"SELECT * FROM recipes WHERE recipe_id IN ({placeholders}) ORDER BY recipe_version DESC",
                tuple(recipe_ids),
            )
            result = {}
            for row in cur.fetchall():
                rid = row["recipe_id"]
                if rid not in result:
                    result[rid] = self._build_recipe_from_row(conn, row)
            return result

    def get_all_recipes(self, *, verified_only: bool = True) -> list[Recipe]:
        with self._connection() as conn:
            cur = conn.cursor()
            if verified_only:
                cur.execute(
                    "SELECT * FROM recipes WHERE verified = 1 AND verification_status = 'VERIFIED' "
                    "ORDER BY author_id, title"
                )
            else:
                cur.execute("SELECT * FROM recipes ORDER BY author_id, title")
            recipes = []
            for row in cur.fetchall():
                recipes.append(self._build_recipe_from_row(conn, row))
            return recipes

    def list_recipes_by_author(self, author_id: str, *, verified_only: bool = True) -> list[Recipe]:
        with self._connection() as conn:
            cur = conn.cursor()
            if verified_only:
                cur.execute(
                    "SELECT * FROM recipes WHERE author_id = ? AND verified = 1 AND verification_status = 'VERIFIED' "
                    "ORDER BY title",
                    (author_id,),
                )
            else:
                cur.execute("SELECT * FROM recipes WHERE author_id = ? ORDER BY title", (author_id,))
            return [self._build_recipe_from_row(conn, row) for row in cur.fetchall()]

    def _build_recipe_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Recipe:
        recipe_id = row["recipe_id"]
        version = row["recipe_version"]
        cur = conn.cursor()

        # Ingredients
        cur.execute(
            "SELECT * FROM recipe_ingredients WHERE recipe_id = ? AND recipe_version = ? ORDER BY position",
            (recipe_id, version),
        )
        ingredients = tuple(
            RecipeIngredient(
                id=i_row["id"],
                recipe_id=i_row["recipe_id"],
                ingredient_id=i_row["ingredient_id"],
                display_name=i_row["display_name"],
                quantity=Decimal(i_row["quantity_value"]) if i_row["quantity_value"] is not None else None,
                unit=i_row["quantity_unit"],
                optional=bool(i_row["optional"]),
                preparation_note=i_row["preparation_note"],
                position=i_row["position"],
            )
            for i_row in cur.fetchall()
        )

        # Instructions
        cur.execute(
            "SELECT * FROM recipe_instructions WHERE recipe_id = ? AND recipe_version = ? ORDER BY step_number",
            (recipe_id, version),
        )
        instructions = tuple(
            RecipeInstruction(
                step_number=ins_row["step_number"],
                text=ins_row["text"],
                timing_minutes=ins_row["timing_minutes"],
            )
            for ins_row in cur.fetchall()
        )

        meal_types = tuple(MealType(t) for t in json.loads(row["meal_types_json"]))
        tags = tuple(json.loads(row["tags_json"]))

        return Recipe(
            recipe_id=recipe_id,
            recipe_version=version,
            author_id=row["author_id"],
            source_id=row["source_id"],
            title=row["title"],
            normalized_title=row["normalized_title"],
            meal_types=meal_types,
            cuisine=row["cuisine"],
            servings=Decimal(row["servings"]),
            prep_minutes=row["prep_minutes"],
            cook_minutes=row["cook_minutes"],
            total_minutes=row["total_minutes"],
            difficulty=row["difficulty"],
            ingredients=ingredients,
            instructions=instructions,
            tags=tags,
            source_locator=row["source_locator"],
            source_content_hash=row["source_content_hash"],
            rights_status=RightsStatus(row["rights_status"]),
            verified=bool(row["verified"]),
            verification_status=VerificationStatus(row["verification_status"]),
            created_at=row["created_at"],
        )


def initialize_empty_catalog(db_path: str | Path, *, build_id: str = "init") -> None:
    """Initialize a brand new catalog database schema for ingestion."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        cur.executescript(CATALOG_SCHEMA_SQL)
        cur.executescript(CATALOG_FTS_SQL)
        content_hash = compute_catalog_content_hash(conn)
        meta_items = [
            ("schema_version", str(CATALOG_SCHEMA_VERSION)),
            ("build_id", build_id),
            ("content_hash", content_hash),
            ("recipe_count", "0"),
            ("verified_recipe_count", "0"),
            ("created_at", "2026-09-21 00:00:00"),
        ]
        cur.executemany("INSERT OR REPLACE INTO catalog_metadata(key, value) VALUES (?, ?)", meta_items)
        conn.commit()
    finally:
        conn.close()
