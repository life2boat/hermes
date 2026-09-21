from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from gateway.healbite_recipe_catalog_domain import (
    AUTHOR_ESCOFFIER,
    AUTHOR_JAMIE_OLIVER,
    AUTHOR_POKHLEBKIN,
    ContentScope,
    MealType,
    RecipeAuthor,
    RecipeSource,
    RightsEvidenceType,
    RightsStatus,
    SourceType,
    VerificationStatus,
)
from gateway.healbite_recipe_catalog_store import (
    CatalogIntegrityError,
    HealBiteRecipeCatalogStore,
)
from gateway.healbite_recipe_fixtures import (
    PRODUCTION_MANIFESTS,
    TEST_AUTHOR_A,
    TEST_AUTHOR_B,
    TEST_AUTHOR_C,
    build_pilot_recipe_catalog,
)
from gateway.healbite_recipe_ingestion import (
    IngestionValidationError,
    RecipeIngestionPipeline,
    RightsPolicyViolationError,
    calculate_author_coverage,
    calculate_catalog_readiness,
)
from gateway.healbite_recipe_retrieval import RecipeRetriever


# ==============================================================================
# RIGHTS ADVERSARIAL TESTS (10 REQUIRED SCENARIOS)
# ==============================================================================

def test_adversarial_1_unknown_source_rejects_recipe(tmp_path: Path) -> None:
    """1. UNKNOWN source + recipe content => REJECT"""
    db_path = tmp_path / "adv1.db"
    pipeline = RecipeIngestionPipeline(db_path)

    # Source with UNKNOWN rights is rejected upon source ingestion
    bad_source = RecipeSource(
        source_id="SRC_UNKNOWN_TEST",
        author_id=TEST_AUTHOR_A,
        title="Unknown Source",
        rights_status=RightsStatus.UNKNOWN,
    )
    with pytest.raises(RightsPolicyViolationError, match="UNKNOWN rights status"):
        pipeline.ingest_source(bad_source)

    # Ingest a valid author
    pipeline.ingest_author(
        RecipeAuthor(author_id=TEST_AUTHOR_A, display_name="Test Author A")
    )

    # Attempting to ingest a recipe with UNKNOWN rights status directly must also be rejected
    recipe_data = {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_NONEXISTENT",
        "title": "Dish with Unknown Rights",
        "meal_types": ["lunch"],
        "rights_status": RightsStatus.UNKNOWN.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Картофель", "quantity": 100, "unit": "g"}],
        "instructions": ["Сварить"],
    }
    with pytest.raises(RightsPolicyViolationError, match="rights status is UNKNOWN"):
        pipeline.ingest_recipe(recipe_data)

    pipeline.close()


def test_adversarial_2_link_only_source_rejects_recipe_content(tmp_path: Path) -> None:
    """2. LINK_ONLY source + full recipe content => REJECT"""
    db_path = tmp_path / "adv2.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(
        RecipeAuthor(author_id=AUTHOR_POKHLEBKIN, display_name="Вильям Похлёбкин")
    )
    link_only_source = RecipeSource(
        source_id="SRC_POKHLEBKIN_REF",
        author_id=AUTHOR_POKHLEBKIN,
        title="Энциклопедия (Ссылка)",
        source_type=SourceType.BOOK,
        source_locator="стр. 42",
        rights_status=RightsStatus.LINK_ONLY,
        rights_evidence_type=RightsEvidenceType.LINK_ONLY_CITATION,
        rights_evidence_locator="ISBN 978-5-9524",
        content_scope=ContentScope.METADATA_ONLY,
    )
    pipeline.ingest_source(link_only_source)

    # Attempting to ingest full recipe content under LINK_ONLY source must fail closed
    recipe_data = {
        "author_id": AUTHOR_POKHLEBKIN,
        "source_id": "SRC_POKHLEBKIN_REF",
        "title": "Похлёбкинский суп",
        "source_locator": "стр. 42",
        "meal_types": ["lunch"],
        "rights_status": RightsStatus.LINK_ONLY.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Картофель", "quantity": 100, "unit": "g"}],
        "instructions": ["Варить до готовности"],
    }
    with pytest.raises(RightsPolicyViolationError, match="LINK_ONLY / METADATA_ONLY"):
        pipeline.ingest_recipe(recipe_data)

    pipeline.close()


