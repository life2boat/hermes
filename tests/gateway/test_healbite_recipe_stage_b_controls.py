from __future__ import annotations

import concurrent.futures
import json
import logging
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import MagicMock

import pytest

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_household_schema import HouseholdRole
from gateway.healbite_households import HealBiteHouseholdService, HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryItemInput,
    InventoryOwnerScope,
    InventorySourceType,
)
from gateway.healbite_recipe_catalog_domain import MealType, TargetRightsScope
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore
from gateway.healbite_recipe_fixtures import TEST_AUTHORS, build_test_recipe_catalog
from gateway.healbite_recipe_grounded_planner import (
    MAX_PLANNER_REPAIR_ATTEMPTS,
    PlannerGroundingError,
    RecipeGroundedPlan,
    RecipeGroundedWeeklyPlanner,
)
from gateway.healbite_recipe_grounded_service import (
    HealBiteRecipeGroundedService,
    RecipeGroundedStatus,
)
from gateway.healbite_recipe_stage_b_policy import (
    DEFAULT_MAX_GENERATIONS_PER_WINDOW,
    GuardAcquireResult,
    GuardAcquireState,
    MAX_PROVIDER_CALLS_PER_GENERATION,
    RecipeGenerationGuard,
    STAGE_B_MAX_ALLOWLIST_USERS,
    STAGE_B_REQUIRED_RIGHTS_SCOPE,
    evaluate_stage_b_cohort_policy,
    safe_pseudonym,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuRevisionStatus,
)


def _protein_of(cand):
    tags = cand.get("tags") or []
    for t in tags:
        t_low = t.lower()
        if "говяд" in t_low or "беф" in t_low:
            return "говядина"
        if "кури" in t_low:
            return "курица"
        if "рыб" in t_low or "лосос" in t_low or "треск" in t_low:
            return "рыба"
        if "свин" in t_low:
            return "свинина"
        if "индей" in t_low:
            return "индейка"

    title = cand.get("title", "").lower()
    if "говяд" in title or "беф" in title:
        return "говядина"
    if "кури" in title:
        return "курица"
    if "рыб" in title or "треск" in title or "лосос" in title:
        return "рыба"
    if "свин" in title:
        return "свинина"
    if "индей" in title:
        return "индейка"
    return None


def _deterministic_planner_caller(messages, **kwargs):
    content = messages[1]["content"]
    if "\n\nВНИМАНИЕ:" in content:
        content = content.split("\n\nВНИМАНИЕ:")[0]
    user_content = json.loads(content)
    week_start = user_content["week_start"]
    dates = user_content["dates"]
    cands = user_content["allowed_candidates"]
    breakfast_cands = [c for c in cands if c["slot"] == "breakfast"]
    lunch_cands = [c for c in cands if c["slot"] == "lunch"]
    dinner_cands = [c for c in cands if c["slot"] == "dinner"]

    used_ids: set[str] = set()
    meals = []
    last_protein = None
    for dt in dates:
        b = next(c for c in breakfast_cands if c["recipe_id"] not in used_ids)
        used_ids.add(b["recipe_id"])
        meals.append({"date": dt, "slot": "breakfast", "recipe_id": b["recipe_id"], "recipe_version": b["recipe_version"], "servings": 2})

        l = next(c for c in lunch_cands if c["recipe_id"] not in used_ids)
        used_ids.add(l["recipe_id"])
        meals.append({"date": dt, "slot": "lunch", "recipe_id": l["recipe_id"], "recipe_version": l["recipe_version"], "servings": 2})

        eligible_dinners = [c for c in dinner_cands if c["recipe_id"] not in used_ids]
        d = next(
            (c for c in eligible_dinners if _protein_of(c) is None or _protein_of(c) != last_protein),
            eligible_dinners[0],
        )
        used_ids.add(d["recipe_id"])
        last_protein = _protein_of(d)
        meals.append({"date": dt, "slot": "dinner", "recipe_id": d["recipe_id"], "recipe_version": d["recipe_version"], "servings": 2})

    return json.dumps({"week_start": week_start, "meals": meals})


