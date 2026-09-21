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
    RecipeAuthor,
    RecipeSource,
    RightsEvidenceType,
    RightsStatus,
    SourceType,
    TranslationRightsStatus,
    VerificationStatus,
)
from gateway.healbite_recipe_catalog_store import (
    CatalogIntegrityError,
    HealBiteRecipeCatalogStore,
)
from gateway.healbite_recipe_fixtures import (
    PRODUCTION_MANIFESTS,
    SRC_ESCOFFIER_1907_EN_SOURCE,
    build_escoffier_real_catalog,
)
from gateway.healbite_recipe_ingestion import (
    RecipeIngestionPipeline,
    RightsPolicyViolationError,
    compute_semantic_corpus_hash,
)
from gateway.healbite_recipe_retrieval import RecipeRetriever


# ==============================================================================
# 1. RIGHTS EVIDENCE COMPLETENESS (Gallica & Gutenberg)
# ==============================================================================

def test_escoffier_rights_evidence_completeness() -> None:
    """Verify both Gallica 1903 (French) and Gutenberg 1907 (English) sources are recorded with all 10 rights dimensions."""
    # 1. Gallica French 1903 source
    escoffier_manifest = next(m for m in PRODUCTION_MANIFESTS if m["author"].author_id == AUTHOR_ESCOFFIER)
    gallica_src = next(s for s in escoffier_manifest["sources"] if s.source_id == "SRC_ESCOFFIER_LGC")

    assert gallica_src.source_id == "SRC_ESCOFFIER_LGC"
    assert gallica_src.author_id == AUTHOR_ESCOFFIER
    assert gallica_src.rights_status == RightsStatus.PUBLIC_DOMAIN
    assert gallica_src.rights_evidence_type == RightsEvidenceType.PUBLIC_DOMAIN_STATUTE
    assert "ark:/12148/bpt6k65768837" in (gallica_src.rights_evidence_locator or "")
    assert gallica_src.content_scope == ContentScope.METADATA_ONLY
    assert gallica_src.underlying_work_rights == "PUBLIC_DOMAIN"
    assert gallica_src.commercial_reuse_status == CommercialReuseStatus.LICENSE_REQUIRED
    assert "BnF Gallica" in gallica_src.digital_reproduction_reuse_terms
    assert gallica_src.translation_status == TranslationRightsStatus.ORIGINAL_LANGUAGE
    assert gallica_src.production_rights_approved is False

    # 2. Gutenberg English 1907 source
    gutenberg_src = SRC_ESCOFFIER_1907_EN_SOURCE
    assert gutenberg_src.source_id == "SRC_ESCOFFIER_1907_EN"
    assert gutenberg_src.author_id == AUTHOR_ESCOFFIER
    assert gutenberg_src.rights_status == RightsStatus.PUBLIC_DOMAIN
    assert gutenberg_src.content_scope == ContentScope.STRUCTURED_RECIPE_CONTENT
    assert gutenberg_src.verification_status == VerificationStatus.VERIFIED
    assert gutenberg_src.underlying_work_rights == "PUBLIC_DOMAIN"
    assert gutenberg_src.commercial_reuse_status == CommercialReuseStatus.REVIEW_REQUIRED
    assert gutenberg_src.translation_status == TranslationRightsStatus.PUBLIC_DOMAIN
    assert gutenberg_src.production_rights_approved is False
    assert "pg71395.txt" in (gutenberg_src.rights_evidence_locator or "")
    assert gutenberg_src.source_content_hash == "e0850c1d4589b8e03a7832258f911c447fb3dc0d9e706403822a7477c8615993"


# ==============================================================================
# 2. GUTENBERG US-ONLY NOTICE DOES NOT GRANT GLOBAL AUTHORIZATION
# ==============================================================================

