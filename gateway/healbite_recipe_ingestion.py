from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from gateway.healbite_recipe_catalog_domain import (
    CommercialReuseStatus,
    ContentScope,
    MealType,
    Recipe,
    RecipeAuthor,
    RecipeIngredient,
    RecipeInstruction,
    RecipeSource,
    RightsClearanceScope,
    RightsEvidenceType,
    RightsStatus,
    SourceType,
    TargetRightsScope,
    TranslationRightsStatus,
    VerificationStatus,
    generate_recipe_id,
    load_target_rights_scope,
    normalize_ingredient_id,
    normalize_title,
    normalize_unit,
)
from gateway.healbite_recipe_catalog_store import (
    CATALOG_SCHEMA_VERSION,
    HealBiteRecipeCatalogStore,
    compute_catalog_content_hash,
    initialize_empty_catalog,
)


class IngestionError(Exception):
    pass


class RightsPolicyViolationError(IngestionError):
    pass


class IngestionValidationError(IngestionError):
    pass


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    exact_duplicates: tuple[str, ...]
    normalized_duplicates: tuple[str, ...]
    conflicting_variants: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthorCoverage:
    author_id: str
    display_name: str
    metadata_sources: int
    structured_authorized_sources: int
    verified_recipes: int
    blocked_sources: int
    block_reasons: tuple[str, ...]
    breakfast_verified: int
    lunch_verified: int
    dinner_verified: int
    unique_verified_recipes: int
    can_form_21_meal_week: bool
    production_canary_eligible: bool = False


@dataclass(frozen=True, slots=True)
class CatalogReadinessReport:
    schema_version: int
    build_id: str
    content_hash: str
    recipe_count: int
    verified_recipe_count: int
    real_verified_recipes: int
    test_fixture_recipes: int
    breakfast_verified: int
    lunch_verified: int
    dinner_verified: int
    unique_verified_recipes: int
    can_form_21_meal_week: bool
    author_coverages: dict[str, AuthorCoverage]
    production_canary_eligible: bool = False


DEFAULT_BLOCKED_REASONS: dict[str, str] = {
    "AUTHOR_POKHLEBKIN": (
        "Copyright protected under Russian Civil Code Art. 1281 (term expires 2071); "
        "LINK_ONLY metadata citation only; no structured recipe content license."
    ),
    "AUTHOR_JAMIE_OLIVER": (
        "Copyright protected under UK CDPA 1988 (living author); "
        "LINK_ONLY metadata citation only; no structured recipe content license."
    ),
    "AUTHOR_ESCOFFIER": (
        "BnF Gallica scan ark:/12148/bpt6k65768837 commercial reuse requires separate licensing agreement; "
        "retained as METADATA_ONLY citation; structured recipes cleared via SRC_ESCOFFIER_1903_COMMONS."
    ),
}

PROTECTED_REAL_AUTHORS: frozenset[str] = frozenset({
    "AUTHOR_POKHLEBKIN",
    "AUTHOR_ESCOFFIER",
    "AUTHOR_JAMIE_OLIVER",
})


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


def compute_semantic_corpus_hash(recipes: Sequence[Mapping[str, Any]]) -> str:
    """Deterministically compute SHA-256 over normalized recipe representations for reproducibility."""
    hasher = hashlib.sha256()
    sorted_recipes = sorted(
        recipes,
        key=lambda r: (
            str(r.get("author_id", "")),
            str(r.get("source_id", "")),
            str(r.get("source_locator", "")),
            str(r.get("title", "")),
        ),
    )
    for r in sorted_recipes:
        author_id = str(r.get("author_id", "")).strip()
        source_id = str(r.get("source_id", "")).strip()
        source_locator = str(r.get("source_locator", "")).strip()
        title = str(r.get("title", "")).strip()
        norm_title = normalize_title(title)
        hasher.update(f"{author_id}|{source_id}|{source_locator}|{norm_title}".encode("utf-8"))
        for ing in r.get("ingredients", []):
            d_name = str(ing.get("display_name", "")).strip()
            i_id = ing.get("ingredient_id") or normalize_ingredient_id(d_name)
            qty = str(ing.get("quantity", ""))
            unit = normalize_unit(ing.get("unit"))
            hasher.update(f"{i_id}|{qty}|{unit}".encode("utf-8"))
        for step in r.get("instructions", []):
            text = step if isinstance(step, str) else step.get("text", "")
            hasher.update(text.strip().encode("utf-8"))
    return hasher.hexdigest()