def _setup_stage_b_env(tmp_path: Path, *, actor_ids: Sequence[int] = (101,)):
    db_path = tmp_path / "stage_b_test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for aid in actor_ids:
        conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (aid, f"user_{aid}"))
    conn.commit()
    conn.close()

    household_store = HealBiteHouseholdStore(db_path=db_path)
    for aid in actor_ids:
        household_store.get_or_create_personal_household(aid)
    household_service = HealBiteHouseholdService(household_store)

    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()

    inventory_store = HealBiteInventoryStore(db_path=db_path)
    inventory_store.initialize_schema()

    catalog_path = tmp_path / "catalog.db"
    build_test_recipe_catalog(catalog_path)

    config = FeatureGateConfig(
        enabled=True,
        allowlist=frozenset(actor_ids),
        public_access=False,
    )

    guard = RecipeGenerationGuard()

    return db_path, catalog_path, household_service, weekly_store, inventory_store, config, guard


# ==============================================================================
# 1. Cohort Allowlist Size Tests
# ==============================================================================

def test_stage_b_cohort_policy_allowlist_size() -> None:
    # 0 users -> fails closed
    d0 = evaluate_stage_b_cohort_policy([], public_access=False, target_rights_scope="FR")
    assert not d0.valid
    assert d0.status == "allowlist_empty"

    # 1 user -> valid
    d1 = evaluate_stage_b_cohort_policy([101], public_access=False, target_rights_scope="FR")
    assert d1.valid
    assert d1.status == "ready"
    assert d1.allowlist_count == 1

    # 5 users (max) -> valid
    d5 = evaluate_stage_b_cohort_policy([101, 102, 103, 104, 105], public_access=False, target_rights_scope="FR")
    assert d5.valid
    assert d5.status == "ready"
    assert d5.allowlist_count == 5

    # 6 users (max + 1) -> fails closed
    d6 = evaluate_stage_b_cohort_policy([101, 102, 103, 104, 105, 106], public_access=False, target_rights_scope="FR")
    assert not d6.valid
    assert d6.status == "allowlist_exceeds_max"
    assert d6.allowlist_count == 6


# ==============================================================================
# 2. PUBLIC=true Rejection Tests
# ==============================================================================

def test_stage_b_cohort_policy_public_access_rejection() -> None:
    d = evaluate_stage_b_cohort_policy([101], public_access=True, target_rights_scope="FR")
    assert not d.valid
    assert d.status == "public_not_allowed"


# ==============================================================================
# 3. Rights Scope Tests
# ==============================================================================

def test_stage_b_cohort_policy_rights_scope() -> None:
    # Missing scope -> fails closed
    dm = evaluate_stage_b_cohort_policy([101], public_access=False, target_rights_scope=None)
    assert not dm.valid
    assert dm.status == "missing_rights_scope"

    # OTHER -> fails closed
    do = evaluate_stage_b_cohort_policy([101], public_access=False, target_rights_scope="OTHER")
    assert not do.valid
    assert do.status == "invalid_rights_scope"

    # FR,OTHER -> fails closed
    dfo = evaluate_stage_b_cohort_policy([101], public_access=False, target_rights_scope="FR,OTHER")
    assert not dfo.valid
    assert dfo.status == "invalid_rights_scope"

    # FR -> valid
    dfr = evaluate_stage_b_cohort_policy([101], public_access=False, target_rights_scope="FR")
    assert dfr.valid
    assert dfr.status == "ready"
    assert dfr.rights_scope == "FR"


# ==============================================================================
# 4. Service Enforcement of Stage B Policy
# ==============================================================================