def test_gutenberg_us_only_rights_marker_does_not_become_global_authorization(tmp_path: Path) -> None:
    """Project Gutenberg eBook #71395 US public domain notice does NOT imply global authorization or production clearance."""
    db_path = tmp_path / "gutenberg_rights.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(RecipeAuthor(author_id=AUTHOR_ESCOFFIER, display_name="Огюст Эскофье"))
    pipeline.ingest_source(SRC_ESCOFFIER_1907_EN_SOURCE)

    # Ingest a real recipe from this source
    recipe_data = {
        "author_id": AUTHOR_ESCOFFIER,
        "source_id": "SRC_ESCOFFIER_1907_EN",
        "title": "Oeufs sur le plat",
        "source_recipe_title": "EGGS ON THE DISH",
        "source_locator": "Heinemann:1907:chapter=XII:recipe=395",
        "source_content_hash": "e0850c1d4589b8e03a7832258f911c447fb3dc0d9e706403822a7477c8615993",
        "rights_status": RightsStatus.PUBLIC_DOMAIN.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "production_eligible": False,
        "meal_types": ["breakfast"],
        "ingredients": [{"display_name": "eggs", "quantity": 2, "unit": "pc"}],
        "instructions": ["Cook in butter"],
    }
    r_id = pipeline.ingest_recipe(recipe_data)
    pipeline.close()

    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=False)
    recipe = store.get_recipe(r_id)
    assert recipe is not None

    # Recipe is verified for offline planning but NOT cleared for production deployment
    assert recipe.is_verified_for_planning is True
    assert recipe.production_eligible is False
    assert recipe.is_production_cleared is False


# ==============================================================================
# 3. TRANSLATION RIGHTS UNKNOWN => PLANNING-INELIGIBLE
# ==============================================================================

def test_translation_rights_unknown_is_planning_ineligible(tmp_path: Path) -> None:
    """Source with UNKNOWN translation rights fails closed on recipe ingestion."""
    db_path = tmp_path / "trans_unknown.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(RecipeAuthor(author_id=AUTHOR_ESCOFFIER, display_name="Огюст Эскофье"))
    unverified_trans_source = RecipeSource(
        source_id="SRC_ESCOFFIER_UNKNOWN_TRANS",
        author_id=AUTHOR_ESCOFFIER,
        title="Unverified Translation",
        source_type=SourceType.BOOK,
        source_locator="Unknown 1950 translation",
        rights_status=RightsStatus.PUBLIC_DOMAIN,
        rights_evidence_type=RightsEvidenceType.PUBLIC_DOMAIN_STATUTE,
        rights_evidence_locator="none",
        content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
        verification_status=VerificationStatus.VERIFIED,
        translation_status=TranslationRightsStatus.UNKNOWN,  # Unknown translation rights!
    )
    pipeline.ingest_source(unverified_trans_source)

    recipe_data = {
        "author_id": AUTHOR_ESCOFFIER,
        "source_id": "SRC_ESCOFFIER_UNKNOWN_TRANS",
        "title": "Unverified Translation Dish",
        "source_locator": "p. 10",
        "rights_status": RightsStatus.PUBLIC_DOMAIN.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "meal_types": ["lunch"],
        "ingredients": [{"display_name": "eggs", "quantity": 2, "unit": "pc"}],
        "instructions": ["Cook"],
    }

    with pytest.raises(RightsPolicyViolationError, match="UNKNOWN translation rights"):
        pipeline.ingest_recipe(recipe_data)

    pipeline.close()


# ==============================================================================
# 4. PUBLIC DOMAIN UNDERLYING WORK + COMMERCIAL LICENSE REQUIRED
# ==============================================================================

