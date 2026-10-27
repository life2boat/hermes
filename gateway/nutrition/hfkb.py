"""
gateway.nutrition.hfkb — HealBite Food Knowledge Base (HFKB) Storage & Query Engine.

Provides authoritative SQLite-based storage and fast indexed lookup for:
1. Multi-dataset nutritional profiles (USDA SR Legacy, USDA Foundation, Fineli, Skurikhin).
2. Per-item provenance (source_id, dataset_version, source_provenance, cooking_state).
3. Rich Russian aliases, inflections, and culinary preparation states.
4. Standard serving and portion weight conversions.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway.healbite_food_composition import FoodLookupResult, FoodNutrientProfile
from gateway.nutrition.search import (
    extract_stemmed_tokens,
    match_query_against_aliases,
    normalize_russian_food_text,
)

logger = logging.getLogger(__name__)

HFKB_SCHEMA_VERSION = "hfkb_v2"

_CREATE_HFKB_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS foods (
    food_id TEXT PRIMARY KEY,
    name_ru TEXT NOT NULL,
    name_en TEXT,
    category TEXT DEFAULT 'general',
    cooking_state TEXT DEFAULT 'raw',
    source_dataset TEXT NOT NULL,
    source_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    source_provenance TEXT NOT NULL,
    edible_portion_basis TEXT DEFAULT 'per 100g',
    reference_confidence REAL DEFAULT 0.90,
    variance_note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS nutrients (
    food_id TEXT PRIMARY KEY,
    calories_kcal REAL NOT NULL,
    protein_g REAL NOT NULL,
    fat_g REAL NOT NULL,
    carbs_g REAL NOT NULL,
    fiber_g REAL DEFAULT 0.0,
    sugars_g REAL DEFAULT 0.0,
    FOREIGN KEY (food_id) REFERENCES foods(food_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS food_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    food_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    alias_type TEXT DEFAULT 'synonym',
    FOREIGN KEY (food_id) REFERENCES foods(food_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS food_portions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    food_id TEXT NOT NULL,
    portion_name TEXT NOT NULL,
    grams REAL NOT NULL,
    FOREIGN KEY (food_id) REFERENCES foods(food_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_foods_name_ru ON foods(name_ru);
CREATE INDEX IF NOT EXISTS idx_foods_source_id ON foods(source_id);
CREATE INDEX IF NOT EXISTS idx_food_aliases_normalized ON food_aliases(normalized_alias);
CREATE INDEX IF NOT EXISTS idx_food_aliases_food_id ON food_aliases(food_id);
CREATE INDEX IF NOT EXISTS idx_food_portions_food_id ON food_portions(food_id);
"""


@dataclass(frozen=True, slots=True)
class HFKBFoodItem:
    food_id: str
    name_ru: str
    name_en: str = ""
    category: str = "general"
    cooking_state: str = "raw"
    source_dataset: str = ""
    source_id: str = ""
    dataset_version: str = "2026.2"
    source_provenance: str = ""
    edible_portion_basis: str = "per 100g"
    reference_confidence: float = 0.90
    variance_note: str = ""
    calories_kcal: float = 0.0
    protein_g: float = 0.0
    fat_g: float = 0.0
    carbs_g: float = 0.0
    fiber_g: float = 0.0
    sugars_g: float = 0.0
    aliases: tuple[str, ...] = ()
    portions: tuple[tuple[str, float], ...] = ()

    @property
    def canonical_name(self) -> str:
        return self.name_ru

    @property
    def preparation_state(self) -> str:
        return self.cooking_state

    @property
    def nutrient_profile(self) -> HFKBFoodItem:
        return self


@dataclass(frozen=True, slots=True)
class HFKBSearchResult:
    best_item: HFKBFoodItem | None
    confidence: float
    matched: bool
    candidate_suggestions: tuple[str, ...] = ()
    match_type: str = "none"


def hfkb_item_to_nutrient_profile(item: HFKBFoodItem) -> FoodNutrientProfile:
    """Converts HFKBFoodItem to the canonical FoodNutrientProfile contract."""
    return FoodNutrientProfile(
        canonical_id=item.food_id,
        display_name=item.name_ru,
        category=item.category,
        calories_kcal=item.calories_kcal,
        protein_g=item.protein_g,
        fat_g=item.fat_g,
        carbs_g=item.carbs_g,
        reference_confidence=item.reference_confidence,
        source_id=item.source_id,
        dataset_version=item.dataset_version,
        cooking_state=item.cooking_state,
        edible_portion_basis=item.edible_portion_basis,
        variance_note=item.variance_note,
        source_provenance=item.source_provenance,
        aliases=item.aliases,
    )