def test_service_stage_b_enforcement(tmp_path: Path) -> None:
    db_path, catalog_path, hh_svc, wk_store, inv_store, _, guard = _setup_stage_b_env(tmp_path, actor_ids=(101,))

    # Config with 6 allowlisted users (exceeds max 5)
    bad_config = FeatureGateConfig(
        enabled=True,
        allowlist=frozenset([101, 102, 103, 104, 105, 106]),
        public_access=False,
    )
    service_bad = HealBiteRecipeGroundedService(
        catalog_path=str(catalog_path),
        weekly_menu_store=wk_store,
        household_service=hh_svc,
        inventory_store=inv_store,
        planner=RecipeGroundedWeeklyPlanner(llm_caller=_deterministic_planner_caller),
        config=bad_config,
        target_rights_scope="FR",
        guard=guard,
        stage_b_policy_enforced=True,
    )

    res_bad = service_bad.generate_menu(
        actor_user_id=101,
        week_start="2026-09-21",
        authors=list(TEST_AUTHORS.keys()),
    )
    assert res_bad.status == RecipeGroundedStatus.MISCONFIGURED
    assert "exceeds maximum allowed cohort limit" in str(res_bad.error_message)

    # Valid config with 1 user and FR rights scope
    good_config = FeatureGateConfig(
        enabled=True,
        allowlist=frozenset([101]),
        public_access=False,
    )
    service_good = HealBiteRecipeGroundedService(
        catalog_path=str(catalog_path),
        weekly_menu_store=wk_store,
        household_service=hh_svc,
        inventory_store=inv_store,
        planner=RecipeGroundedWeeklyPlanner(llm_caller=_deterministic_planner_caller),
        config=good_config,
        target_rights_scope="FR",
        guard=guard,
        stage_b_policy_enforced=True,
    )

    res_good = service_good.generate_menu(
        actor_user_id=101,
        week_start="2026-09-21",
        authors=list(TEST_AUTHORS.keys()),
    )
    assert res_good.status == RecipeGroundedStatus.SUCCESS
    assert res_good.revision_view is not None


# ==============================================================================
# 5. Duplicate Callback & Idempotency Replay (0 Provider Calls on Replay)
# ==============================================================================

def test_duplicate_callback_and_idempotency_replay(tmp_path: Path) -> None:
    db_path, catalog_path, hh_svc, wk_store, inv_store, config, guard = _setup_stage_b_env(tmp_path, actor_ids=(101,))

    call_count = [0]

    def _counting_llm_caller(messages, **kwargs):
        call_count[0] += 1
        return _deterministic_planner_caller(messages, **kwargs)

    planner = RecipeGroundedWeeklyPlanner(llm_caller=_counting_llm_caller)
    service = HealBiteRecipeGroundedService(
        catalog_path=str(catalog_path),
        weekly_menu_store=wk_store,
        household_service=hh_svc,
        inventory_store=inv_store,
        planner=planner,
        config=config,
        target_rights_scope="FR",
        guard=guard,
        stage_b_policy_enforced=True,
    )

    week_start = "2026-09-21"
    authors = list(TEST_AUTHORS.keys())
    cb_key = "telegram-rcp-gen:101:2026-09-21:query_9999"

    # Initial generation
    res1 = service.generate_menu(
        actor_user_id=101,
        week_start=week_start,
        authors=authors,
        idempotency_key=cb_key,
    )
    assert res1.status == RecipeGroundedStatus.SUCCESS
    assert res1.revision_view is not None
    initial_rev_id = res1.revision_view.revision.id
    assert call_count[0] == 1

    # Replayed callback delivery with same idempotency key
    res2 = service.generate_menu(
        actor_user_id=101,
        week_start=week_start,
        authors=authors,
        idempotency_key=cb_key,
    )
    assert res2.status == RecipeGroundedStatus.SUCCESS
    assert res2.revision_view is not None
    assert res2.revision_view.revision.id == initial_rev_id

    # ZERO additional provider LLM calls!
    assert call_count[0] == 1


# ==============================================================================
# 6. Rapid Duplicate Callback / Provider Amplification Protection
# ==============================================================================