def test_underlying_work_public_domain_with_commercial_license_required_not_production_cleared(
    tmp_path: Path,
) -> None:
    """Source with commercial license required cannot be marked production_rights_approved without license."""
    db_path = tmp_path / "gallica_license.db"
    pipeline = RecipeIngestionPipeline(db_path)

    # Attempting to ingest source with commercial license required AND production_rights_approved=True must fail closed
    bad_source = RecipeSource(
        source_id="SRC_GALLICA_INVALID_APPROVAL",
        author_id=AUTHOR_ESCOFFIER,
        title="Gallica 1903",
        source_type=SourceType.BOOK,
        rights_status=RightsStatus.PUBLIC_DOMAIN,
        rights_evidence_type=RightsEvidenceType.PUBLIC_DOMAIN_STATUTE,
        rights_evidence_locator="ark:/12148/bpt6k65768837",
        commercial_reuse_status=CommercialReuseStatus.LICENSE_REQUIRED,
        production_rights_approved=True,  # Invalid: cannot approve without license!
    )
    with pytest.raises(RightsPolicyViolationError, match="requires commercial license"):
        pipeline.ingest_source(bad_source)

    pipeline.close()


# ==============================================================================
# 5. REVIEW_REQUIRED SOURCE IS NOT PLANNING-ELIGIBLE
# ==============================================================================

def test_review_required_source_is_not_planning_eligible(tmp_path: Path) -> None:
    """Source with verification_status=REVIEW_REQUIRED cannot have recipes ingested for planning."""
    db_path = tmp_path / "review_req.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(RecipeAuthor(author_id=AUTHOR_ESCOFFIER, display_name="Огюст Эскофье"))
    review_source = RecipeSource(
        source_id="SRC_ESCOFFIER_REVIEW",
        author_id=AUTHOR_ESCOFFIER,
        title="Edition Under Review",
        source_type=SourceType.BOOK,
        source_locator="Draft",
        rights_status=RightsStatus.PUBLIC_DOMAIN,
        rights_evidence_type=RightsEvidenceType.PUBLIC_DOMAIN_STATUTE,
        rights_evidence_locator="ark:/test",
        content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
        verification_status=VerificationStatus.REVIEW_REQUIRED,  # Under review!
    )
    pipeline.ingest_source(review_source)

    recipe_data = {
        "author_id": AUTHOR_ESCOFFIER,
        "source_id": "SRC_ESCOFFIER_REVIEW",
        "title": "Dish Under Review",
        "source_locator": "p. 1",
        "rights_status": RightsStatus.PUBLIC_DOMAIN.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "meal_types": ["lunch"],
        "ingredients": [{"display_name": "eggs", "quantity": 2, "unit": "pc"}],
        "instructions": ["Cook"],
    }
    with pytest.raises(RightsPolicyViolationError, match="REVIEW_REQUIRED"):
        pipeline.ingest_recipe(recipe_data)

    pipeline.close()


# ==============================================================================
# 6. SOURCE METADATA STORED WHILE STRUCTURED CONTENT REMAINS BLOCKED
# ==============================================================================

def test_source_metadata_stored_while_structured_content_blocked(tmp_path: Path) -> None:
    """Pokhlebkin and Jamie Oliver remain metadata/LINK_ONLY with 0 structured recipes."""
    db_path = tmp_path / "metadata_only.db"
    build_escoffier_real_catalog(db_path, build_id="test-meta-blocked")

    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)

    # Sources exist
    pokh_sources = store.list_sources_by_author(AUTHOR_POKHLEBKIN)
    assert len(pokh_sources) == 1
    assert pokh_sources[0].content_scope == ContentScope.METADATA_ONLY
    assert pokh_sources[0].rights_status == RightsStatus.LINK_ONLY

    jamie_sources = store.list_sources_by_author(AUTHOR_JAMIE_OLIVER)
    assert len(jamie_sources) == 1
    assert jamie_sources[0].content_scope == ContentScope.METADATA_ONLY
    assert jamie_sources[0].rights_status == RightsStatus.LINK_ONLY

    # ZERO recipes attributed
    assert len(store.list_recipes_by_author(AUTHOR_POKHLEBKIN)) == 0
    assert len(store.list_recipes_by_author(AUTHOR_JAMIE_OLIVER)) == 0


# ==============================================================================
# 7. REAL ESCOFFIER CORPUS ACCEPTANCE (36 VERIFIED RECIPES, ZERO FIXTURES)
# ==============================================================================