def test_adversarial_3_public_domain_without_evidence_rejects(tmp_path: Path) -> None:
    """3. PUBLIC_DOMAIN without evidence => REJECT / REVIEW_REQUIRED"""
    db_path = tmp_path / "adv3.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(
        RecipeAuthor(author_id=AUTHOR_ESCOFFIER, display_name="Огюст Эскофье")
    )

    # PUBLIC_DOMAIN source without evidence locator/type
    unsupported_pd = RecipeSource(
        source_id="SRC_ESCOFFIER_NO_EVIDENCE",
        author_id=AUTHOR_ESCOFFIER,
        title="Unverified PD Escoffier",
        source_type=SourceType.BOOK,
        rights_status=RightsStatus.PUBLIC_DOMAIN,
        rights_evidence_type=RightsEvidenceType.NONE,
        rights_evidence_locator=None,
    )
    with pytest.raises(RightsPolicyViolationError, match="requires recorded rights evidence"):
        pipeline.ingest_source(unsupported_pd)

    pipeline.close()


def test_adversarial_4_licensed_without_evidence_rejects(tmp_path: Path) -> None:
    """4. LICENSED without license evidence => REJECT / REVIEW_REQUIRED"""
    db_path = tmp_path / "adv4.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(
        RecipeAuthor(author_id="AUTHOR_COMMERCIAL", display_name="Коммерческий автор")
    )

    unverified_license = RecipeSource(
        source_id="SRC_COMMERCIAL_NO_LIC",
        author_id="AUTHOR_COMMERCIAL",
        title="Commercial Recipe Book",
        source_type=SourceType.BOOK,
        rights_status=RightsStatus.LICENSED,
        rights_evidence_type=RightsEvidenceType.NONE,
        rights_evidence_locator=None,
    )
    with pytest.raises(RightsPolicyViolationError, match="requires recorded license evidence"):
        pipeline.ingest_source(unverified_license)

    pipeline.close()


def test_adversarial_5_recipe_author_mismatch_rejects(tmp_path: Path) -> None:
    """5. recipe author != source author => REJECT"""
    db_path = tmp_path / "adv5.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(
        RecipeAuthor(author_id=TEST_AUTHOR_A, display_name="Test Author A")
    )
    pipeline.ingest_author(
        RecipeAuthor(author_id=TEST_AUTHOR_B, display_name="Test Author B")
    )
    pipeline.ingest_source(
        RecipeSource(
            source_id="SRC_TEST_A",
            author_id=TEST_AUTHOR_A,
            title="Book by Author A",
            source_type=SourceType.USER_DOCUMENT,
            source_locator="p. 1",
            rights_status=RightsStatus.USER_PROVIDED_AUTHORIZED,
            rights_evidence_type=RightsEvidenceType.OPERATOR_USER_GRANT,
            rights_evidence_locator="internal://tests",
            content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
        )
    )

    # Recipe claims author B, but source belongs to author A
    mismatched_recipe = {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_A",
        "title": "Mismatched Author Dish",
        "source_locator": "p. 1",
        "meal_types": ["lunch"],
        "rights_status": RightsStatus.USER_PROVIDED_AUTHORIZED.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Рис", "quantity": 100, "unit": "g"}],
        "instructions": ["Сварить рис"],
    }
    with pytest.raises(IngestionValidationError, match="does not match source author"):
        pipeline.ingest_recipe(mismatched_recipe)

    pipeline.close()


def test_adversarial_6_missing_source_locator_rejects(tmp_path: Path) -> None:
    """6. missing source locator where required => REJECT"""
    db_path = tmp_path / "adv6.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(
        RecipeAuthor(author_id=TEST_AUTHOR_A, display_name="Test Author A")
    )
    pipeline.ingest_source(
        RecipeSource(
            source_id="SRC_TEST_A_BOOK",
            author_id=TEST_AUTHOR_A,
            title="Book by Author A",
            source_type=SourceType.BOOK,
            rights_status=RightsStatus.USER_PROVIDED_AUTHORIZED,
            rights_evidence_type=RightsEvidenceType.OPERATOR_USER_GRANT,
            rights_evidence_locator="internal://tests",
            content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
        )
    )

    # Missing source_locator for a BOOK source
    recipe_without_locator = {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A_BOOK",
        "title": "Unlocated Book Recipe",
        "source_locator": "",  # Empty locator
        "meal_types": ["dinner"],
        "rights_status": RightsStatus.USER_PROVIDED_AUTHORIZED.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Мясо", "quantity": 200, "unit": "g"}],
        "instructions": ["Запечь"],
    }
    with pytest.raises(IngestionValidationError, match="requires explicit source_locator"):
        pipeline.ingest_recipe(recipe_without_locator)

    pipeline.close()