def test_rapid_duplicate_callback_amplification_protection(tmp_path: Path) -> None:
    db_path, catalog_path, hh_svc, wk_store, inv_store, config, guard = _setup_stage_b_env(tmp_path, actor_ids=(101,))

    call_count = [0]

    def _slow_counting_llm_caller(messages, **kwargs):
        call_count[0] += 1
        time.sleep(0.15)  # Simulate real network provider latency
        return _deterministic_planner_caller(messages, **kwargs)

    planner = RecipeGroundedWeeklyPlanner(llm_caller=_slow_counting_llm_caller)
    service = HealBiteRecipeGroundedService(
        catalog_path=str(catalog_path),
        weekly_menu_store=wk_store,
        household_service=hh_svc,
        inventory_store=inv_store,
        planner=planner,
        config=config,
        target_rights_scope="FR",
        guard=guard,
        stage_b_policy_enforced=True,
    )

    week_start = "2026-09-21"
    authors = list(TEST_AUTHORS.keys())
    cb_key = "telegram-rcp-gen:101:2026-09-21:double_click_777"

    # Launch two simultaneous generation calls with the same key
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(
            service.generate_menu,
            actor_user_id=101,
            week_start=week_start,
            authors=authors,
            idempotency_key=cb_key,
        )
        # Small delay to ensure f1 enters and acquires guard first
        time.sleep(0.02)
        f2 = executor.submit(
            service.generate_menu,
            actor_user_id=101,
            week_start=week_start,
            authors=authors,
            idempotency_key=cb_key,
        )

        res1 = f1.result(timeout=10.0)
        res2 = f2.result(timeout=10.0)

    assert res1.status == RecipeGroundedStatus.SUCCESS
    assert res2.status == RecipeGroundedStatus.SUCCESS
    assert res1.revision_view is not None
    assert res2.revision_view is not None
    assert res1.revision_view.revision.id == res2.revision_view.revision.id

    # Crucial assertion: provider was called AT MOST ONCE!
    assert call_count[0] == 1


# ==============================================================================
# 7. Multi-Household Concurrency & Complete Isolation
# ==============================================================================

def test_multi_household_concurrency_and_isolation(tmp_path: Path) -> None:
    actors = (101, 102, 103)
    db_path, catalog_path, hh_svc, wk_store, inv_store, config, guard = _setup_stage_b_env(tmp_path, actor_ids=actors)

    # Seed distinct inventory for each household
    for aid, ing_name in zip(actors, ["морковь", "картофель", "лук"]):
        hh_context = hh_svc.resolve_existing_actor_household_context(aid)
        scope = InventoryOwnerScope(household_id=hh_context.household_id)
        snap = inv_store.create_snapshot(
            scope,
            InventorySourceType.TEXT,
            [
                InventoryItemInput(
                    display_name=ing_name,
                    quantity_value="500",
                    unit="g",
                    category="vegetables",
                    confidence="1.0",
                    uncertainty="confirmed",
                ),
            ],
        )
        inv_store.confirm_snapshot(scope, snap.snapshot.id)

    service = HealBiteRecipeGroundedService(
        catalog_path=str(catalog_path),
        weekly_menu_store=wk_store,
        household_service=hh_svc,
        inventory_store=inv_store,
        planner=RecipeGroundedWeeklyPlanner(llm_caller=_deterministic_planner_caller),
        config=config,
        target_rights_scope="FR",
        guard=guard,
        stage_b_policy_enforced=True,
    )

    week_start = "2026-09-21"
    authors = list(TEST_AUTHORS.keys())

    # Run simultaneous generations for all 3 households concurrently
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                service.generate_menu,
                actor_user_id=aid,
                week_start=week_start,
                authors=authors,
            ): aid
            for aid in actors
        }
        for fut in concurrent.futures.as_completed(futures):
            aid = futures[fut]
            results[aid] = fut.result(timeout=15.0)

    for aid in actors:
        assert results[aid].status == RecipeGroundedStatus.SUCCESS, f"Actor {aid} failed: {results[aid].error_message} (grounding: {results[aid].grounding_result})"
        assert results[aid].revision_view is not None

    revs = {aid: results[aid].revision_view for aid in actors}

    # Verify household and revision isolation
    series_ids = {r.series.id for r in revs.values()}
    revision_ids = {r.revision.id for r in revs.values()}
    assert len(series_ids) == 3, "Each household must have its own distinct series ID"
    assert len(revision_ids) == 3, "Each household must have its own distinct revision ID"

    # Verify entries: 21 per household, zero shared entry IDs
    all_entry_ids = set()
    for aid, rv in revs.items():
        assert len(rv.entries) == 21, f"Household {aid} must have exactly 21 meals"
        entry_ids = {e.id for e in rv.entries}
        assert not (entry_ids & all_entry_ids), "No shared entry IDs between households"
        all_entry_ids.update(entry_ids)

    # Verify recipe refs isolation in SQLite
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM household_weekly_menu_entry_recipe_refs").fetchall()
        assert len(rows) == 63  # 21 refs * 3 households = 63 refs

        # Check that each ref belongs to the correct household
        for row in rows:
            entry_id = row["entry_id"]
            hh_id = row["household_id"]
            # Find matching entry in revs
            matching = [aid for aid, rv in revs.items() if any(e.id == entry_id for e in rv.entries)]
            assert len(matching) == 1
            expected_aid = matching[0]
            expected_hh = hh_svc.resolve_existing_actor_household_context(expected_aid).household_id
            assert hh_id == expected_hh, "Cross-household recipe ref ownership detected!"

        # Verify DB integrity
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert len(fk_violations) == 0