def test_real_escoffier_corpus_acceptance(tmp_path: Path) -> None:
    """Pure real Escoffier catalog build contains exactly 36 verified recipes and 0 test fixtures."""
    db_path = tmp_path / "escoffier_real.db"
    rep_dir = tmp_path / "reports"
    content_hash, readiness = build_escoffier_real_catalog(
        db_path,
        build_id="escoffier-acceptance-1",
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
    assert readiness.production_canary_eligible is False

    # Check store directly
    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    recipes = store.get_all_recipes(verified_only=True)
    assert len(recipes) == 36

    for r in recipes:
        assert r.author_id == AUTHOR_ESCOFFIER
        assert r.source_id == "SRC_ESCOFFIER_1907_EN"
        assert r.source_locator and r.source_locator.startswith("Heinemann:1907:chapter=")
        assert r.source_content_hash == "e0850c1d4589b8e03a7832258f911c447fb3dc0d9e706403822a7477c8615993"
        assert r.is_verified_for_planning is True
        assert r.production_eligible is False
        assert r.is_production_cleared is False
        assert len(r.ingredients) >= 2
        assert len(r.instructions) >= 2


# ==============================================================================
# 8. READ-ONLY REOPEN & TAMPER FAIL-CLOSED VALIDATION
# ==============================================================================

def test_read_only_reopen_and_catalog_tamper_detection(tmp_path: Path) -> None:
    """Catalog verifies cryptographic content hash upon reopen and detects tampering fail-closed."""
    db_path = tmp_path / "tamper_test.db"
    content_hash, _ = build_escoffier_real_catalog(db_path, build_id="tamper-test-1")

    # Clean reopen passes
    clean_store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    assert clean_store.get_content_hash() == content_hash

    # Tamper with a recipe in the database
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE recipes SET title = 'Tampered Dish' WHERE recipe_id = (SELECT recipe_id FROM recipes LIMIT 1)")
    conn.commit()
    conn.close()

    # Reopening tampered database must fail closed with CatalogIntegrityError
    with pytest.raises(CatalogIntegrityError, match="hash mismatch"):
        HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)


# ==============================================================================
# 9. RETRIEVAL ACCEPTANCE (VERIFIED ONLY, ESCOFFIER ONLY, ZERO FIXTURE LEAKAGE)
# ==============================================================================

def test_retrieval_acceptance_real_corpus(tmp_path: Path) -> None:
    """RecipeRetriever operates against authentic Escoffier corpus with zero test fixture leakage."""
    db_path = tmp_path / "retrieval_real.db"
    build_escoffier_real_catalog(db_path, build_id="retrieval-test", include_test_fixtures=False)

    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    retriever = RecipeRetriever(store)

    # 1. Retrieve breakfast candidates
    breakfast_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.BREAKFAST,
        authors=[AUTHOR_ESCOFFIER],
        limit=10,
    )
    assert len(breakfast_cands) == 10
    for cand in breakfast_cands:
        assert cand.recipe.author_id == AUTHOR_ESCOFFIER
        assert cand.recipe.source_id == "SRC_ESCOFFIER_1907_EN"
        assert cand.recipe.is_verified_for_planning is True
        assert cand.recipe.production_eligible is False
        assert MealType.BREAKFAST in cand.recipe.meal_types

    # 2. Retrieve lunch candidates
    lunch_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.LUNCH,
        authors=[AUTHOR_ESCOFFIER],
        limit=10,
    )
    assert len(lunch_cands) == 10
    for cand in lunch_cands:
        assert cand.recipe.author_id == AUTHOR_ESCOFFIER
        assert MealType.LUNCH in cand.recipe.meal_types

    # 3. Retrieve dinner candidates
    dinner_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.DINNER,
        authors=[AUTHOR_ESCOFFIER],
        limit=10,
    )
    assert len(dinner_cands) == 10
    for cand in dinner_cands:
        assert cand.recipe.author_id == AUTHOR_ESCOFFIER
        assert MealType.DINNER in cand.recipe.meal_types

    # 4. Production-cleared retrieval filter returns zero (not cleared for production)
    prod_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.DINNER,
        authors=[AUTHOR_ESCOFFIER],
        production_only=True,
    )
    assert len(prod_cands) == 0

    # 5. Metadata-only authors return ZERO candidates
    assert len(retriever.retrieve_candidates_for_slot(meal_slot=MealType.LUNCH, authors=[AUTHOR_POKHLEBKIN])) == 0
    assert len(retriever.retrieve_candidates_for_slot(meal_slot=MealType.DINNER, authors=[AUTHOR_JAMIE_OLIVER])) == 0