def test_adversarial_7_modified_source_hash_rejects(tmp_path: Path) -> None:
    """7. modified source after recorded source hash => REJECT / rebuild required"""
    db_path = tmp_path / "adv7.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(
        RecipeAuthor(author_id=TEST_AUTHOR_A, display_name="Test Author A")
    )
    pipeline.ingest_source(
        RecipeSource(
            source_id="SRC_TEST_A",
            author_id=TEST_AUTHOR_A,
            title="Collection A",
            source_type=SourceType.USER_DOCUMENT,
            source_locator="p. 5",
            rights_status=RightsStatus.USER_PROVIDED_AUTHORIZED,
            rights_evidence_type=RightsEvidenceType.OPERATOR_USER_GRANT,
            rights_evidence_locator="internal://tests",
            content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
            source_content_hash="canonical_recorded_hash_12345",
        )
    )

    # Recipe payload claims differing source content hash (source was modified/tampered)
    tampered_recipe = {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Tampered Source Recipe",
        "source_locator": "p. 5",
        "source_content_hash": "modified_unrecorded_hash_99999",
        "meal_types": ["dinner"],
        "rights_status": RightsStatus.USER_PROVIDED_AUTHORIZED.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Рыба", "quantity": 200, "unit": "g"}],
        "instructions": ["Пожарить"],
    }
    with pytest.raises(IngestionValidationError, match="Source content hash mismatch"):
        pipeline.ingest_recipe(tampered_recipe)

    pipeline.close()


def test_adversarial_8_synthetic_recipe_assigned_to_pokhlebkin_rejects(tmp_path: Path) -> None:
    """8. synthetic recipe assigned to POKHLEBKIN => REJECT"""
    db_path = tmp_path / "adv8.db"
    pipeline = RecipeIngestionPipeline(db_path)

    for manifest in PRODUCTION_MANIFESTS:
        pipeline.ingest_author(manifest["author"])
        for src in manifest["sources"]:
            pipeline.ingest_source(src)

    fake_pokhlebkin_recipe = {
        "author_id": AUTHOR_POKHLEBKIN,
        "source_id": "SRC_POKHLEBKIN_BEKA",
        "title": "Синтетический борщ Похлёбкина",
        "source_locator": "стр. 12",
        "meal_types": ["lunch"],
        "rights_status": RightsStatus.USER_PROVIDED_AUTHORIZED.value,
        "is_synthetic": True,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Свекла", "quantity": 100, "unit": "g"}],
        "instructions": ["Сварить"],
    }
    with pytest.raises(RightsPolicyViolationError, match="Synthetic recipes cannot be attributed"):
        pipeline.ingest_recipe(fake_pokhlebkin_recipe)

    pipeline.close()


def test_adversarial_9_synthetic_recipe_assigned_to_escoffier_rejects(tmp_path: Path) -> None:
    """9. synthetic recipe assigned to ESCOFFIER => REJECT"""
    db_path = tmp_path / "adv9.db"
    pipeline = RecipeIngestionPipeline(db_path)

    for manifest in PRODUCTION_MANIFESTS:
        pipeline.ingest_author(manifest["author"])
        for src in manifest["sources"]:
            pipeline.ingest_source(src)

    fake_escoffier_recipe = {
        "author_id": AUTHOR_ESCOFFIER,
        "source_id": "SRC_ESCOFFIER_LGC",
        "title": "Синтетический соус Эскофье",
        "source_locator": "p. 50",
        "meal_types": ["dinner"],
        "rights_status": RightsStatus.USER_PROVIDED_AUTHORIZED.value,
        "is_synthetic": True,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Масло", "quantity": 50, "unit": "g"}],
        "instructions": ["Эмульгировать"],
    }
    with pytest.raises(RightsPolicyViolationError, match="Synthetic recipes cannot be attributed"):
        pipeline.ingest_recipe(fake_escoffier_recipe)

    pipeline.close()