# ==============================================================================
# 8. Same-Household Concurrency (Conflict or Replay, Zero Orphan Refs)
# ==============================================================================

def test_same_household_concurrency_clean_outcome(tmp_path: Path) -> None:
    db_path, catalog_path, hh_svc, wk_store, inv_store, config, guard = _setup_stage_b_env(tmp_path, actor_ids=(101,))

    def _slow_llm_caller(messages, **kwargs):
        time.sleep(0.1)
        return _deterministic_planner_caller(messages, **kwargs)

    service = HealBiteRecipeGroundedService(
        catalog_path=str(catalog_path),
        weekly_menu_store=wk_store,
        household_service=hh_svc,
        inventory_store=inv_store,
        planner=RecipeGroundedWeeklyPlanner(llm_caller=_slow_llm_caller),
        config=config,
        target_rights_scope="FR",
        guard=guard,
        stage_b_policy_enforced=True,
    )

    week_start = "2026-09-21"
    authors = list(TEST_AUTHORS.keys())

    # Two concurrent calls for same household with distinct idempotency keys
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(
            service.generate_menu,
            actor_user_id=101,
            week_start=week_start,
            authors=authors,
            idempotency_key="key_attempt_A",
        )
        time.sleep(0.01)
        f2 = executor.submit(
            service.generate_menu,
            actor_user_id=101,
            week_start=week_start,
            authors=authors,
            idempotency_key="key_attempt_B",
        )

        r1 = f1.result(timeout=10.0)
        r2 = f2.result(timeout=10.0)

    # One must succeed; the other must return CONCURRENT_GENERATION_IN_FLIGHT (clean conflict)
    statuses = {r1.status, r2.status}
    assert RecipeGroundedStatus.SUCCESS in statuses
    assert RecipeGroundedStatus.CONCURRENT_GENERATION_IN_FLIGHT in statuses

    # Verify DB has exactly 1 draft revision and 21 recipe refs (no orphan refs or duplicates)
    with sqlite3.connect(db_path) as conn:
        draft_count = conn.execute(
            "SELECT COUNT(*) FROM household_weekly_menus WHERE status = 'draft'"
        ).fetchone()[0]
        assert draft_count == 1

        ref_count = conn.execute(
            "SELECT COUNT(*) FROM household_weekly_menu_entry_recipe_refs"
        ).fetchone()[0]
        assert ref_count == 21


# ==============================================================================
# 9. Provider Failure Matrix & Repair Budget Control (Calls <= 3)
# ==============================================================================