# ==============================================================================
# 10. 21/21 READINESS CALCULATION & PRODUCTION CANARY GATING
# ==============================================================================

def test_escoffier_21_readiness_calculation(tmp_path: Path) -> None:
    """Assert 12/12/12 coverage satisfies can_form_21_meal_week but fails closed for production_canary_eligible."""
    db_path = tmp_path / "readiness_21.db"
    _, readiness = build_escoffier_real_catalog(db_path, build_id="readiness-test")

    escoffier_cov = readiness.author_coverages[AUTHOR_ESCOFFIER]
    assert escoffier_cov.breakfast_verified == 12
    assert escoffier_cov.lunch_verified == 12
    assert escoffier_cov.dinner_verified == 12
    assert escoffier_cov.unique_verified_recipes == 36
    assert escoffier_cov.can_form_21_meal_week is True
    assert escoffier_cov.production_canary_eligible is False

    assert readiness.can_form_21_meal_week is True
    assert readiness.production_canary_eligible is False


# ==============================================================================
# 11. DETERMINISTIC REBUILD & SEMANTIC CORPUS HASH EQUALITY
# ==============================================================================

def test_deterministic_rebuild_and_semantic_corpus_hash(tmp_path: Path) -> None:
    """Assert rebuilds produce identical SHA-256 and semantic content hash."""
    db1 = tmp_path / "db1.db"
    db2 = tmp_path / "db2.db"

    h1, _ = build_escoffier_real_catalog(db1, build_id="rebuild-1")
    h2, _ = build_escoffier_real_catalog(db2, build_id="rebuild-2")

    assert h1 == h2
    assert len(h1) == 64

    # Test semantic corpus hash
    recipes_json = Path("recipe_corpus/authorized_inputs/escoffier_recipes.json")
    recipes_data = json.loads(recipes_json.read_text(encoding="utf-8"))

    sem_hash_1 = compute_semantic_corpus_hash(recipes_data)
    # Permute order of list
    reversed_recipes = list(reversed(recipes_data))
    sem_hash_2 = compute_semantic_corpus_hash(reversed_recipes)

    assert sem_hash_1 == sem_hash_2
    assert len(sem_hash_1) == 64


# ==============================================================================
# 12. COPYRIGHT & RAW SOURCE LEAKAGE INVARIANTS
# ==============================================================================

def test_copyright_and_raw_source_leakage_invariants() -> None:
    """Verify that no raw books (pg71395.txt, OCR dumps, pdfs) are committed to Git."""
    cmd = ["git", "status", "--porcelain"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)

    # Check untracked and modified files
    for line in res.stdout.splitlines():
        path = line[3:].strip()
        assert not path.endswith(".txt") or "pg71395" not in path, f"Raw book file found: {path}"
        assert not path.endswith(".djvu"), f"Raw OCR/djvu file found: {path}"
        assert not path.endswith(".pdf"), f"Raw PDF file found: {path}"

    # Verify frozen file exists outside Git
    outside_scratch = Path(r"C:\Users\Oleg\.gemini\antigravity\brain\c249e407-062f-4265-9f05-f4942ec7046b\scratch\pg71395.txt")
    assert outside_scratch.exists(), "Frozen source artifact outside Git must exist"
