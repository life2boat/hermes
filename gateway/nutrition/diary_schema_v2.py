"""
gateway.nutrition.diary_schema_v2 — Database schema migration and compatibility for Nutrition Intelligence v2.2.

Extends the nutrition_log table non-destructively with nullable interval columns:
- calories_min, calories_max
- protein_min, protein_max
- fat_min, fat_max
- carbs_min, carbs_max
- uncertainty_model_type
- is_approximate_range

Maintains 100% backward and rollback compatibility with HER-10.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from enum import Enum
from typing import Any

from gateway.nutrition.contracts import (
    MacronutrientIntervals,
    NutrientInterval,
    UncertaintyModelType,
)

logger = logging.getLogger(__name__)

NUTRITION_LOG_TABLE = "nutrition_log"
NUTRITION_DIARY_V2_MIGRATION_ID = "healbite-nutrition-diary-schema-v2"

BASE_NUTRITION_LOG_COLUMNS: tuple[tuple[str, str, int, str | None, int], ...] = (
    ("id", "INTEGER", 0, None, 1),
    ("user_id", "INTEGER", 1, None, 0),
    ("source", "TEXT", 1, None, 0),
    ("meal_name", "TEXT", 1, None, 0),
    ("items_json", "TEXT", 1, None, 0),
    ("calories_kcal", "REAL", 0, None, 0),
    ("protein_g", "REAL", 0, None, 0),
    ("fat_g", "REAL", 0, None, 0),
    ("carbs_g", "REAL", 0, None, 0),
    ("confidence", "REAL", 0, None, 0),
    ("occurred_at", "TEXT", 1, None, 0),
    ("created_at", "TEXT", 1, "CURRENT_TIMESTAMP", 0),
    ("raw_summary", "TEXT", 1, None, 0),
    ("image_ref", "TEXT", 0, None, 0),
    ("qdrant_indexed", "INTEGER", 1, "0", 0),
)

V2_NUTRITION_DIARY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("calories_min", "REAL"),
    ("calories_max", "REAL"),
    ("protein_min", "REAL"),
    ("protein_max", "REAL"),
    ("fat_min", "REAL"),
    ("fat_max", "REAL"),
    ("carbs_min", "REAL"),
    ("carbs_max", "REAL"),
    ("uncertainty_model_type", "TEXT"),
    ("is_approximate_range", "INTEGER"),
)

NUTRITION_LOG_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {NUTRITION_LOG_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    meal_name TEXT NOT NULL,
    items_json TEXT NOT NULL,
    calories_kcal REAL,
    protein_g REAL,
    fat_g REAL,
    carbs_g REAL,
    confidence REAL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_summary TEXT NOT NULL,
    image_ref TEXT,
    qdrant_indexed INTEGER NOT NULL DEFAULT 0,
    calories_min REAL,
    calories_max REAL,
    protein_min REAL,
    protein_max REAL,
    fat_min REAL,
    fat_max REAL,
    carbs_min REAL,
    carbs_max REAL,
    uncertainty_model_type TEXT,
    is_approximate_range INTEGER
)
""".strip()

NUTRITION_LOG_USER_OCCURRED_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{NUTRITION_LOG_TABLE}_user_occurred_at "
    f"ON {NUTRITION_LOG_TABLE}(user_id, occurred_at)"
)

NUTRITION_LOG_USER_IMAGE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{NUTRITION_LOG_TABLE}_user_image_ref "
    f"ON {NUTRITION_LOG_TABLE}(user_id, image_ref)"
)

NUTRITION_DIARY_V2_SCHEMA_STATEMENTS: tuple[str, ...] = (
    NUTRITION_LOG_CREATE_SQL,
    NUTRITION_LOG_USER_OCCURRED_INDEX_SQL,
    NUTRITION_LOG_USER_IMAGE_INDEX_SQL,
)