def test_provider_repair_budget_and_failure_matrix(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.db"
    build_test_recipe_catalog(catalog_path)
    cat_store = HealBiteRecipeCatalogStore(catalog_path, read_only=True)
    from gateway.healbite_recipe_retrieval import RecipeRetriever
    retriever = RecipeRetriever(cat_store, target_rights_scope="FR")
    authors = list(TEST_AUTHORS.keys())
    slot_cands = {
        MealType.BREAKFAST: retriever.retrieve_candidates_for_slot(meal_slot=MealType.BREAKFAST, authors=authors),
        MealType.LUNCH: retriever.retrieve_candidates_for_slot(meal_slot=MealType.LUNCH, authors=authors),
        MealType.DINNER: retriever.retrieve_candidates_for_slot(meal_slot=MealType.DINNER, authors=authors),
    }
    all_cands = [c for cands in slot_cands.values() for c in cands]
    dates = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]

    # Case A: Timeout / Exception -> calls = 1, raises PlannerGroundingError
    calls_a = [0]
    def _timeout_caller(messages, **kwargs):
        calls_a[0] += 1
        raise TimeoutError("Provider connection timed out")

    planner_a = RecipeGroundedWeeklyPlanner(llm_caller=_timeout_caller)
    with pytest.raises(PlannerGroundingError) as exc_a:
        planner_a.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)
    assert calls_a[0] == 3  # Tried initial + 2 repairs before failing
    assert "Provider connection timed out" in str(exc_a.value)

    # Case B: Invalid JSON all 3 times -> calls = 3, raises PlannerGroundingError
    calls_b = [0]
    def _bad_json_caller(messages, **kwargs):
        calls_b[0] += 1
        return "This is not json at all"

    planner_b = RecipeGroundedWeeklyPlanner(llm_caller=_bad_json_caller)
    with pytest.raises(PlannerGroundingError) as exc_b:
        planner_b.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)
    assert calls_b[0] == 3
    assert "not valid JSON" in str(exc_b.value)

    # Case C: 20 meals instead of 21 -> calls = 3
    calls_c = [0]
    def _twenty_meals_caller(messages, **kwargs):
        calls_c[0] += 1
        # Generate 20 meals
        meals = []
        for i in range(20):
            cand = all_cands[i]
            meals.append({"date": dates[i % 7], "slot": cand.meal_types[0].value, "recipe_id": cand.recipe_id, "recipe_version": cand.recipe_version, "servings": 2})
        return json.dumps({"week_start": "2026-09-21", "meals": meals})

    planner_c = RecipeGroundedWeeklyPlanner(llm_caller=_twenty_meals_caller)
    with pytest.raises(PlannerGroundingError) as exc_c:
        planner_c.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)
    assert calls_c[0] == 3
    assert "expected exactly 21" in str(exc_c.value)

    # Case D: Unknown recipe ID -> calls = 3
    calls_d = [0]
    def _unknown_recipe_caller(messages, **kwargs):
        calls_d[0] += 1
        res = json.loads(_deterministic_planner_caller(messages, **kwargs))
        res["meals"][0]["recipe_id"] = "totally_unknown_fake_recipe_id"
        return json.dumps(res)

    planner_d = RecipeGroundedWeeklyPlanner(llm_caller=_unknown_recipe_caller)
    with pytest.raises(PlannerGroundingError) as exc_d:
        planner_d.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)
    assert calls_d[0] == 3
    assert "Unknown or unallowed recipe_id" in str(exc_d.value)

    # Case E: Duplicate recipe ID -> calls = 3
    calls_e = [0]
    def _duplicate_recipe_caller(messages, **kwargs):
        calls_e[0] += 1
        res = json.loads(_deterministic_planner_caller(messages, **kwargs))
        res["meals"][1]["recipe_id"] = res["meals"][0]["recipe_id"]
        return json.dumps(res)

    planner_e = RecipeGroundedWeeklyPlanner(llm_caller=_duplicate_recipe_caller)
    with pytest.raises(PlannerGroundingError) as exc_e:
        planner_e.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)
    assert calls_e[0] == 3
    assert "Duplicate recipe_id" in str(exc_e.value)

    # Case F: Repair succeeds on attempt 2 -> calls = 2
    calls_f = [0]
    def _repair_at_2_caller(messages, **kwargs):
        calls_f[0] += 1
        if calls_f[0] == 1:
            return "bad json"
        return _deterministic_planner_caller(messages, **kwargs)

    planner_f = RecipeGroundedWeeklyPlanner(llm_caller=_repair_at_2_caller)
    plan_f = planner_f.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)
    assert calls_f[0] == 2
    assert plan_f.attempts == 2
    assert plan_f.repairs == 1
    assert len(plan_f.meals) == 21

    # Case G: Repair succeeds on attempt 3 -> calls = 3
    calls_g = [0]
    def _repair_at_3_caller(messages, **kwargs):
        calls_g[0] += 1
        if calls_g[0] < 3:
            return "bad json"
        return _deterministic_planner_caller(messages, **kwargs)

    planner_g = RecipeGroundedWeeklyPlanner(llm_caller=_repair_at_3_caller)
    plan_g = planner_g.plan(week_start="2026-09-21", dates=dates, slot_candidates=slot_cands)
    assert calls_g[0] == 3
    assert plan_g.attempts == 3
    assert plan_g.repairs == 2
    assert len(plan_g.meals) == 21