class HFKBCatalog:
    """
    SQLite-backed HealBite Food Knowledge Base reader and search index.
    """

    def __init__(self, db_path: Path | str, *, read_only: bool = True) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        self._lock = threading.Lock()
        self._alias_map: dict[str, str] = {}
        self._id_to_name: dict[str, str] = {}
        self._id_to_stemmed_tokens: dict[str, tuple[str, ...]] = {}
        self._food_cache: dict[str, HFKBFoodItem] = {}
        self._is_indexed = False

    @property
    def is_open(self) -> bool:
        return self.db_path.exists()

    def connect(self) -> sqlite3.Connection:
        if self.read_only:
            uri = f"file:{self.db_path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @classmethod
    def initialize_database(cls, db_path: Path | str) -> HFKBCatalog:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(_CREATE_HFKB_TABLES_SQL)
            conn.commit()
        finally:
            conn.close()
        return cls(path, read_only=False)

    def insert_food_item(self, item: HFKBFoodItem, conn: sqlite3.Connection | None = None) -> None:
        """Inserts food record, nutrients, aliases, and portions into HFKB database."""
        owns_conn = conn is None
        c = conn if conn is not None else sqlite3.connect(self.db_path)
        try:
            c.execute(
                """
                INSERT OR REPLACE INTO foods (
                    food_id, name_ru, name_en, category, cooking_state,
                    source_dataset, source_id, dataset_version, source_provenance,
                    edible_portion_basis, reference_confidence, variance_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.food_id,
                    item.name_ru,
                    item.name_en,
                    item.category,
                    item.cooking_state,
                    item.source_dataset,
                    item.source_id,
                    item.dataset_version,
                    item.source_provenance,
                    item.edible_portion_basis,
                    item.reference_confidence,
                    item.variance_note,
                ),
            )
            c.execute(
                """
                INSERT OR REPLACE INTO nutrients (
                    food_id, calories_kcal, protein_g, fat_g, carbs_g, fiber_g, sugars_g
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.food_id,
                    item.calories_kcal,
                    item.protein_g,
                    item.fat_g,
                    item.carbs_g,
                    item.fiber_g,
                    item.sugars_g,
                ),
            )
            # Add self name as default alias
            all_aliases = set(item.aliases) | {item.name_ru}
            for alias in all_aliases:
                norm_alias = normalize_russian_food_text(alias)
                if norm_alias and norm_alias not in ("орехи", "орех"):
                    c.execute(
                        """
                        INSERT INTO food_aliases (food_id, alias, normalized_alias, alias_type)
                        VALUES (?, ?, ?, 'synonym')
                        """,
                        (item.food_id, alias, norm_alias),
                    )
            for portion_name, grams in item.portions:
                c.execute(
                    """
                    INSERT INTO food_portions (food_id, portion_name, grams)
                    VALUES (?, ?, ?)
                    """,
                    (item.food_id, portion_name, grams),
                )
            if owns_conn:
                c.commit()
        finally:
            if owns_conn:
                c.close()

    def build_in_memory_index(self) -> None:
        """Loads fast search indexes into memory."""
        with self._lock:
            if not self.db_path.exists():
                logger.warning("HFKB database does not exist: %s", self.db_path)
                return

            alias_map: dict[str, str] = {}
            id_to_name: dict[str, str] = {}
            id_to_stemmed_tokens: dict[str, tuple[str, ...]] = {}

            with self.connect() as conn:
                food_rows = conn.execute("SELECT food_id, name_ru FROM foods").fetchall()
                for row in food_rows:
                    fid = str(row["food_id"])
                    name = str(row["name_ru"])
                    id_to_name[fid] = name
                    stems = extract_stemmed_tokens(name)
                    if stems:
                        id_to_stemmed_tokens[fid] = stems

                alias_rows = conn.execute("SELECT food_id, normalized_alias FROM food_aliases").fetchall()
                for row in alias_rows:
                    fid = str(row["food_id"])
                    norm = str(row["normalized_alias"])
                    if norm and norm not in alias_map:
                        alias_map[norm] = fid

            self._alias_map = alias_map
            self._id_to_name = id_to_name
            self._id_to_stemmed_tokens = id_to_stemmed_tokens
            self._is_indexed = True

    def get_food_by_id(self, food_id: str) -> HFKBFoodItem | None:
        """Loads complete HFKBFoodItem by canonical food_id."""
        if food_id in self._food_cache:
            return self._food_cache[food_id]

        if not self.db_path.exists():
            return None

        with self.connect() as conn:
            food_row = conn.execute(
                "SELECT * FROM foods WHERE food_id = ?",
                (food_id,),
            ).fetchone()
            if not food_row:
                return None

            nutr_row = conn.execute(
                "SELECT * FROM nutrients WHERE food_id = ?",
                (food_id,),
            ).fetchone()

            alias_rows = conn.execute(
                "SELECT alias FROM food_aliases WHERE food_id = ?",
                (food_id,),
            ).fetchall()
            aliases = tuple(str(r["alias"]) for r in alias_rows)

            portion_rows = conn.execute(
                "SELECT portion_name, grams FROM food_portions WHERE food_id = ?",
                (food_id,),
            ).fetchall()
            portions = tuple((str(r["portion_name"]), float(r["grams"])) for r in portion_rows)

            item = HFKBFoodItem(
                food_id=str(food_row["food_id"]),
                name_ru=str(food_row["name_ru"]),
                name_en=str(food_row["name_en"] or ""),
                category=str(food_row["category"] or "general"),
                cooking_state=str(food_row["cooking_state"] or "raw"),
                source_dataset=str(food_row["source_dataset"]),
                source_id=str(food_row["source_id"]),
                dataset_version=str(food_row["dataset_version"]),
                source_provenance=str(food_row["source_provenance"]),
                edible_portion_basis=str(food_row["edible_portion_basis"] or "per 100g"),
                reference_confidence=float(food_row["reference_confidence"] or 0.90),
                variance_note=str(food_row["variance_note"] or ""),
                calories_kcal=float(nutr_row["calories_kcal"]) if nutr_row else 0.0,
                protein_g=float(nutr_row["protein_g"]) if nutr_row else 0.0,
                fat_g=float(nutr_row["fat_g"]) if nutr_row else 0.0,
                carbs_g=float(nutr_row["carbs_g"]) if nutr_row else 0.0,
                fiber_g=float(nutr_row["fiber_g"]) if nutr_row else 0.0,
                sugars_g=float(nutr_row["sugars_g"]) if nutr_row else 0.0,
                aliases=aliases,
                portions=portions,
            )
            self._food_cache[food_id] = item
            return item

    def resolve(
        self,
        query: str,
        preferred_cooking_state: str | None = None,
    ) -> FoodLookupResult:
        """
        Resolves a food name query into a deterministic FoodLookupResult.
        """
        norm_q = normalize_russian_food_text(query)
        if norm_q in ("орехи", "орех"):
            return FoodLookupResult(
                matched=False,
                confidence=0.0,
                match_type="ambiguous_category",
                candidate_suggestions=("грецкий орех", "миндаль", "фундук", "кешью", "арахис"),
            )

        if not self._is_indexed:
            self.build_in_memory_index()

        target_id, conf, match_type, suggestions = match_query_against_aliases(
            query=query,
            alias_map=self._alias_map,
            id_to_name=self._id_to_name,
            id_to_stemmed_tokens=self._id_to_stemmed_tokens,
            fuzzy_threshold=0.85,
        )

        if target_id is None:
            return FoodLookupResult(
                matched=False,
                confidence=0.0,
                match_type="none",
                candidate_suggestions=suggestions,
            )

        item = self.get_food_by_id(target_id)
        if item is None:
            return FoodLookupResult(
                matched=False,
                confidence=0.0,
                match_type="none",
            )

        profile = hfkb_item_to_nutrient_profile(item)
        final_confidence = round(conf * profile.reference_confidence, 2)

        return FoodLookupResult(
            matched=True,
            profile=profile,
            confidence=final_confidence,
            match_type=match_type,
            candidate_suggestions=suggestions,
            source_id=profile.source_id,
            dataset_version=profile.dataset_version,
            cooking_state=profile.cooking_state,
            source_provenance=profile.source_provenance,
        )

    def search(self, query: str, limit: int = 5) -> HFKBSearchResult:
        """Compatibility search method returning HFKBSearchResult."""
        res = self.resolve(query)
        if res.matched and res.profile:
            item = self.get_food_by_id(res.profile.canonical_id)
            return HFKBSearchResult(
                best_item=item,
                confidence=res.confidence,
                matched=True,
                candidate_suggestions=tuple(res.candidate_suggestions[:limit]),
                match_type=res.match_type,
            )
        return HFKBSearchResult(
            best_item=None,
            confidence=0.0,
            matched=False,
            candidate_suggestions=tuple(res.candidate_suggestions[:limit]),
            match_type="none",
        )

    def count_records(self) -> int:
        if not self.db_path.exists():
            return 0
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM foods").fetchone()
            return int(row["cnt"]) if row else 0