def test_adversarial_10_synthetic_recipe_assigned_to_jamie_oliver_rejects(tmp_path: Path) -> None:
    """10. synthetic recipe assigned to JAMIE_OLIVER => REJECT"""
    db_path = tmp_path / "adv10.db"
    pipeline = RecipeIngestionPipeline(db_path)

    for manifest in PRODUCTION_MANIFESTS:
        pipeline.ingest_author(manifest["author"])
        for src in manifest["sources"]:
            pipeline.ingest_source(src)

    fake_jamie_recipe = {
        "author_id": AUTHOR_JAMIE_OLIVER,
        "source_id": "SRC_JAMIE_OLIVER_15MIN",
        "title": "Синтетическая паста Джейми",
        "source_locator": "p. 15",
        "meal_types": ["dinner"],
        "rights_status": RightsStatus.USER_PROVIDED_AUTHORIZED.value,
        "is_synthetic": True,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Паста", "quantity": 100, "unit": "g"}],
        "instructions": ["Сварить за 15 минут"],
    }
    with pytest.raises(RightsPolicyViolationError, match="Synthetic recipes cannot be attributed"):
        pipeline.ingest_recipe(fake_jamie_recipe)

    pipeline.close()


# ==============================================================================
# DUPLICATE DETECTION & VERSION CONTRACT
# ==============================================================================

def test_duplicate_contract_exact_and_conflicting(tmp_path: Path) -> None:
    db_path = tmp_path / "dup_test.db"
    pipeline = RecipeIngestionPipeline(db_path)

    pipeline.ingest_author(
        RecipeAuthor(author_id=TEST_AUTHOR_A, display_name="Test Author A")
    )
    pipeline.ingest_source(
        RecipeSource(
            source_id="SRC_TEST_A",
            author_id=TEST_AUTHOR_A,
            title="Collection",
            source_type=SourceType.USER_DOCUMENT,
            source_locator="p. 1",
            rights_status=RightsStatus.USER_PROVIDED_AUTHORIZED,
            rights_evidence_type=RightsEvidenceType.OPERATOR_USER_GRANT,
            rights_evidence_locator="internal://tests",
            content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
        )
    )

    base_recipe = {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Гречневая каша с маслом",
        "source_locator": "p. 1",
        "meal_types": ["breakfast"],
        "rights_status": RightsStatus.USER_PROVIDED_AUTHORIZED.value,
        "verified": True,
        "verification_status": VerificationStatus.VERIFIED.value,
        "ingredients": [{"display_name": "Гречка", "quantity": 100, "unit": "g"}],
        "instructions": ["Сварить"],
    }

    # 1. Initial ingestion
    r_id_1 = pipeline.ingest_recipe(base_recipe)

    # 2. Exact duplicate re-ingestion: idempotent, records exact duplicate
    r_id_2 = pipeline.ingest_recipe(base_recipe)
    assert r_id_1 == r_id_2

    # 3. Normalized duplicate: differing whitespace & case in title
    norm_dup = dict(base_recipe)
    norm_dup["title"] = "  ГРЕЧНЕВАЯ   каша с маслом! "
    r_id_3 = pipeline.ingest_recipe(norm_dup)
    assert r_id_1 == r_id_3

    dup_rep = pipeline.get_duplicate_report()
    assert len(dup_rep.exact_duplicates) == 1
    assert len(dup_rep.normalized_duplicates) == 1
    assert len(dup_rep.conflicting_variants) == 0

    # 4. Conflicting variant with is_conflicting_variant=True must fail closed
    conflicting_recipe = dict(base_recipe)
    conflicting_recipe["ingredients"] = [{"display_name": "Совсем другие ингредиенты", "quantity": 500, "unit": "g"}]
    conflicting_recipe["is_conflicting_variant"] = True

    with pytest.raises(IngestionValidationError, match="Conflicting variant detected"):
        pipeline.ingest_recipe(conflicting_recipe)

    dup_rep_after = pipeline.get_duplicate_report()
    assert len(dup_rep_after.conflicting_variants) == 1

    pipeline.close()


# ==============================================================================
# AUTHOR COVERAGE & 21/21 READINESS
# ==============================================================================

