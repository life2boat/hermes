from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, new_household_member_id
from gateway.healbite_households import (
    HealBiteHouseholdStore,
    HouseholdContext,
    HouseholdMemberStatus,
    HouseholdRole,
    HouseholdStatus,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    HouseholdAuthorizationContext,
    WeeklyMenuAccessError,
    WeeklyMenuConflictError,
    WeeklyMenuEntryInput,
    WeeklyMenuMealSlot,
    WeeklyMenuNotFoundError,
    WeeklyMenuRevisionStatus,
    WeeklyMenuSchemaError,
    WeeklyMenuStateError,
    WeeklyMenuValidationError,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _create_nutrition_log_table(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nutrition_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                meal_name TEXT NOT NULL,
                calories_kcal REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _insert_user(db_path: Path, user_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (int(user_id), f"user-{user_id}"))


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _seed_personal_household(db_path: Path, actor_user_id: int = 101):
    _create_users_table(db_path)
    _insert_user(db_path, actor_user_id)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    personal = household_store.get_or_create_personal_household(actor_user_id)
    context = household_store.resolve_actor_context(actor_user_id)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    return household_store, weekly_store, personal, context


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


def _sample_entries(*, week_start: str = "2026-07-06") -> list[WeeklyMenuEntryInput]:
    return [
        WeeklyMenuEntryInput(
            local_date=week_start,
            meal_slot=WeeklyMenuMealSlot.BREAKFAST,
            position=1,
            title="Овсянка",
            description="С ягодами",
            servings="2",
        ),
        WeeklyMenuEntryInput(
            local_date="2026-07-07",
            meal_slot=WeeklyMenuMealSlot.DINNER,
            position=1,
            title="Рыба",
            description=None,
            servings="3",
        ),
    ]


def test_read_methods_fail_closed_without_creating_missing_db(tmp_path):
    db_path = tmp_path / "missing.db"
    store = HealBiteWeeklyMenuStore(db_path=db_path)
    context = HouseholdAuthorizationContext(
        actor_user_id=101,
        household_id="11111111-1111-4111-8111-111111111111",
        household_member_id="22222222-2222-4222-8222-222222222222",
        role=HouseholdRole.OWNER,
        member_status=HouseholdMemberStatus.ACTIVE,
        household_status=HouseholdStatus.ACTIVE,
    )

    assert store.schema_state().value == "not_initialized"
    with pytest.raises(WeeklyMenuSchemaError):
        store.get_weekly_menu_series(context, context.household_id, "2026-07-06")
    assert not db_path.exists()


def test_create_or_get_series_is_idempotent_for_same_household_week(tmp_path):
    db_path = tmp_path / "weekly.db"
    _, store, _, context = _seed_personal_household(db_path)

    first = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    second = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")

    assert first.id == second.id
    assert first.week_start == "2026-07-06"


def test_adult_member_is_read_only_for_weekly_menu_mutations(tmp_path):
    db_path = tmp_path / "roles.db"
    _, store, personal, owner_context = _seed_personal_household(db_path)
    member_context = _add_active_member(
        db_path,
        household_id=personal.household.id,
        linked_user_id=202,
        role=HouseholdRole.ADULT_MEMBER,
    )
    series = store.create_or_get_weekly_menu_series(owner_context, owner_context.household_id, "2026-07-06")

    with pytest.raises(WeeklyMenuAccessError):
        store.create_draft_revision(member_context, series.id, expected_series_version=series.version, idempotency_key="draft-1")

    assert store.get_weekly_menu_series(member_context, personal.household.id, "2026-07-06") is not None


def test_draft_publish_and_copy_forward_flow(tmp_path):
    db_path = tmp_path / "flow.db"
    _, store, _, context = _seed_personal_household(db_path)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")

    draft = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    updated = store.replace_draft_entries(
        context,
        draft.revision.id,
        _sample_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-1",
    )
    published = store.publish_weekly_menu_revision(
        context,
        updated.revision.id,
        expected_series_version=updated.series.version,
        expected_revision_version=updated.revision.version,
        idempotency_key="publish-1",
    )
    copied = store.create_draft_revision(
        context,
        series.id,
        expected_series_version=published.series.version,
        idempotency_key="draft-2",
    )

    assert published.revision.status is WeeklyMenuRevisionStatus.PUBLISHED
    assert len(published.entries) == 2
    assert copied.revision.status is WeeklyMenuRevisionStatus.DRAFT
    assert [entry.title for entry in copied.entries] == [entry.title for entry in published.entries]
    assert copied.revision.source_revision_id == published.revision.id


def test_publishing_new_revision_archives_previous_published(tmp_path):
    db_path = tmp_path / "archive.db"
    _, store, _, context = _seed_personal_household(db_path)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    first_draft = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-a")
    first_ready = store.replace_draft_entries(
        context,
        first_draft.revision.id,
        _sample_entries(),
        expected_revision_version=first_draft.revision.version,
        idempotency_key="replace-a",
    )
    first_published = store.publish_weekly_menu_revision(
        context,
        first_ready.revision.id,
        expected_series_version=first_ready.series.version,
        expected_revision_version=first_ready.revision.version,
        idempotency_key="publish-a",
    )
    second_draft = store.create_draft_revision(
        context,
        series.id,
        expected_series_version=first_published.series.version,
        idempotency_key="draft-b",
    )
    second_ready = store.replace_draft_entries(
        context,
        second_draft.revision.id,
        [
            WeeklyMenuEntryInput(
                local_date="2026-07-08",
                meal_slot=WeeklyMenuMealSlot.LUNCH,
                position=1,
                title="Суп",
            )
        ],
        expected_revision_version=second_draft.revision.version,
        idempotency_key="replace-b",
    )
    second_published = store.publish_weekly_menu_revision(
        context,
        second_ready.revision.id,
        expected_series_version=second_ready.series.version,
        expected_revision_version=second_ready.revision.version,
        idempotency_key="publish-b",
    )
    revisions = store.list_weekly_menu_revisions(context, series.id)

    assert second_published.revision.status is WeeklyMenuRevisionStatus.PUBLISHED
    archived = [revision for revision in revisions if revision.id == first_published.revision.id][0]
    assert archived.status is WeeklyMenuRevisionStatus.ARCHIVED
    assert sum(1 for revision in revisions if revision.status is WeeklyMenuRevisionStatus.PUBLISHED) == 1


def test_replace_entries_rejects_out_of_week_and_duplicate_slots(tmp_path):
    db_path = tmp_path / "validation.db"
    _, store, _, context = _seed_personal_household(db_path)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")

    with pytest.raises(WeeklyMenuValidationError):
        store.replace_draft_entries(
            context,
            draft.revision.id,
            [
                WeeklyMenuEntryInput(local_date="2026-07-20", meal_slot=WeeklyMenuMealSlot.BREAKFAST, position=1, title="X")
            ],
            expected_revision_version=draft.revision.version,
            idempotency_key="replace-outside",
        )
    with pytest.raises(WeeklyMenuValidationError):
        store.replace_draft_entries(
            context,
            draft.revision.id,
            [
                WeeklyMenuEntryInput(local_date="2026-07-06", meal_slot=WeeklyMenuMealSlot.BREAKFAST, position=1, title="A"),
                WeeklyMenuEntryInput(local_date="2026-07-06", meal_slot=WeeklyMenuMealSlot.BREAKFAST, position=1, title="B"),
            ],
            expected_revision_version=draft.revision.version,
            idempotency_key="replace-dup",
        )


def test_idempotency_replays_same_result_and_rejects_different_payload(tmp_path):
    db_path = tmp_path / "idem.db"
    _, store, _, context = _seed_personal_household(db_path)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")

    first = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    replay = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")

    assert first.revision.id == replay.revision.id
    with pytest.raises(WeeklyMenuConflictError):
        store.publish_weekly_menu_revision(
            context,
            first.revision.id,
            expected_series_version=series.version,
            expected_revision_version=first.revision.version,
            idempotency_key="draft-1",
        )


def test_version_mismatches_raise_conflict(tmp_path):
    db_path = tmp_path / "versions.db"
    _, store, _, context = _seed_personal_household(db_path)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")

    with pytest.raises(WeeklyMenuConflictError):
        store.replace_draft_entries(
            context,
            draft.revision.id,
            _sample_entries(),
            expected_revision_version=draft.revision.version + 1,
            idempotency_key="replace-1",
        )


def test_archive_revision_is_supported_and_readable(tmp_path):
    db_path = tmp_path / "archive-direct.db"
    _, store, _, context = _seed_personal_household(db_path)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    ready = store.replace_draft_entries(
        context,
        draft.revision.id,
        _sample_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-1",
    )
    archived = store.archive_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="archive-1",
    )

    assert archived.revision.status is WeeklyMenuRevisionStatus.ARCHIVED
    view = store.get_weekly_menu_revision(context, archived.revision.id)
    assert view.revision.status is WeeklyMenuRevisionStatus.ARCHIVED


def test_nonexistent_revision_raises_not_found(tmp_path):
    db_path = tmp_path / "missing-revision.db"
    _, store, _, context = _seed_personal_household(db_path)

    with pytest.raises(WeeklyMenuNotFoundError):
        store.get_weekly_menu_revision(context, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_apply_generated_draft_entries_creates_generated_draft_and_replays(tmp_path):
    db_path = tmp_path / "generated-create.db"
    _, store, _, context = _seed_personal_household(db_path)
    payload_hash = hashlib.sha256(b"generated-create").hexdigest()

    view = store.apply_generated_draft_entries(
        context,
        week_start="2026-07-06",
        entries=[
            WeeklyMenuEntryInput(
                local_date="2026-07-06",
                meal_slot=WeeklyMenuMealSlot.BREAKFAST,
                position=1,
                title="Generated breakfast",
                origin="generated",
            )
        ],
        expected_series_version=None,
        expected_draft_revision_id=None,
        expected_draft_revision_version=None,
        idempotency_key="generate:abc",
        payload_hash=payload_hash,
    )
    replay = store.lookup_generated_draft_replay(
        context,
        idempotency_key="generate:abc",
        payload_hash=payload_hash,
    )

    assert view.revision.status is WeeklyMenuRevisionStatus.DRAFT
    assert len(view.entries) == 1
    assert view.entries[0].origin.value == "generated"
    assert replay is not None
    assert replay.revision.id == view.revision.id


def test_apply_generated_draft_entries_replaces_existing_draft_with_expected_version(tmp_path):
    db_path = tmp_path / "generated-replace.db"
    _, store, _, context = _seed_personal_household(db_path)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    payload_hash = hashlib.sha256(b"generated-replace").hexdigest()

    replaced = store.apply_generated_draft_entries(
        context,
        week_start="2026-07-06",
        entries=[
            WeeklyMenuEntryInput(
                local_date="2026-07-06",
                meal_slot=WeeklyMenuMealSlot.DINNER,
                position=1,
                title="Generated dinner",
                origin="generated",
            )
        ],
        expected_series_version=draft.series.version,
        expected_draft_revision_id=draft.revision.id,
        expected_draft_revision_version=draft.revision.version,
        idempotency_key="generate:def",
        payload_hash=payload_hash,
    )

    assert replaced.revision.id == draft.revision.id
    assert replaced.revision.version == draft.revision.version + 1
    assert [entry.title for entry in replaced.entries] == ["Generated dinner"]


def test_audit_schema_returns_aggregate_counts_only(tmp_path):
    db_path = tmp_path / "audit.db"
    _, store, _, context = _seed_personal_household(db_path)
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    ready = store.replace_draft_entries(
        context,
        draft.revision.id,
        _sample_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-1",
    )
    store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="publish-1",
    )

    audit = store.audit_schema()

    assert audit.schema_state.value == "canonical"
    assert audit.series_count == 1
    assert audit.revision_count == 1
    assert audit.entry_count == 2
    assert audit.orphan_revision_count == 0
    assert audit.orphan_entry_count == 0
    assert audit.cross_household_inconsistency_count == 0


def test_weekly_menu_mutations_do_not_touch_nutrition_log(tmp_path):
    db_path = tmp_path / "nutrition.db"
    _create_nutrition_log_table(db_path)
    _, store, _, context = _seed_personal_household(db_path)
    with _connect(db_path) as conn:
        before = _count(conn, "nutrition_log")
    series = store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    ready = store.replace_draft_entries(
        context,
        draft.revision.id,
        _sample_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-1",
    )
    store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="publish-1",
    )
    with _connect(db_path) as conn:
        after = _count(conn, "nutrition_log")

    assert before == after