_MIGRATION_RECIPE = "\n".join(
    (
        f"migration_id={NUTRITION_DIARY_V2_MIGRATION_ID}",
        *NUTRITION_DIARY_V2_SCHEMA_STATEMENTS,
        *(
            f"ALTER TABLE {NUTRITION_LOG_TABLE} ADD COLUMN {col_name} {col_type}"
            for col_name, col_type in V2_NUTRITION_DIARY_COLUMNS
        ),
    )
)
NUTRITION_DIARY_V2_MIGRATION_SOURCE = _MIGRATION_RECIPE + "\n"
NUTRITION_DIARY_V2_MIGRATION_SHA256 = hashlib.sha256(
    NUTRITION_DIARY_V2_MIGRATION_SOURCE.encode("utf-8")
).hexdigest()


class NutritionDiarySchemaClassification(str, Enum):
    ABSENT = "ABSENT"
    KNOWN_COMPATIBLE_PARTIAL = "KNOWN_COMPATIBLE_PARTIAL"
    CURRENT = "CURRENT"
    INCOMPATIBLE = "INCOMPATIBLE"


def nutrition_diary_v2_schema_statements() -> tuple[str, ...]:
    return NUTRITION_DIARY_V2_SCHEMA_STATEMENTS


def get_existing_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Returns set of column names currently present in the specified table."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def classify_nutrition_diary_v2_schema(
    conn: sqlite3.Connection,
    table_name: str = NUTRITION_LOG_TABLE,
) -> NutritionDiarySchemaClassification:
    """Classifies the nutrition diary schema state against canonical v2 requirements."""
    # Check if table exists in sqlite_master
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = ?",
        (table_name,),
    ).fetchone()
    if row is None:
        return NutritionDiarySchemaClassification.ABSENT
    if str(row[0]).lower() != "table":
        return NutritionDiarySchemaClassification.INCOMPATIBLE

    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    cols = cursor.fetchall()
    if not cols:
        return NutritionDiarySchemaClassification.INCOMPATIBLE

    col_map = {r[1]: r for r in cols}
    base_names = {b[0] for b in BASE_NUTRITION_LOG_COLUMNS}
    v2_names = {v[0] for v in V2_NUTRITION_DIARY_COLUMNS}
    v2_type_map = dict(V2_NUTRITION_DIARY_COLUMNS)

    # 1. Base columns must all be present with compatible types
    for name, expected_type, notnull, _dflt, pk in BASE_NUTRITION_LOG_COLUMNS:
        if name not in col_map:
            return NutritionDiarySchemaClassification.INCOMPATIBLE
        r = col_map[name]
        col_type = str(r[2]).upper()
        if col_type != expected_type.upper():
            return NutritionDiarySchemaClassification.INCOMPATIBLE
        if r[5] != pk:
            return NutritionDiarySchemaClassification.INCOMPATIBLE

    # 2. Inspect extra columns beyond the 15 base columns
    extra_names = set(col_map) - base_names
    for col_name in extra_names:
        if col_name not in v2_names:
            # Unknown column outside base and v2 specification
            return NutritionDiarySchemaClassification.INCOMPATIBLE
        r = col_map[col_name]
        col_type = str(r[2]).upper()
        if col_type != v2_type_map[col_name].upper():
            return NutritionDiarySchemaClassification.INCOMPATIBLE
        if r[5] != 0:  # cannot be part of PK
            return NutritionDiarySchemaClassification.INCOMPATIBLE

    # 3. Check indices
    index_rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
        (table_name,),
    ).fetchall()
    index_names = {r[0] for r in index_rows}
    has_occurred_idx = f"idx_{table_name}_user_occurred_at" in index_names
    has_image_idx = f"idx_{table_name}_user_image_ref" in index_names

    # If all 10 v2 columns and indices are present -> CURRENT
    if len(extra_names) == len(v2_names) and has_occurred_idx and has_image_idx:
        return NutritionDiarySchemaClassification.CURRENT

    # If all base columns are valid and either fewer v2 columns or missing indices -> KNOWN_COMPATIBLE_PARTIAL
    return NutritionDiarySchemaClassification.KNOWN_COMPATIBLE_PARTIAL