_GLOBAL_HFKB: HFKBCatalog | None = None
_GLOBAL_HFKB_LOCK = threading.Lock()


class CatalogVerificationError(RuntimeError):
    """Raised when strict catalog verification fails (checksum mismatch, corruption, missing manifest)."""
    pass


def _resolve_catalog_path(explicit_path: Path | str | None = None) -> Path:
    if explicit_path is not None:
        return Path(explicit_path)

    # Search candidates in priority order:
    # 1. Working directory
    # 2. Repository root (two levels above gateway/)
    # 3. gateway/nutrition/data/
    # 4. Fallback to nutrition_catalog_v2.db in CWD
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        Path("nutrition_catalog_v2.db"),
        repo_root / "nutrition_catalog_v2.db",
        Path(__file__).resolve().parent / "data" / "nutrition_catalog_v2.db",
        Path.cwd() / "nutrition_catalog_v2.db",
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return Path("nutrition_catalog_v2.db")


def load_verified_hfkb(
    catalog_path: Path | str | None = None,
    manifest_path: Path | str | None = None,
    *,
    strict: bool = True,
) -> HFKBCatalog:
    """
    Loads and verifies the HFKB catalog against its manifest and SQLite integrity.

    In strict mode (default):
    - Checks manifest existence, schema, and declared SHA-256 checksum.
    - Runs SQLite PRAGMA integrity_check.
    - Fails closed by raising CatalogVerificationError if the catalog or manifest
      is missing, corrupt, altered by even 1 byte, or fails integrity.

    In non-strict mode:
    - Verifies when manifest is present; if manifest is missing or in dev environment,
      falls back gracefully to unverified catalog if it exists, or raises FileNotFoundError.
    """
    from gateway.nutrition.manifest import load_and_verify_catalog_binding

    cat_path = _resolve_catalog_path(catalog_path)
    man_path = Path(manifest_path) if manifest_path is not None else None

    if not cat_path.exists():
        if strict:
            raise CatalogVerificationError(f"Catalog file not found: {cat_path}")
        raise FileNotFoundError(f"Catalog file not found: {cat_path}")

    binding = load_and_verify_catalog_binding(cat_path, man_path)

    if strict:
        if not binding.is_verified:
            raise CatalogVerificationError(
                f"Strict catalog verification failed: {binding.verification_error or 'Unknown verification failure'}"
            )
        catalog = HFKBCatalog(binding.catalog_path, read_only=True)
        catalog.build_in_memory_index()
        return catalog

    # Non-strict mode
    if binding.is_verified:
        catalog = HFKBCatalog(binding.catalog_path, read_only=True)
        catalog.build_in_memory_index()
        return catalog

    logger.warning("Loading unverified HFKB catalog in non-strict mode: %s", binding.verification_error)
    catalog = HFKBCatalog(cat_path, read_only=True)
    catalog.build_in_memory_index()
    return catalog


def get_default_hfkb(catalog_path: Path | str | None = None) -> HFKBCatalog | None:
    """Returns or initializes default HFKB catalog if the database exists (with graceful fallback in non-strict mode)."""
    global _GLOBAL_HFKB
    with _GLOBAL_HFKB_LOCK:
        if _GLOBAL_HFKB is not None:
            return _GLOBAL_HFKB
        target_path = _resolve_catalog_path(catalog_path)
        if target_path.exists():
            try:
                catalog = load_verified_hfkb(target_path, strict=False)
                _GLOBAL_HFKB = catalog
                return _GLOBAL_HFKB
            except Exception as exc:
                logger.warning("Failed to load default HFKB catalog: %s", exc)
                return None
        return None


def set_default_hfkb(catalog: HFKBCatalog | None) -> None:
    """Explicitly sets or clears global default HFKB instance."""
    global _GLOBAL_HFKB
    with _GLOBAL_HFKB_LOCK:
        _GLOBAL_HFKB = catalog
