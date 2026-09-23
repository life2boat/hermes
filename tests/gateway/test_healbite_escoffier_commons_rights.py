"""
Comprehensive rights closure, source provenance, and adversarial validation tests
for the Hermes Escoffier Commons French Corpus v1 (SRC_ESCOFFIER_1903_COMMONS).

Verifies:
1. Complete 10-dimensional rights matrix on SRC_ESCOFFIER_1903_COMMONS.
2. Gutenberg (SRC_ESCOFFIER_1907_EN) and Gallica (SRC_ESCOFFIER_LGC) production blocks.
3. Zero test fixture recipes in real catalog artifact (36/36 authentic French Escoffier).
4. 12 breakfast, 12 lunch, 12 dinner with can_form_21_meal_week=True and production_canary_eligible=True.
5. Explicit classification source and reason tracking (Adversarial meal testing).
6. Tamper fail-closed behavior via cryptographic catalog content hash.
7. Recipe retriever production-only filtering.
8. Migration mapping integrity (36 mapped, 0 unmapped, 0 conflicts).
9. Wikisource cross-check report verification.
10. Raw binary/text scan non-leakage in git repository.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from gateway.healbite_recipe_catalog_domain import (
    AUTHOR_ESCOFFIER,
    AUTHOR_JAMIE_OLIVER,
    AUTHOR_POKHLEBKIN,
    CommercialReuseStatus,
    ContentScope,
    MealType,
    RightsStatus,
    TranslationRightsStatus,
    VerificationStatus,
)
from gateway.healbite_recipe_catalog_store import (
    CatalogIntegrityError,
    HealBiteRecipeCatalogStore,
)
from gateway.healbite_recipe_fixtures import (
    SRC_ESCOFFIER_1903_COMMONS_SOURCE,
    SRC_ESCOFFIER_1907_EN_SOURCE,
    build_escoffier_real_catalog,
)
from gateway.healbite_recipe_retrieval import RecipeRetriever

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ==============================================================================
# 1. RIGHTS MATRIX COMPLETENESS (10 DIMENSIONS)
# ==============================================================================

def test_escoffier_commons_10_rights_dimensions() -> None:
    """SRC_ESCOFFIER_1903_COMMONS must satisfy all 10 production rights requirements."""
    src = SRC_ESCOFFIER_1903_COMMONS_SOURCE

    assert src.source_id == "SRC_ESCOFFIER_1903_COMMONS"
    assert src.author_id == AUTHOR_ESCOFFIER
    assert src.rights_status == RightsStatus.PUBLIC_DOMAIN
    assert src.underlying_work_rights == "PUBLIC_DOMAIN"
    assert src.digital_reproduction_rights == "PUBLIC_DOMAIN_MARKED"
    assert "Public Domain Mark 1.0" in src.digital_reproduction_reuse_terms
    assert src.commercial_reuse_status == CommercialReuseStatus.COMMERCIAL_ALLOWED
    assert src.transcription_source == "OWN_EXTRACTION"
    assert src.transcription_rights == "NOT_APPLICABLE"
    assert "Leeds University Library" in src.partner_institution_terms
    assert "Worldwide Public Domain" in src.target_jurisdiction_status
    assert src.translation_status == TranslationRightsStatus.ORIGINAL_LANGUAGE
    assert src.content_scope == ContentScope.STRUCTURED_RECIPE_CONTENT
    assert src.verification_status == VerificationStatus.VERIFIED
    assert src.production_rights_approved is True
    assert src.source_content_hash == "e030f727e3e28102a02b46a2dc60a8ba342bc150deafbc02fd0d130d52e0dab9"


# ==============================================================================
# 2. GUTENBERG & GALLICA ZERO PRODUCTION LEAKAGE
# ==============================================================================

def test_gutenberg_and_gallica_sources_not_production_approved() -> None:
    """Historical Gutenberg and Gallica sources must remain unapproved for production."""
    # 1. Gutenberg 1907 English source
    assert SRC_ESCOFFIER_1907_EN_SOURCE.production_rights_approved is False
    assert SRC_ESCOFFIER_1907_EN_SOURCE.commercial_reuse_status == CommercialReuseStatus.REVIEW_REQUIRED

    # 2. In catalog store, Gutenberg and Gallica recipes cannot have is_production_cleared
    manifest_path = REPO_ROOT / "recipe_corpus" / "manifests" / "sources_manifest.json"
    manifests = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_map = {s["source_id"]: s for s in manifests}

    assert source_map["SRC_ESCOFFIER_1907_EN"]["production_rights_approved"] is False
    assert source_map["SRC_ESCOFFIER_LGC"]["production_rights_approved"] is False
    assert source_map["SRC_ESCOFFIER_1903_COMMONS"]["production_rights_approved"] is True


def test_zero_production_recipes_from_gutenberg_or_gallica(tmp_path: Path) -> None:
    """Verify production candidate catalog has exactly ZERO recipes backed by Gutenberg or Gallica."""
    db_path = tmp_path / "zero_leak.db"
    build_escoffier_real_catalog(db_path, build_id="zero-leak-test")

    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    all_recipes = store.get_all_recipes(verified_only=True)

    gutenberg_recipes = [r for r in all_recipes if r.source_id == "SRC_ESCOFFIER_1907_EN"]
    gallica_recipes = [r for r in all_recipes if r.source_id == "SRC_ESCOFFIER_LGC"]
    commons_recipes = [r for r in all_recipes if r.source_id == "SRC_ESCOFFIER_1903_COMMONS"]

    assert len(gutenberg_recipes) == 0
    assert len(gallica_recipes) == 0
    assert len(commons_recipes) == 36

    # All production-cleared recipes come exclusively from Commons
    prod_cleared = [r for r in all_recipes if r.is_production_cleared]
    assert len(prod_cleared) == 36
    assert all(r.source_id == "SRC_ESCOFFIER_1903_COMMONS" for r in prod_cleared)


# ==============================================================================
# 3. AUTHENTIC FRENCH ESCOFFIER CATALOG ACCEPTANCE (36/36, 12/12/12)
# ==============================================================================

def test_commons_escoffier_catalog_acceptance(tmp_path: Path) -> None:
    """Pure Commons catalog build contains 36 verified recipes, 0 test fixtures, and canary eligibility."""
    db_path = tmp_path / "acceptance.db"
    rep_dir = tmp_path / "reports"
    content_hash, readiness = build_escoffier_real_catalog(
        db_path,
        build_id="acceptance-test-1",
        reports_dir=rep_dir,
        include_test_fixtures=False,
    )

    assert len(content_hash) == 64
    assert readiness.recipe_count == 36
    assert readiness.verified_recipe_count == 36
    assert readiness.real_verified_recipes == 36
    assert readiness.test_fixture_recipes == 0
    assert readiness.breakfast_verified == 12
    assert readiness.lunch_verified == 12
    assert readiness.dinner_verified == 12
    assert readiness.unique_verified_recipes == 36
    assert readiness.can_form_21_meal_week is True
    assert readiness.production_canary_eligible is True

    # Author coverage checks
    escoffier_cov = readiness.author_coverages[AUTHOR_ESCOFFIER]
    assert escoffier_cov.verified_recipes == 36
    assert escoffier_cov.can_form_21_meal_week is True
    assert escoffier_cov.production_canary_eligible is True

    pokhlebkin_cov = readiness.author_coverages[AUTHOR_POKHLEBKIN]
    assert pokhlebkin_cov.verified_recipes == 0
    assert pokhlebkin_cov.production_canary_eligible is False

    jamie_cov = readiness.author_coverages[AUTHOR_JAMIE_OLIVER]
    assert jamie_cov.verified_recipes == 0
    assert jamie_cov.production_canary_eligible is False


# ==============================================================================
# 4. ADVERSARIAL CLASSIFICATION & MEAL GATING
# ==============================================================================

def test_meal_classification_integrity_and_adversarial_rejection(tmp_path: Path) -> None:
    """Each recipe has explicit classification source and reason matching its canonical chapter."""
    db_path = tmp_path / "classification.db"
    build_escoffier_real_catalog(db_path, build_id="class-test")

    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    recipes = store.get_all_recipes(verified_only=True)

    for r in recipes:
        assert r.classification_source == "CANONICAL_SOURCE_SERIES"
        assert r.classification_reason is not None

        if MealType.BREAKFAST in r.meal_types:
            assert "Chapitre V: Oeufs" in r.classification_reason
            assert r.source_locator and "p.3" in r.source_locator  # Pages 330-377
        elif MealType.LUNCH in r.meal_types:
            assert "Chapitre III: Potages" in r.classification_reason
            assert r.source_locator and ("p.1" in r.source_locator or "p.2" in r.source_locator)
        elif MealType.DINNER in r.meal_types:
            assert "Chapitres VI/VII/VIII" in r.classification_reason


# ==============================================================================
# 5. TAMPER FAIL-CLOSED VALIDATION
# ==============================================================================

def test_catalog_tamper_detection_fail_closed(tmp_path: Path) -> None:
    """Any SQLite byte or row modification triggers fail-closed CatalogIntegrityError."""
    db_path = tmp_path / "tamper_target.db"
    content_hash, _ = build_escoffier_real_catalog(db_path, build_id="tamper-test")

    # Read-only verification passes on clean DB
    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    assert store.get_content_hash() == content_hash

    # Tamper with recipe row
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE recipes SET title = 'Corrupted Soufflé' WHERE recipe_id = (SELECT recipe_id FROM recipes LIMIT 1)")
    conn.commit()
    conn.close()

    # Must raise CatalogIntegrityError on reopen
    with pytest.raises(CatalogIntegrityError, match="hash mismatch"):
        HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)


# ==============================================================================
# 6. PRODUCTION RETRIEVAL ACCEPTANCE
# ==============================================================================

def test_production_retriever_filter_behavior(tmp_path: Path) -> None:
    """RecipeRetriever retrieves production candidates only when production_rights_approved is True."""
    db_path = tmp_path / "retrieval_suite.db"
    build_escoffier_real_catalog(db_path, build_id="retriever-test")

    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    retriever = RecipeRetriever(store)

    # 1. Retrieve production-only breakfast
    b_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.BREAKFAST,
        authors=[AUTHOR_ESCOFFIER],
        production_only=True,
    )
    assert len(b_cands) == 12
    for c in b_cands:
        assert c.recipe.is_production_cleared is True
        assert c.recipe.source_id == "SRC_ESCOFFIER_1903_COMMONS"

    # 2. Retrieve production-only lunch
    l_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.LUNCH,
        authors=[AUTHOR_ESCOFFIER],
        production_only=True,
    )
    assert len(l_cands) == 12
    for c in l_cands:
        assert c.recipe.is_production_cleared is True

    # 3. Retrieve production-only dinner
    d_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.DINNER,
        authors=[AUTHOR_ESCOFFIER],
        production_only=True,
    )
    assert len(d_cands) == 12
    for c in d_cands:
        assert c.recipe.is_production_cleared is True

    # 4. Blocked authors return 0 candidates
    assert len(retriever.retrieve_candidates_for_slot(meal_slot=MealType.LUNCH, authors=[AUTHOR_POKHLEBKIN], production_only=True)) == 0
    assert len(retriever.retrieve_candidates_for_slot(meal_slot=MealType.DINNER, authors=[AUTHOR_JAMIE_OLIVER], production_only=True)) == 0


# ==============================================================================
# 7. RECIPE MIGRATION MAP INTEGRITY
# ==============================================================================

def test_recipe_id_migration_map_integrity() -> None:
    """Migration map accurately maps 36 legacy recipes with 0 conflicts and 0 unmapped."""
    map_path = REPO_ROOT / "recipe_corpus" / "reports" / "recipe_id_migration_map.json"
    assert map_path.exists(), "recipe_id_migration_map.json must exist"

    data = json.loads(map_path.read_text(encoding="utf-8"))
    assert data["total_mapped"] == 36
    assert data["unmapped_count"] == 0
    assert data["conflicts_count"] == 0
    assert len(data["mappings"]) == 36

    legacy_ids = {m["legacy_recipe_id"] for m in data["mappings"]}
    canonical_ids = {m["canonical_recipe_id"] for m in data["mappings"]}
    assert len(legacy_ids) == 36
    assert len(canonical_ids) == 36

    # Verify all meal types are represented (12 each)
    meal_counts: dict[str, int] = {}
    for m in data["mappings"]:
        meal = m["meal_type"]
        meal_counts[meal] = meal_counts.get(meal, 0) + 1

    assert meal_counts.get("breakfast") == 12
    assert meal_counts.get("lunch") == 12
    assert meal_counts.get("dinner") == 12


# ==============================================================================
# 8. WIKISOURCE CROSS-CHECK VERIFICATION
# ==============================================================================

def test_wikisource_crosscheck_report_validity() -> None:
    """Wikisource cross-check report must show sample size 6 and match rate 1.0."""
    ws_path = REPO_ROOT / "recipe_corpus" / "reports" / "wikisource_crosscheck_report.json"
    assert ws_path.exists(), "wikisource_crosscheck_report.json must exist"

    data = json.loads(ws_path.read_text(encoding="utf-8"))
    assert data["sample_size"] == 6
    assert data["overall_status"] == "MATCH_CONFIRMED"
    assert len(data["samples"]) == 6

    for sample in data["samples"]:
        assert sample["match_rate"] == 1.0
        assert "djvu_page" in sample
        assert "printed_page" in sample
        assert sample["djvu_page"] == sample["printed_page"] + 20


# ==============================================================================
# 9. RAW BOOK / TEXT SCAN NON-LEAKAGE IN GIT
# ==============================================================================

def test_raw_book_and_text_layer_not_tracked_in_git() -> None:
    """Assert neither DjVu binary nor raw text layer are tracked in Git."""
    res = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_files = res.stdout.splitlines()

    for path in tracked_files:
        p_lower = path.lower()
        assert not p_lower.endswith(".djvu"), f"Raw DjVu binary tracked in git: {path}"
        assert not ("b21525912" in p_lower and p_lower.endswith(".txt")), f"Raw text layer tracked in git: {path}"
        assert not ("pg71395" in p_lower and p_lower.endswith(".txt")), f"Raw text layer tracked in git: {path}"