def ensure_nutrition_diary_v2_schema(
    conn: sqlite3.Connection,
    table_name: str = NUTRITION_LOG_TABLE,
    *,
    commit: bool = True,
) -> list[str]:
    """
    Applies non-destructive schema migration to the nutrition log table.
    Adds any missing v2 interval columns via ALTER TABLE ADD COLUMN.
    Returns list of newly added column names.
    """
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    if not cursor.fetchone():
        logger.debug("Table %s does not exist yet; skipping v2 column migration", table_name)
        return []

    existing = get_existing_table_columns(conn, table_name)
    added: list[str] = []

    for col_name, col_type in V2_NUTRITION_DIARY_COLUMNS:
        if col_name not in existing:
            sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
            conn.execute(sql)
            added.append(col_name)
            logger.info("Added v2 column %s to %s", col_name, table_name)

    if added and commit:
        conn.commit()

    return added


def migrate_nutrition_diary_v2_schema(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> list[str]:
    """
    Applies canonical staged schema migration for nutrition_diary_v2.
    - If nutrition_log is absent, creates the full 25-column table and indices.
    - If nutrition_log exists, non-destructively adds missing interval columns and ensures indices.
    Returns list of added column names.
    """
    state = classify_nutrition_diary_v2_schema(conn)
    if state is NutritionDiarySchemaClassification.INCOMPATIBLE:
        raise sqlite3.DatabaseError("nutrition diary schema is incompatible")
    if state is NutritionDiarySchemaClassification.CURRENT:
        return []

    if state is NutritionDiarySchemaClassification.ABSENT:
        for stmt in NUTRITION_DIARY_V2_SCHEMA_STATEMENTS:
            conn.execute(stmt)
        if commit:
            conn.commit()
        return [col_name for col_name, _ in V2_NUTRITION_DIARY_COLUMNS]

    # Staged migration: add missing columns
    added = ensure_nutrition_diary_v2_schema(conn, NUTRITION_LOG_TABLE, commit=False)
    conn.execute(NUTRITION_LOG_USER_OCCURRED_INDEX_SQL)
    conn.execute(NUTRITION_LOG_USER_IMAGE_INDEX_SQL)
    if commit:
        conn.commit()
    return added


def extract_macronutrient_intervals_from_row(row: dict[str, Any] | sqlite3.Row) -> MacronutrientIntervals:
    """Extracts MacronutrientIntervals from a database row dictionary if interval columns are populated."""
    def _make_interval(min_val: Any, center_val: Any, max_val: Any) -> NutrientInterval | None:
        if min_val is None and max_val is None:
            if center_val is None:
                return None
            c = float(center_val)
            return NutrientInterval(min_value=c, center_value=c, max_value=c)
        c = float(center_val) if center_val is not None else float(min_val or 0.0)
        mn = float(min_val) if min_val is not None else c
        mx = float(max_val) if max_val is not None else c
        return NutrientInterval(min_value=mn, center_value=c, max_value=mx)

    cal = _make_interval(
        row.get("calories_min") if isinstance(row, dict) else row["calories_min"],
        row.get("calories_kcal") if isinstance(row, dict) else row["calories_kcal"],
        row.get("calories_max") if isinstance(row, dict) else row["calories_max"],
    )
    prot = _make_interval(
        row.get("protein_min") if isinstance(row, dict) else row["protein_min"],
        row.get("protein_g") if isinstance(row, dict) else row["protein_g"],
        row.get("protein_max") if isinstance(row, dict) else row["protein_max"],
    )
    fat = _make_interval(
        row.get("fat_min") if isinstance(row, dict) else row["fat_min"],
        row.get("fat_g") if isinstance(row, dict) else row["fat_g"],
        row.get("fat_max") if isinstance(row, dict) else row["fat_max"],
    )
    carbs = _make_interval(
        row.get("carbs_min") if isinstance(row, dict) else row["carbs_min"],
        row.get("carbs_g") if isinstance(row, dict) else row["carbs_g"],
        row.get("carbs_max") if isinstance(row, dict) else row["carbs_max"],
    )

    return MacronutrientIntervals(
        calories_kcal=cal,
        protein_g=prot,
        fat_g=fat,
        carbs_g=carbs,
    )