def test_author_coverage_and_21_readiness(tmp_path: Path) -> None:
    db_path = tmp_path / "pilot_test.db"
    content_hash, readiness = build_pilot_recipe_catalog(db_path, build_id="pilot-test-1")

    # Author coverage checks
    cov_pokhlebkin = readiness.author_coverages[AUTHOR_POKHLEBKIN]
    assert cov_pokhlebkin.metadata_sources == 1
    assert cov_pokhlebkin.structured_authorized_sources == 0
    assert cov_pokhlebkin.verified_recipes == 0
    assert cov_pokhlebkin.blocked_sources == 1
    assert cov_pokhlebkin.can_form_21_meal_week is False

    cov_escoffier = readiness.author_coverages[AUTHOR_ESCOFFIER]
    assert cov_escoffier.metadata_sources == 1
    assert cov_escoffier.structured_authorized_sources == 0
    assert cov_escoffier.verified_recipes == 0
    assert cov_escoffier.blocked_sources == 1
    assert cov_escoffier.can_form_21_meal_week is False

    cov_jamie = readiness.author_coverages[AUTHOR_JAMIE_OLIVER]
    assert cov_jamie.metadata_sources == 1
    assert cov_jamie.structured_authorized_sources == 0
    assert cov_jamie.verified_recipes == 0
    assert cov_jamie.blocked_sources == 1
    assert cov_jamie.can_form_21_meal_week is False

    # Combined pilot catalog readiness
    assert readiness.real_verified_recipes == 0
    assert readiness.test_fixture_recipes == 27
    assert readiness.breakfast_verified == 9
    assert readiness.lunch_verified == 9
    assert readiness.dinner_verified == 9
    assert readiness.unique_verified_recipes == 27
    assert readiness.can_form_21_meal_week is True


# ==============================================================================
# SECURITY & ISOLATION TESTS
# ==============================================================================

def test_security_and_isolation_no_user_or_household_data(tmp_path: Path) -> None:
    """Assert catalog artifact contains no user, household, telegram, or shopping data."""
    db_path = tmp_path / "isolated.db"
    build_pilot_recipe_catalog(db_path, build_id="isolation-test")

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # List all tables
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    table_names = [row[0] for row in cur.fetchall()]

    # Assert forbidden prefixes / names do not exist
    forbidden_table_prefixes = ("household", "user", "telegram", "inventory", "shopping", "weekly_menu")
    for tbl in table_names:
        for prefix in forbidden_table_prefixes:
            assert not tbl.lower().startswith(prefix), f"Forbidden table '{tbl}' found in recipe_catalog.db"

    # Assert forbidden columns do not exist across all tables
    forbidden_columns = {"user_id", "household_id", "telegram_id", "chat_id", "telegram_user_id"}
    for tbl in table_names:
        if tbl.startswith("sqlite_") or "fts" in tbl:
            continue
        cur.execute(f"PRAGMA table_info({tbl})")
        cols = {row[1].lower() for row in cur.fetchall()}
        intersection = cols.intersection(forbidden_columns)
        assert not intersection, f"Forbidden columns {intersection} found in table '{tbl}'"

    # Assert SQLite FTS5 table is present
    assert "recipes_fts" in table_names

    conn.close()


# ==============================================================================
# OFFLINE RETRIEVAL ACCEPTANCE
# ==============================================================================

def test_offline_retrieval_acceptance(tmp_path: Path) -> None:
    db_path = tmp_path / "retrieval_pilot.db"
    build_pilot_recipe_catalog(db_path, build_id="retrieval-pilot-1")

    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    retriever = RecipeRetriever(store)

    # 1. Only VERIFIED planning-eligible recipes returned
    breakfast_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.BREAKFAST,
        authors=[TEST_AUTHOR_A, TEST_AUTHOR_B],
        limit=10,
    )
    assert len(breakfast_cands) > 0
    for cand in breakfast_cands:
        assert cand.recipe.is_verified_for_planning
        assert cand.recipe.rights_status not in (RightsStatus.UNKNOWN, RightsStatus.LINK_ONLY)
        assert cand.recipe.author_id in (TEST_AUTHOR_A, TEST_AUTHOR_B)
        assert MealType.BREAKFAST in cand.recipe.meal_types

    # 2. Production metadata-only authors return ZERO candidates
    pokhlebkin_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.LUNCH,
        authors=[AUTHOR_POKHLEBKIN],
    )
    assert len(pokhlebkin_cands) == 0

    escoffier_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.DINNER,
        authors=[AUTHOR_ESCOFFIER],
    )
    assert len(escoffier_cands) == 0

    jamie_cands = retriever.retrieve_candidates_for_slot(
        meal_slot=MealType.DINNER,
        authors=[AUTHOR_JAMIE_OLIVER],
    )
    assert len(jamie_cands) == 0

    # 3. Candidate IDs resolve back to exact catalog rows
    first_cand = breakfast_cands[0]
    fetched = store.get_recipe(first_cand.recipe_id)
    assert fetched is not None
    assert fetched.recipe_id == first_cand.recipe_id
    assert fetched.title == first_cand.title

    # 4. Unknown recipe_id returns None
    assert store.get_recipe("00000000-0000-0000-0000-000000000000") is None
