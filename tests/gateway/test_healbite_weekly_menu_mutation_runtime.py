from __future__ import annotations

import sqlite3
from pathlib import Path

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, new_household_member_id
from gateway.healbite_households import HealBiteHouseholdStore, HouseholdContext, HouseholdMemberStatus, HouseholdRole, HouseholdStatus
from gateway.healbite_runtime_resources import borrowed_runtime_resource
from gateway.healbite_weekly_menu_mutation_runtime import (
    HealBiteWeeklyMenuMutationRuntimeService,
    WeeklyMenuMutationStatus,
)
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore, WeeklyMenuEntryInput, WeeklyMenuMealSlot


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                {identity_column} INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _insert_user(db_path: Path, user_id: int, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO users ({identity_column}, username) VALUES (?, ?)",
            (int(user_id), f"user-{user_id}"),
        )


def _seed_runtime(db_path: Path, actor_user_id: int = 101) -> tuple[HealBiteWeeklyMenuStore, HouseholdContext]:
    _create_users_table(db_path)
    _insert_user(db_path, actor_user_id)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(actor_user_id)
    context = household_store.resolve_actor_context(actor_user_id)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    return weekly_store, context


def _add_active_member(
    db_path: Path,
    *,
    household_id: str,
    linked_user_id: int,
    role: HouseholdRole,
) -> HouseholdContext:
    _insert_user(db_path, linked_user_id)
    member_id = new_household_member_id()
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, display_name, member_type, role, status, age_band, created_at, updated_at, version)
            VALUES (?, ?, ?, ?, 'linked_adult', ?, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)
            """,
            (member_id, household_id, linked_user_id, f"user-{linked_user_id}", role.value),
        )
    return HouseholdContext(
        actor_user_id=linked_user_id,
        household_id=household_id,
        household_member_id=member_id,
        role=role,
        member_status=HouseholdMemberStatus.ACTIVE,
        household_status=HouseholdStatus.ACTIVE,
    )


def _sample_entries(title: str = "Суп") -> list[WeeklyMenuEntryInput]:
    return [
        WeeklyMenuEntryInput(
            local_date="2026-07-06",
            meal_slot=WeeklyMenuMealSlot.LUNCH,
            position=1,
            title=title,
            servings="2",
        )
    ]


class _CountingHouseholdStoreFactory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return borrowed_runtime_resource(HealBiteHouseholdStore(db_path=self.db_path, ensure_schema_on_init=False))


class _CountingWeeklyStoreFactory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return borrowed_runtime_resource(HealBiteWeeklyMenuStore(db_path=self.db_path))


def test_disabled_gate_does_not_open_any_store(tmp_path):
    db_path = tmp_path / "mutation.db"
    household_factory = _CountingHouseholdStoreFactory(db_path)
    weekly_factory = _CountingWeeklyStoreFactory(db_path)
    service = HealBiteWeeklyMenuMutationRuntimeService(
        config=FeatureGateConfig(enabled=False, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        weekly_menu_store_factory=weekly_factory,
    )

    result = service.create_draft_for_week(101, "2026-07-06", expected_series_version=None, idempotency_key="draft-1")

    assert result.status is WeeklyMenuMutationStatus.DISABLED
    assert household_factory.calls == 0
    assert weekly_factory.calls == 0


def test_owner_can_run_full_mutation_lifecycle(tmp_path):
    db_path = tmp_path / "mutation.db"
    weekly_store, context = _seed_runtime(db_path)
    service = HealBiteWeeklyMenuMutationRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({context.actor_user_id}), configuration_valid=True),
        db_path=db_path,
    )

    draft = service.create_draft_for_week(context.actor_user_id, "2026-07-06", expected_series_version=None, idempotency_key="draft-1")
    assert draft.success is True
    assert draft.revision_view is not None
    replace = service.replace_draft_entries(
        context.actor_user_id,
        draft.revision_view.revision.id,
        _sample_entries("Борщ"),
        expected_revision_version=draft.revision_view.revision.version,
        idempotency_key="replace-1",
    )
    assert replace.success is True
    publish = service.publish_draft(
        context.actor_user_id,
        replace.revision_view.revision.id,
        expected_series_version=replace.revision_view.series.version,
        expected_revision_version=replace.revision_view.revision.version,
        idempotency_key="publish-1",
    )
    assert publish.success is True
    archive = service.archive_revision(
        context.actor_user_id,
        publish.revision_view.revision.id,
        expected_series_version=publish.revision_view.series.version,
        expected_revision_version=publish.revision_view.revision.version,
        idempotency_key="archive-1",
    )

    assert archive.success is True
    assert archive.revision_view is not None
    assert archive.revision_view.revision.status.value == "archived"
    latest = weekly_store.get_weekly_menu_revision(context, archive.revision_view.revision.id)
    assert latest.revision.status.value == "archived"


def test_adult_admin_is_still_forbidden_by_runtime(tmp_path):
    db_path = tmp_path / "mutation.db"
    weekly_store, owner_context = _seed_runtime(db_path)
    admin_context = _add_active_member(
        db_path,
        household_id=owner_context.household_id,
        linked_user_id=202,
        role=HouseholdRole.ADULT_ADMIN,
    )
    series = weekly_store.create_or_get_weekly_menu_series(owner_context, owner_context.household_id, "2026-07-06")
    service = HealBiteWeeklyMenuMutationRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101, 202}), configuration_valid=True),
        db_path=db_path,
    )

    result = service.create_draft_for_week(admin_context.actor_user_id, "2026-07-06", expected_series_version=series.version, idempotency_key="draft-1")

    assert result.status is WeeklyMenuMutationStatus.FORBIDDEN


def test_cross_household_revision_is_not_found(tmp_path):
    db_path = tmp_path / "mutation.db"
    weekly_store, owner_context = _seed_runtime(db_path, actor_user_id=101)
    _create_users_table(db_path)
    _insert_user(db_path, 202)
    other_household_store = HealBiteHouseholdStore(db_path=db_path)
    other_household_store.get_or_create_personal_household(202)
    series = weekly_store.create_or_get_weekly_menu_series(owner_context, owner_context.household_id, "2026-07-06")
    draft = weekly_store.create_draft_revision(owner_context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    service = HealBiteWeeklyMenuMutationRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101, 202}), configuration_valid=True),
        db_path=db_path,
    )

    result = service.replace_draft_entries(
        202,
        draft.revision.id,
        _sample_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-1",
    )

    assert result.status is WeeklyMenuMutationStatus.NOT_FOUND


def test_same_idempotency_key_with_different_payload_is_typed_conflict(tmp_path):
    db_path = tmp_path / "mutation.db"
    _, context = _seed_runtime(db_path)
    service = HealBiteWeeklyMenuMutationRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({context.actor_user_id}), configuration_valid=True),
        db_path=db_path,
    )
    draft = service.create_draft_for_week(context.actor_user_id, "2026-07-06", expected_series_version=None, idempotency_key="draft-1")

    first = service.replace_draft_entries(
        context.actor_user_id,
        draft.revision_view.revision.id,
        _sample_entries("Первый"),
        expected_revision_version=draft.revision_view.revision.version,
        idempotency_key="replace-1",
    )
    second = service.replace_draft_entries(
        context.actor_user_id,
        first.revision_view.revision.id,
        _sample_entries("Второй"),
        expected_revision_version=first.revision_view.revision.version,
        idempotency_key="replace-1",
    )

    assert first.success is True
    assert second.status is WeeklyMenuMutationStatus.IDEMPOTENCY_CONFLICT