class RecipeIngestionPipeline:
    def __init__(self, db_path: str | Path, *, build_id: str = "build-1") -> None:
        self._path = Path(db_path)
        self._build_id = build_id
        if not self._path.exists():
            initialize_empty_catalog(self._path, build_id=build_id)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._exact_duplicates: list[str] = []
        self._normalized_duplicates: list[str] = []
        self._conflicting_variants: list[str] = []

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
        # 1. Reject UNKNOWN rights status
        if source.rights_status is RightsStatus.UNKNOWN:
            raise RightsPolicyViolationError(f"Cannot ingest source '{source.source_id}' with UNKNOWN rights status")

        # 2. PUBLIC_DOMAIN requires recorded evidence type and locator
        if source.rights_status is RightsStatus.PUBLIC_DOMAIN:
            if not source.rights_evidence_locator or source.rights_evidence_type is RightsEvidenceType.NONE:
                raise RightsPolicyViolationError(
                    f"PUBLIC_DOMAIN source '{source.source_id}' requires recorded rights evidence type and locator"
                )

        # 3. LICENSED requires recorded evidence type and locator
        if source.rights_status is RightsStatus.LICENSED:
            if not source.rights_evidence_locator or source.rights_evidence_type is RightsEvidenceType.NONE:
                raise RightsPolicyViolationError(
                    f"LICENSED source '{source.source_id}' requires recorded license evidence locator and type"
                )

        # 4. Check commercial reuse terms and production rights
        if source.commercial_reuse_status is CommercialReuseStatus.LICENSE_REQUIRED:
            if source.production_rights_approved:
                raise RightsPolicyViolationError(
                    f"Source '{source.source_id}' requires commercial license; cannot have production_rights_approved=True without verified commercial license"
                )

        if source.production_rights_approved:
            if source.rights_status not in (
                RightsStatus.PUBLIC_DOMAIN,
                RightsStatus.LICENSED,
                RightsStatus.USER_PROVIDED_AUTHORIZED,
            ):
                raise RightsPolicyViolationError(
                    f"Source '{source.source_id}' cannot have production rights approved with rights status '{source.rights_status.value}'"
                )
            if source.translation_status not in (
                TranslationRightsStatus.ORIGINAL_LANGUAGE,
                TranslationRightsStatus.PUBLIC_DOMAIN,
            ):
                raise RightsPolicyViolationError(
                    f"Source '{source.source_id}' translation rights must be cleared (ORIGINAL_LANGUAGE or PUBLIC_DOMAIN) for production approval"
                )
            if source.commercial_reuse_status not in (
                CommercialReuseStatus.PUBLIC_DOMAIN,
                CommercialReuseStatus.COMMERCIAL_ALLOWED,
                CommercialReuseStatus.PERMITTED,
            ):
                raise RightsPolicyViolationError(
                    f"Source '{source.source_id}' commercial reuse terms must be PUBLIC_DOMAIN, COMMERCIAL_ALLOWED, or PERMITTED for production approval"
                )
            if source.rights_clearance_scope is None:
                raise RightsPolicyViolationError(
                    f"Source '{source.source_id}' cannot have production rights approved without explicit rights_clearance_scope"
                )
            if not source.rights_clearance_scope.approved_jurisdictions:
                raise RightsPolicyViolationError(
                    f"Source '{source.source_id}' cannot have production rights approved with empty approved_jurisdictions in rights_clearance_scope"
                )
            if not source.rights_clearance_scope.commercial_use_allowed:
                raise RightsPolicyViolationError(
                    f"Source '{source.source_id}' cannot have production rights approved when rights_clearance_scope.commercial_use_allowed is False"
                )
            # Exact edition provenance closure
            edition_str = (source.edition or "").strip()
            if not edition_str:
                raise RightsPolicyViolationError(
                    f"Production approved source '{source.source_id}' must specify an explicit edition statement"
                )
            if "/" in edition_str or "1903/1907" in edition_str or "1903/1907" in (source.edition_basis or ""):
                raise RightsPolicyViolationError(
                    f"Production approved source '{source.source_id}' cannot have composite or ambiguous edition '{source.edition}'"
                )
            if source.publication_year and "(" in edition_str and ")" in edition_str:
                year_match = re.search(r"\((\d{4})\)", edition_str)
                if year_match and int(year_match.group(1)) != source.publication_year:
                    raise RightsPolicyViolationError(
                        f"Production approved source '{source.source_id}' publication year {source.publication_year} contradicts edition year {year_match.group(1)}"
                    )

        scope_json = (
            json.dumps(source.rights_clearance_scope.to_dict())
            if source.rights_clearance_scope is not None
            else None
        )

        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO recipe_sources "
            "(source_id, author_id, title, source_type, source_locator, publication_year, "
            "edition, language, rights_status, rights_evidence_type, rights_evidence_locator, "
            "rights_evidence_note, source_content_hash, ingestion_timestamp, ingestion_tool_version, "
            "content_scope, verification_status, created_at, "
            "underlying_work_rights, digital_reproduction_rights, digital_reproduction_reuse_terms, commercial_reuse_status, "
            "transcription_source, transcription_rights, partner_institution_terms, target_jurisdiction_status, translation_status, "
            "jurisdiction_basis, edition_basis, canonical_url, production_rights_approved, rights_clearance_scope_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source.source_id,
                source.author_id,
                source.title,
                source.source_type.value,
                source.source_locator,
                source.publication_year,
                source.edition,
                source.language,
                source.rights_status.value,
                source.rights_evidence_type.value,
                source.rights_evidence_locator,
                source.rights_evidence_note,
                source.source_content_hash,
                source.ingestion_timestamp or "2026-09-21 00:00:00",
                source.ingestion_tool_version,
                source.content_scope.value,
                source.verification_status.value,
                source.created_at or "2026-09-21 00:00:00",
                source.underlying_work_rights,
                source.digital_reproduction_rights,
                source.digital_reproduction_reuse_terms,
                source.commercial_reuse_status.value,
                source.transcription_source,
                source.transcription_rights,
                source.partner_institution_terms,
                source.target_jurisdiction_status,
                source.translation_status.value,
                source.jurisdiction_basis,
                source.edition_basis,
                source.canonical_url,
                1 if source.production_rights_approved else 0,
                scope_json,
            ),
        )
        self._conn.commit()


    def ingest_recipe(self, raw: Mapping[str, Any]) -> str:
        # 1. Validate recipe rights status
        rights_raw = raw.get("rights_status", RightsStatus.UNKNOWN.value)
        try:
            rights = RightsStatus(rights_raw)
        except ValueError:
            rights = RightsStatus.UNKNOWN

        if rights is RightsStatus.UNKNOWN:
            raise RightsPolicyViolationError(
                f"Recipe '{raw.get('title')}' rejected: rights status is UNKNOWN"
            )

        # 2. Validate recipe verification status
        verified = bool(raw.get("verified", False))
        verif_raw = raw.get("verification_status", VerificationStatus.REVIEW_REQUIRED.value)
        if not verified or verif_raw != VerificationStatus.VERIFIED.value:
            raise IngestionValidationError(
                f"Recipe '{raw.get('title')}' rejected: unverified recipes cannot be ingested"
            )

        # 3. Reject synthetic recipes attributed to real authors
        author_id = str(raw["author_id"]).strip()
        if author_id in PROTECTED_REAL_AUTHORS:
            if (
                raw.get("is_synthetic", False)
                or "TEST" in str(raw.get("source_id", ""))
                or rights is RightsStatus.USER_PROVIDED_AUTHORIZED
            ):
                raise RightsPolicyViolationError(
                    f"Synthetic recipes cannot be attributed to {author_id}"
                )

        # 4. Source lookup and rights validation
        source_id = str(raw.get("source_id", "")).strip()
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM recipe_sources WHERE source_id = ?", (source_id,))
        source_row = cur.fetchone()
        if not source_row:
            raise IngestionValidationError(
                f"Recipe '{raw.get('title')}' rejected: source '{source_id}' not found in catalog"
            )

        source_rights = RightsStatus(source_row["rights_status"])
        if source_rights is RightsStatus.UNKNOWN:
            raise RightsPolicyViolationError(
                f"Recipe '{raw.get('title')}' rejected: source '{source_id}' has UNKNOWN rights status"
            )

        source_keys = set(source_row.keys())
        content_scope = source_row["content_scope"] if "content_scope" in source_keys else "METADATA_ONLY"
        if source_rights is RightsStatus.LINK_ONLY or content_scope == ContentScope.METADATA_ONLY.value:
            raise RightsPolicyViolationError(
                f"Recipe '{raw.get('title')}' rejected: source '{source_id}' is LINK_ONLY / METADATA_ONLY and cannot contain recipe content"
            )

        source_ver_status = source_row["verification_status"] if "verification_status" in source_keys else VerificationStatus.VERIFIED.value
        source_trans_status = source_row["translation_status"] if "translation_status" in source_keys else TranslationRightsStatus.UNKNOWN.value
        source_comm_status = source_row["commercial_reuse_status"] if "commercial_reuse_status" in source_keys else CommercialReuseStatus.UNKNOWN.value
        source_prod_approved = bool(source_row["production_rights_approved"]) if "production_rights_approved" in source_keys else False

        if source_ver_status == VerificationStatus.REVIEW_REQUIRED.value:
            raise RightsPolicyViolationError(
                f"Recipe '{raw.get('title')}' rejected: source '{source_id}' is REVIEW_REQUIRED; structured content is not planning-eligible"
            )
        if source_trans_status == TranslationRightsStatus.UNKNOWN.value:
            raise RightsPolicyViolationError(
                f"Recipe '{raw.get('title')}' rejected: source '{source_id}' has UNKNOWN translation rights; structured content is not planning-eligible"
            )

        # 5. Recipe author != source author check
        if source_row["author_id"] != author_id:
            raise IngestionValidationError(
                f"Recipe author '{author_id}' does not match source author '{source_row['author_id']}'"
            )

        # 6. Missing source locator check
        source_locator = raw.get("source_locator")
        source_type_val = source_row["source_type"]
        if source_type_val == SourceType.BOOK.value and (source_locator is None or not str(source_locator).strip()):
            raise IngestionValidationError(
                f"Recipe '{raw.get('title')}' requires explicit source_locator (page, chapter, or section citation)"
            )

        # 7. Source content hash verification if supplied
        if raw.get("source_content_hash") and raw["source_content_hash"] != source_row["source_content_hash"]:
            raise IngestionValidationError(
                f"Source content hash mismatch: source modified after recorded provenance "
                f"(expected {source_row['source_content_hash']}, got {raw.get('source_content_hash')})"
            )

        title = str(raw["title"]).strip()
        source_recipe_title = str(raw.get("source_recipe_title", title)).strip()
        norm_title = normalize_title(title)

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

        normalized_hash = compute_recipe_content_hash(
            author_id, source_id, norm_title, normalized_ingredients, normalized_instructions
        )
        source_hash = raw.get("source_content_hash") or normalized_hash

        # Check existing versions & duplicate contract
        cur.execute(
            "SELECT recipe_version, source_content_hash, normalized_content_hash, title "
            "FROM recipes WHERE recipe_id = ? ORDER BY recipe_version DESC LIMIT 1",
            (recipe_id,),
        )
        existing = cur.fetchone()
        version = 1
        if existing:
            existing_ver = existing["recipe_version"]
            existing_hash = existing["normalized_content_hash"] or existing["source_content_hash"]
            if existing_hash == normalized_hash:
                if existing["title"] == title:
                    self._exact_duplicates.append(f"{recipe_id}:{title}")
                else:
                    self._normalized_duplicates.append(f"{recipe_id}:{title}")
                # Idempotent re-ingestion: exact/normalized duplicate
                return recipe_id

            if raw.get("is_conflicting_variant", False):
                self._conflicting_variants.append(f"{recipe_id}:{title}")
                raise IngestionValidationError(
                    f"Conflicting variant detected for recipe '{title}' ({recipe_id}); explicit resolution required"
                )

            version = existing_ver + 1

        # Meal types
        meal_types = [
            t.value if isinstance(t, MealType) else str(t)
            for t in raw.get("meal_types", [MealType.LUNCH.value])
        ]
        tags = list(raw.get("tags", []))
        servings = str(Decimal(str(raw.get("servings", 4))))
        ver_status = raw.get("verification_status", VerificationStatus.VERIFIED.value)
        created_at = raw.get("created_at", "2026-09-21 00:00:00")

        source_scope_raw = (
            source_row["rights_clearance_scope_json"]
            if "rights_clearance_scope_json" in source_keys
            else None
        )
        try:
            source_scope_data = json.loads(source_scope_raw) if source_scope_raw else None
        except Exception:
            source_scope_data = None
        source_scope = RightsClearanceScope.from_dict(source_scope_data) if source_scope_data else None

        production_eligible = (
            source_prod_approved
            and source_scope is not None
            and bool(source_scope.approved_jurisdictions)
            and source_scope.commercial_use_allowed
            and source_comm_status in (
                CommercialReuseStatus.PUBLIC_DOMAIN.value,
                CommercialReuseStatus.COMMERCIAL_ALLOWED.value,
                CommercialReuseStatus.PERMITTED.value,
            )
            and source_trans_status in (TranslationRightsStatus.ORIGINAL_LANGUAGE.value, TranslationRightsStatus.PUBLIC_DOMAIN.value)
            and source_ver_status == VerificationStatus.VERIFIED.value
            and bool(raw.get("production_eligible", True))
        )

        cur.execute(
            "INSERT INTO recipes "
            "(recipe_id, recipe_version, author_id, source_id, title, normalized_title, "
            "source_recipe_title, meal_types_json, cuisine, servings, prep_minutes, "
            "cook_minutes, total_minutes, difficulty, tags_json, source_locator, "
            "source_content_hash, normalized_content_hash, rights_status, verified, "
            "verification_status, ingestion_build_id, created_at, production_eligible, "
            "classification_source, classification_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                recipe_id,
                version,
                author_id,
                source_id,
                title,
                norm_title,
                source_recipe_title,
                json.dumps(meal_types),
                raw.get("cuisine"),
                servings,
                raw.get("prep_minutes"),
                raw.get("cook_minutes"),
                raw.get("total_minutes"),
                raw.get("difficulty"),
                json.dumps(tags),
                source_locator,
                source_hash,
                normalized_hash,
                rights.value,
                1 if verified else 0,
                ver_status,
                self._build_id,
                created_at,
                1 if production_eligible else 0,
                raw.get("classification_source"),
                raw.get("classification_reason"),
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

    def get_duplicate_report(self) -> DuplicateReport:
        return DuplicateReport(
            exact_duplicates=tuple(self._exact_duplicates),
            normalized_duplicates=tuple(self._normalized_duplicates),
            conflicting_variants=tuple(self._conflicting_variants),
        )

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


def calculate_author_coverage(
    store: HealBiteRecipeCatalogStore,
    author_id: str,
    *,
    blocked_reasons_map: Mapping[str, str] | None = None,
    target_rights_scope: str | Sequence[str] | TargetRightsScope | None = None,
) -> AuthorCoverage:
    author = store.get_author(author_id)
    display_name = author.display_name if author else author_id

    sources = store.list_sources_by_author(author_id)
    metadata_sources = len(sources)
    structured_sources = sum(
        1 for s in sources if s.content_scope == ContentScope.STRUCTURED_RECIPE_CONTENT
    )
    blocked_sources = metadata_sources - structured_sources

    reasons_map = blocked_reasons_map or DEFAULT_BLOCKED_REASONS
    block_reasons = (reasons_map.get(author_id, "Source scope is METADATA_ONLY"),) if blocked_sources > 0 else ()

    scope = (
        load_target_rights_scope(target_rights_scope)
        or store.target_rights_scope
    )
    recipes = store.list_recipes_by_author(author_id, verified_only=True, target_rights_scope=scope)
    planning_recipes = [r for r in recipes if r.is_verified_for_planning]
    verified_count = len(planning_recipes)

    breakfast = sum(1 for r in planning_recipes if MealType.BREAKFAST in r.meal_types)
    lunch = sum(1 for r in planning_recipes if MealType.LUNCH in r.meal_types)
    dinner = sum(1 for r in planning_recipes if MealType.DINNER in r.meal_types)
    unique_recipes = len({r.recipe_id for r in planning_recipes})
    can_form = (breakfast >= 7 and lunch >= 7 and dinner >= 7 and unique_recipes >= 21)
    production_canary = (
        verified_count > 0
        and can_form
        and (scope is not None)
        and all(r.is_production_cleared_for_scope(scope) for r in planning_recipes)
    )

    return AuthorCoverage(
        author_id=author_id,
        display_name=display_name,
        metadata_sources=metadata_sources,
        structured_authorized_sources=structured_sources,
        verified_recipes=verified_count,
        blocked_sources=blocked_sources,
        block_reasons=block_reasons,
        breakfast_verified=breakfast,
        lunch_verified=lunch,
        dinner_verified=dinner,
        unique_verified_recipes=unique_recipes,
        can_form_21_meal_week=can_form,
        production_canary_eligible=production_canary,
    )


def calculate_catalog_readiness(
    store: HealBiteRecipeCatalogStore,
    author_ids: Sequence[str] = (
        "AUTHOR_POKHLEBKIN",
        "AUTHOR_ESCOFFIER",
        "AUTHOR_JAMIE_OLIVER",
    ),
    *,
    target_rights_scope: str | Sequence[str] | TargetRightsScope | None = None,
) -> CatalogReadinessReport:
    meta = store.get_metadata()
    scope = (
        load_target_rights_scope(target_rights_scope)
        or store.target_rights_scope
    )
    all_recipes = store.get_all_recipes(verified_only=True, target_rights_scope=scope)
    verified_planning = [r for r in all_recipes if r.is_verified_for_planning]

    real_verified = sum(1 for r in verified_planning if not r.author_id.startswith("TEST_"))
    test_fixture = sum(1 for r in verified_planning if r.author_id.startswith("TEST_"))

    breakfast = sum(1 for r in verified_planning if MealType.BREAKFAST in r.meal_types)
    lunch = sum(1 for r in verified_planning if MealType.LUNCH in r.meal_types)
    dinner = sum(1 for r in verified_planning if MealType.DINNER in r.meal_types)
    unique_recipes = len({r.recipe_id for r in verified_planning})
    can_form = (breakfast >= 7 and lunch >= 7 and dinner >= 7 and unique_recipes >= 21)
    catalog_production_canary = (
        len(verified_planning) > 0
        and can_form
        and (scope is not None)
        and all(r.is_production_cleared_for_scope(scope) for r in verified_planning)
    )

    coverages = {
        aid: calculate_author_coverage(store, aid, target_rights_scope=scope)
        for aid in author_ids
    }

    return CatalogReadinessReport(
        schema_version=meta.schema_version,
        build_id=meta.build_id,
        content_hash=meta.content_hash,
        recipe_count=meta.recipe_count,
        verified_recipe_count=meta.verified_recipe_count,
        real_verified_recipes=real_verified,
        test_fixture_recipes=test_fixture,
        breakfast_verified=breakfast,
        lunch_verified=lunch,
        dinner_verified=dinner,
        unique_verified_recipes=unique_recipes,
        can_form_21_meal_week=can_form,
        author_coverages=coverages,
        production_canary_eligible=catalog_production_canary,
    )