# ==============================================================================
# 10. Observability / Safe Pseudonym Logging (No Secret or Raw ID Leaks)
# ==============================================================================

def test_observability_safe_pseudonymization_no_id_leaks(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db_path, catalog_path, hh_svc, wk_store, inv_store, config, guard = _setup_stage_b_env(tmp_path, actor_ids=(987654321,))

    service = HealBiteRecipeGroundedService(
        catalog_path=str(catalog_path),
        weekly_menu_store=wk_store,
        household_service=hh_svc,
        inventory_store=inv_store,
        planner=RecipeGroundedWeeklyPlanner(llm_caller=_deterministic_planner_caller),
        config=config,
        target_rights_scope="FR",
        guard=guard,
        stage_b_policy_enforced=True,
    )

    with caplog.at_level(logging.INFO):
        res = service.generate_menu(
            actor_user_id=987654321,
            week_start="2026-09-21",
            authors=list(TEST_AUTHORS.keys()),
        )

    assert res.status == RecipeGroundedStatus.SUCCESS

    # Verify safe pseudonym helper works
    expected_hash = safe_pseudonym(987654321)
    assert len(expected_hash) == 8

    # Assert raw actor user ID '987654321' is NOT printed anywhere in the log records
    for record in caplog.records:
        assert "987654321" not in record.message, f"Raw Telegram/actor ID leaked in log: {record.message}"

    # Assert structured log messages contain the expected actor_hash
    log_text = "\n".join(r.message for r in caplog.records)
    assert f"actor_hash={expected_hash}" in log_text
    assert "[RecipeGroundedService][recipe_generation_started]" in log_text
    assert "[RecipeGroundedService][recipe_generation_success]" in log_text


# ==============================================================================
# 11. Rollback-to-Stage-A Configuration Contract
# ==============================================================================

def test_rollback_to_stage_a_cohort_configuration() -> None:
    # Stage B cohort: 3 allowlisted users
    stage_b_cohort = [101, 102, 103]
    decision_b = evaluate_stage_b_cohort_policy(stage_b_cohort, public_access=False, target_rights_scope="FR")
    assert decision_b.valid
    assert decision_b.allowlist_count == 3

    # Stage A rollback cohort: reduce allowlist back to single canary operator
    stage_a_rollback_cohort = [101]
    decision_a = evaluate_stage_b_cohort_policy(stage_a_rollback_cohort, public_access=False, target_rights_scope="FR")
    assert decision_a.valid
    assert decision_a.allowlist_count == 1
    assert decision_a.public_access is False
    assert decision_a.rights_scope == "FR"
