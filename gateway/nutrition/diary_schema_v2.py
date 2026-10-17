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

import logging
import sqlite3
from typing import Any

from gateway.nutrition.contracts import (
    MacronutrientIntervals,
    NutrientInterval,
    UncertaintyModelType,
)

logger = logging.getLogger(__name__)

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


def get_existing_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Returns set of column names currently present in the specified table."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def ensure_nutrition_diary_v2_schema(
    conn: sqlite3.Connection,
    table_name: str = "nutrition_log",
) -> list[str]:
    """
    Applies non-destructive schema migration to the nutrition log table.
    Adds any missing v2 interval columns via ALTER TABLE ADD COLUMN.
    Returns list of newly added column names.
    """
    # Check if table exists
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

    if added:
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
