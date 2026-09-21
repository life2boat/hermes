from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    generate_recipe_id,
    normalize_ingredient_id,
    normalize_title,
    normalize_unit,
)
from gateway.healbite_recipe_catalog_store import (
    CATALOG_SCHEMA_VERSION,
    compute_catalog_content_hash,
    initialize_empty_catalog,
)


class IngestionError(Exception):
    pass


class RightsPolicyViolationError(IngestionError):
    pass


class IngestionValidationError(IngestionError):
    pass


def compute_recipe_content_hash(
    author_id: str,
    source_id: str,
    normalized_title: str,
    ingredients: Sequence[Mapping[str, Any]],
    instructions: Sequence[Mapping[str, Any]],
) -> str:
    hasher = hashlib.sha256()
    hasher.update(f"{author_id}:{source_id}:{normalized_title}".encode("utf-8"))
    for ing in ingredients:
        ing_repr = f"{ing.get('ingredient_id')}:{ing.get('quantity')}:{ing.get('unit')}:{ing.get('optional', False)}"
        hasher.update(ing_repr.encode("utf-8"))
    for ins in instructions:
        ins_repr = f"{ins.get('step_number')}:{ins.get('text')}"
        hasher.update(ins_repr.encode("utf-8"))
    return hasher.hexdigest()


class RecipeIngestionPipeline:
    def __init__(self, db_path: str | Path, *, build_id: str = "build-1") -> None:
        self._path = Path(db_path)
        self._build_id = build_id
        if not self._path.exists():
            initialize_empty_catalog(self._path, build_id=build_id)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def ingest_author(self, author: RecipeAuthor) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO recipe_authors (author_id, display_name, bio, created_at) "
            "VALUES (?, ?, ?, ?)",
            (author.author_id, author.display_name, author.bio, author.created_at or "2026-09-21 00:00:00"),
        )
        self._conn.commit()

    def ingest_source(self, source: RecipeSource) -> None:
        if source.rights_status is RightsStatus.UNKNOWN:
            raise RightsPolicyViolationError(f"Cannot ingest source '{source.source_id}' with UNKNOWN rights status")
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO recipe_sources "
            "(source_id, author_id, title, source_type, source_locator, publication_year, "
            "language, rights_status, source_content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source.source_id,
                source.author_id,
                source.title,
                source.source_type.value,
                source.source_locator,
                source.publication_year,
                source.language,
                source.rights_status.value,
                source.source_content_hash,
                source.created_at or "2026-09-21 00:00:00",
            ),
        )
        self._conn.commit()

    def ingest_recipe(self, raw: Mapping[str, Any]) -> str:
        # Validate rights status
        rights_raw = raw.get("rights_status", RightsStatus.UNKNOWN.value)
        try:
            rights = RightsStatus(rights_raw)
        except ValueError:
            rights = RightsStatus.UNKNOWN

        if rights is RightsStatus.UNKNOWN:
            raise RightsPolicyViolationError(
                f"Recipe '{raw.get('title')}' rejected: source rights status is UNKNOWN"
            )

        # Validate verification status
        verified = bool(raw.get("verified", False))
        verif_raw = raw.get("verification_status", VerificationStatus.REVIEW_REQUIRED.value)
        if not verified or verif_raw != VerificationStatus.VERIFIED.value:
            raise IngestionValidationError(
                f"Recipe '{raw.get('title')}' rejected: unverified recipes cannot be ingested"
            )

        author_id = str(raw["author_id"]).strip()
        source_id = str(raw["source_id"]).strip()
        title = str(raw["title"]).strip()
        norm_title = normalize_title(title)
        source_locator = raw.get("source_locator")

        recipe_id = generate_recipe_id(author_id, source_id, source_locator, norm_title)

        # Normalize ingredients
        raw_ingredients = raw.get("ingredients", [])
        normalized_ingredients: list[dict[str, Any]] = []
        for pos, item in enumerate(raw_ingredients, 1):
            disp_name = str(item.get("display_name", "")).strip()
            ing_id = item.get("ingredient_id") or normalize_ingredient_id(disp_name)
            qty_raw = item.get("quantity")
            qty = Decimal(str(qty_raw)) if qty_raw is not None and str(qty_raw).strip() != "" else None
            unit = normalize_unit(item.get("unit"))
            normalized_ingredients.append({
                "id": f"{recipe_id}:{pos}",
                "ingredient_id": ing_id,
                "display_name": disp_name,
                "quantity": qty,
                "unit": unit,
                "optional": bool(item.get("optional", False)),
                "preparation_note": item.get("preparation_note"),
                "position": pos,
            })

        # Normalize instructions
        raw_instructions = raw.get("instructions", [])
        normalized_instructions: list[dict[str, Any]] = []
        for pos, ins in enumerate(raw_instructions, 1):
            if isinstance(ins, str):
                normalized_instructions.append({
                    "step_number": pos,
                    "text": ins.strip(),
                    "timing_minutes": None,
                })
            else:
                normalized_instructions.append({
                    "step_number": ins.get("step_number", pos),
                    "text": str(ins.get("text", "")).strip(),
                    "timing_minutes": ins.get("timing_minutes"),
                })

        content_hash = compute_recipe_content_hash(
            author_id, source_id, norm_title, normalized_ingredients, normalized_instructions
        )

        cur = self._conn.cursor()

        # Check existing versions
        cur.execute(
            "SELECT recipe_version, source_content_hash FROM recipes WHERE recipe_id = ? ORDER BY recipe_version DESC LIMIT 1",
            (recipe_id,),
        )
        existing = cur.fetchone()
        version = 1
        if existing:
            existing_ver = existing["recipe_version"]
            existing_hash = existing["source_content_hash"]
            if existing_hash == content_hash:
                # Idempotent re-ingestion: exact duplicate, no change needed
                return recipe_id
            version = existing_ver + 1

        # Meal types
        meal_types = [t.value if isinstance(t, MealType) else str(t) for t in raw.get("meal_types", [MealType.LUNCH.value])]
        tags = list(raw.get("tags", []))
        servings = str(Decimal(str(raw.get("servings", 4))))
        verified = bool(raw.get("verified", True))
        ver_status = raw.get("verification_status", VerificationStatus.VERIFIED.value)
        created_at = raw.get("created_at", "2026-09-21 00:00:00")

        cur.execute(
            "INSERT INTO recipes "
            "(recipe_id, recipe_version, author_id, source_id, title, normalized_title, "
            "meal_types_json, cuisine, servings, prep_minutes, cook_minutes, total_minutes, "
            "difficulty, tags_json, source_locator, source_content_hash, rights_status, "
            "verified, verification_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                recipe_id,
                version,
                author_id,
                source_id,
                title,
                norm_title,
                json.dumps(meal_types),
                raw.get("cuisine"),
                servings,
                raw.get("prep_minutes"),
                raw.get("cook_minutes"),
                raw.get("total_minutes"),
                raw.get("difficulty"),
                json.dumps(tags),
                source_locator,
                content_hash,
                rights.value,
                1 if verified else 0,
                ver_status,
                created_at,
            ),
        )

        # Insert ingredients
        for ing in normalized_ingredients:
            cur.execute(
                "INSERT INTO recipe_ingredients "
                "(id, recipe_id, recipe_version, ingredient_id, display_name, quantity_value, "
                "quantity_unit, optional, preparation_note, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{recipe_id}:{version}:{ing['position']}",
                    recipe_id,
                    version,
                    ing["ingredient_id"],
                    ing["display_name"],
                    str(ing["quantity"]) if ing["quantity"] is not None else None,
                    ing["unit"],
                    1 if ing["optional"] else 0,
                    ing["preparation_note"],
                    ing["position"],
                ),
            )

        # Insert instructions
        for ins in normalized_instructions:
            cur.execute(
                "INSERT INTO recipe_instructions "
                "(id, recipe_id, recipe_version, step_number, text, timing_minutes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"{recipe_id}:{version}:{ins['step_number']}",
                    recipe_id,
                    version,
                    ins["step_number"],
                    ins["text"],
                    ins["timing_minutes"],
                ),
            )

        # FTS indexing
        cur.execute(
            "INSERT INTO recipes_fts (recipe_id, title, tags) VALUES (?, ?, ?)",
            (recipe_id, title, " ".join(tags)),
        )

        self._conn.commit()
        return recipe_id

    def finalize_catalog(self) -> str:
        """Compute final content hash and update metadata."""
        cur = self._conn.cursor()
        cur.execute("SELECT count(*) FROM recipes")
        recipe_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM recipes WHERE verified = 1 AND verification_status = 'VERIFIED'")
        verified_count = cur.fetchone()[0]

        final_hash = compute_catalog_content_hash(self._conn)

        meta = [
            ("schema_version", str(CATALOG_SCHEMA_VERSION)),
            ("build_id", self._build_id),
            ("content_hash", final_hash),
            ("recipe_count", str(recipe_count)),
            ("verified_recipe_count", str(verified_count)),
            ("created_at", "2026-09-21 00:00:00"),
        ]
        cur.executemany("INSERT OR REPLACE INTO catalog_metadata(key, value) VALUES (?, ?)", meta)
        self._conn.commit()
        return final_hash
