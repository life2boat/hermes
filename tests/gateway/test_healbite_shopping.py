from __future__ import annotations

import sqlite3
import threading
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
from gateway.healbite_shopping import (
    GeneratedShoppingItemInput,
    HealBiteShoppingStore,
    ManualShoppingItemInput,
    ShoppingAccessError,
    ShoppingConflictError,
    ShoppingNotFoundError,
    ShoppingSchemaError,
    ShoppingStateError,
    ShoppingValidationError,
)
from gateway.healbite_shopping_schema import (
    SHOPPING_IDEMPOTENCY_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LISTS_TABLE,
    ShoppingItemOrigin,
    ShoppingItemOverrideState,
    ShoppingListStatus,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuEntryInput,
    WeeklyMenuMealSlot,
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
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    return household_store, weekly_store, shopping_store, personal, context


def _sample_menu_entries() -> list[WeeklyMenuEntryInput]:
    return [
        WeeklyMenuEntryInput(
            local_date="2026-07-06",
            meal_slot=WeeklyMenuMealSlot.LUNCH,
            position=1,
            title="Салат",
        ),
        WeeklyMenuEntryInput(
            local_date="2026-07-07",
            meal_slot=WeeklyMenuMealSlot.DINNER,
            position=1,
            title="Рыба",
        ),
    ]


def _publish_menu_revision(db_path: Path, actor_user_id: int = 101):
    _, weekly_store, _, personal, context = _seed_personal_household(db_path, actor_user_id)
    series = weekly_store.create_or_get_weekly_menu_series(context, personal.household.id, "2026-07-06")
    draft = weekly_store.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key="menu-draft-1",
    )
    ready = weekly_store.replace_draft_entries(
        context,
        draft.revision.id,
        _sample_menu_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="menu-replace-1",
    )
    published = weekly_store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="menu-publish-1",
    )
    return weekly_store, published, context, personal.household.id


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


def test_read_methods_fail_closed_without_creating_missing_db(tmp_path):
    db_path = tmp_path / "missing.db"
    store = HealBiteShoppingStore(db_path=db_path)
    context = HouseholdContext(
        actor_user_id=101,
        household_id="11111111-1111-4111-8111-111111111111",
        household_member_id="22222222-2222-4222-8222-222222222222",
        role=HouseholdRole.OWNER,
        member_status=HouseholdMemberStatus.ACTIVE,
        household_status=HouseholdStatus.ACTIVE,
    )

    with pytest.raises(ShoppingSchemaError):
        store.get_shopping_list(context, "33333333-3333-4333-8333-333333333333")
    assert not db_path.exists()


def test_create_standalone_shopping_list_and_manual_item(tmp_path):
    db_path = tmp_path / "standalone.db"
    _, _, store, personal, context = _seed_personal_household(db_path)

    created = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    with_item = store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Томаты", quantity_value="2", quantity_unit_normalized="piece"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="manual-1",
    )

    assert with_item.shopping_list.status is ShoppingListStatus.DRAFT
    assert with_item.shopping_list.source_menu_id is None
    assert len(with_item.items) == 1
    assert with_item.items[0].origin is ShoppingItemOrigin.MANUAL
    assert with_item.items[0].display_name == "Томаты"


def test_create_derived_list_retains_exact_source_revision_after_new_menu_publish(tmp_path):
    db_path = tmp_path / "derived.db"
    weekly_store, published, context, household_id = _publish_menu_revision(db_path)
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    created = shopping_store.create_shopping_list(
        context,
        household_id,
        week_start="2026-07-06",
        idempotency_key="list-1",
        source_menu_id=published.revision.id,
    )

    next_draft = weekly_store.create_draft_revision(
        context,
        published.series.id,
        expected_series_version=published.series.version,
        idempotency_key="menu-draft-2",
    )
    next_ready = weekly_store.replace_draft_entries(
        context,
        next_draft.revision.id,
        [
            WeeklyMenuEntryInput(
                local_date="2026-07-08",
                meal_slot=WeeklyMenuMealSlot.LUNCH,
                position=1,
                title="Суп",
            )
        ],
        expected_revision_version=next_draft.revision.version,
        idempotency_key="menu-replace-2",
    )
    weekly_store.publish_weekly_menu_revision(
        context,
        next_ready.revision.id,
        expected_series_version=next_ready.series.version,
        expected_revision_version=next_ready.revision.version,
        idempotency_key="menu-publish-2",
    )
    reloaded = shopping_store.get_shopping_list(context, created.shopping_list.id)

    assert reloaded.shopping_list.source_menu_id == published.revision.id
    assert reloaded.shopping_list.source_menu_revision == published.revision.revision_number


def test_cross_household_source_revision_is_refused(tmp_path):
    db_path = tmp_path / "cross-household.db"
    _, _, store_a, personal_a, context_a = _seed_personal_household(db_path, 101)
    _, published_b, _, _ = _publish_menu_revision(db_path, 202)

    with pytest.raises(ShoppingAccessError):
        store_a.create_shopping_list(
            context_a,
            personal_a.household.id,
            week_start="2026-07-06",
            idempotency_key="cross-1",
            source_menu_id=published_b.revision.id,
        )


def test_adult_member_can_edit_items_but_not_lifecycle(tmp_path):
    db_path = tmp_path / "roles.db"
    _, _, store, personal, owner_context = _seed_personal_household(db_path)
    member_context = _add_active_member(
        db_path,
        household_id=personal.household.id,
        linked_user_id=202,
        role=HouseholdRole.ADULT_MEMBER,
    )
    created = store.create_shopping_list(
        owner_context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )

    with pytest.raises(ShoppingAccessError):
        store.activate_shopping_list(
            member_context,
            created.shopping_list.id,
            expected_version=created.shopping_list.version,
            idempotency_key="activate-1",
        )

    updated = store.add_manual_item(
        member_context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Огурцы", quantity_value="1", quantity_unit_normalized="piece"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="member-manual-1",
    )
    checked = store.set_item_checked(
        member_context,
        updated.items[0].id,
        True,
        expected_list_version=updated.shopping_list.version,
        idempotency_key="member-check-1",
    )

    assert checked.items[0].checked_state is True


def test_activate_complete_archive_lifecycle(tmp_path):
    db_path = tmp_path / "lifecycle.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    created = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    active = store.activate_shopping_list(
        context,
        created.shopping_list.id,
        expected_version=created.shopping_list.version,
        idempotency_key="activate-1",
    )
    completed = store.complete_shopping_list(
        context,
        active.shopping_list.id,
        expected_version=active.shopping_list.version,
        idempotency_key="complete-1",
    )
    archived = store.archive_shopping_list(
        context,
        completed.shopping_list.id,
        expected_version=completed.shopping_list.version,
        idempotency_key="archive-1",
    )

    assert active.shopping_list.status is ShoppingListStatus.ACTIVE
    assert completed.shopping_list.status is ShoppingListStatus.COMPLETED
    assert archived.shopping_list.status is ShoppingListStatus.ARCHIVED
    with pytest.raises(ShoppingStateError):
        store.add_manual_item(
            context,
            archived.shopping_list.id,
            ManualShoppingItemInput(display_name="Лук", quantity_value="1", quantity_unit_normalized="piece"),
            expected_list_version=archived.shopping_list.version,
            idempotency_key="after-archive",
        )


def test_single_active_scope_is_household_week(tmp_path):
    db_path = tmp_path / "single-active.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    first = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    second = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-2",
    )
    active = store.activate_shopping_list(
        context,
        first.shopping_list.id,
        expected_version=first.shopping_list.version,
        idempotency_key="activate-1",
    )

    with pytest.raises(ShoppingConflictError):
        store.activate_shopping_list(
            context,
            second.shopping_list.id,
            expected_version=second.shopping_list.version,
            idempotency_key="activate-2",
        )

    assert active.shopping_list.status is ShoppingListStatus.ACTIVE


def test_generated_regeneration_preserves_manual_checked_and_overrides(tmp_path):
    db_path = tmp_path / "regen.db"
    _, published, context, household_id = _publish_menu_revision(db_path)
    store = HealBiteShoppingStore(db_path=db_path)
    created = store.create_shopping_list(
        context,
        household_id,
        week_start="2026-07-06",
        idempotency_key="list-1",
        source_menu_id=published.revision.id,
    )
    first = store.replace_or_regenerate_generated_items(
        context,
        created.shopping_list.id,
        [
            GeneratedShoppingItemInput(display_name="Томаты", quantity_value="2", quantity_unit_normalized="piece"),
            GeneratedShoppingItemInput(display_name="Салат", quantity_value="1", quantity_unit_normalized="piece"),
        ],
        expected_version=created.shopping_list.version,
        idempotency_key="regen-1",
    )
    manual = store.add_manual_item(
        context,
        first.shopping_list.id,
        ManualShoppingItemInput(display_name="Хлеб", quantity_value="1", quantity_unit_normalized="package"),
        expected_list_version=first.shopping_list.version,
        idempotency_key="manual-1",
    )
    checked_tomatoes = store.set_item_checked(
        context,
        manual.items[0].id if manual.items[0].origin is ShoppingItemOrigin.MENU_GENERATED else manual.items[1].id,
        True,
        expected_list_version=manual.shopping_list.version,
        idempotency_key="check-1",
    )
    generated_item = next(item for item in checked_tomatoes.items if item.origin is ShoppingItemOrigin.MENU_GENERATED and item.display_name == "Салат")
    overridden = store.update_item(
        context,
        generated_item.id,
        expected_list_version=checked_tomatoes.shopping_list.version,
        idempotency_key="override-1",
        quantity_value="3",
    )
    second = store.replace_or_regenerate_generated_items(
        context,
        overridden.shopping_list.id,
        [
            GeneratedShoppingItemInput(display_name="Томаты", quantity_value="2", quantity_unit_normalized="piece"),
            GeneratedShoppingItemInput(display_name="Огурцы", quantity_value="1", quantity_unit_normalized="piece"),
        ],
        expected_version=overridden.shopping_list.version,
        idempotency_key="regen-2",
    )

    names = [item.display_name for item in second.items]
    assert "Хлеб" in names
    assert "Томаты" in names
    assert "Огурцы" in names
    salad = next(item for item in second.items if item.display_name == "Салат")
    tomatoes = next(item for item in second.items if item.display_name == "Томаты")
    cucumbers = next(item for item in second.items if item.display_name == "Огурцы")
    assert salad.override_state is ShoppingItemOverrideState.MANUALIZED
    assert tomatoes.checked_state is True
    assert cucumbers.checked_state is False


def test_regeneration_deduplicates_same_name_and_unit_but_not_incompatible_units(tmp_path):
    db_path = tmp_path / "dedup.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    created = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    first = store.replace_or_regenerate_generated_items(
        context,
        created.shopping_list.id,
        [
            GeneratedShoppingItemInput(display_name="Томаты", quantity_value="1", quantity_unit_normalized="kg"),
            GeneratedShoppingItemInput(display_name="  томаты  ", quantity_value="0.5", quantity_unit_normalized="kg"),
            GeneratedShoppingItemInput(display_name="Томаты", quantity_value="2", quantity_unit_normalized="piece"),
        ],
        expected_version=created.shopping_list.version,
        idempotency_key="regen-1",
    )

    assert len(first.items) == 2
    mass_item = next(item for item in first.items if item.quantity_unit_normalized.value == "kg")
    piece_item = next(item for item in first.items if item.quantity_unit_normalized.value == "piece")
    assert mass_item.quantity_value == "1.5"
    assert piece_item.quantity_value == "2"


def test_regeneration_rejects_ambiguous_unknown_quantity_merge(tmp_path):
    db_path = tmp_path / "ambiguous.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    created = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )

    with pytest.raises(ShoppingValidationError):
        store.replace_or_regenerate_generated_items(
            context,
            created.shopping_list.id,
            [
                GeneratedShoppingItemInput(display_name="Специи", quantity_value=None, quantity_unit_normalized="unknown"),
                GeneratedShoppingItemInput(display_name="специи", quantity_value=None, quantity_unit_normalized="unknown"),
            ],
            expected_version=created.shopping_list.version,
            idempotency_key="regen-1",
        )


def test_same_key_same_payload_replays_but_different_payload_conflicts(tmp_path):
    db_path = tmp_path / "idempotency.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    created = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    first = store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Хлеб", quantity_value="1", quantity_unit_normalized="package"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="same-key",
    )
    replay = store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Хлеб", quantity_value="1", quantity_unit_normalized="package"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="same-key",
    )

    assert first.shopping_list.id == replay.shopping_list.id
    with pytest.raises(ShoppingConflictError):
        store.add_manual_item(
            context,
            created.shopping_list.id,
            ManualShoppingItemInput(display_name="Молоко", quantity_value="1", quantity_unit_normalized="l"),
            expected_list_version=created.shopping_list.version,
            idempotency_key="same-key",
        )


def test_stale_version_is_refused(tmp_path):
    db_path = tmp_path / "stale.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    created = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    updated = store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Хлеб", quantity_value="1", quantity_unit_normalized="package"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="manual-1",
    )

    with pytest.raises(ShoppingConflictError):
        store.add_manual_item(
            context,
            created.shopping_list.id,
            ManualShoppingItemInput(display_name="Молоко", quantity_value="1", quantity_unit_normalized="l"),
            expected_list_version=created.shopping_list.version,
            idempotency_key="manual-2",
        )
    assert updated.shopping_list.version > created.shopping_list.version


def test_source_menu_entry_validation_enforces_scope(tmp_path):
    db_path = tmp_path / "source-entry.db"
    weekly_store, published, context, household_id = _publish_menu_revision(db_path)
    store = HealBiteShoppingStore(db_path=db_path)
    created = store.create_shopping_list(
        context,
        household_id,
        week_start="2026-07-06",
        idempotency_key="list-1",
        source_menu_id=published.revision.id,
    )
    entry_id = published.entries[0].id

    ok = store.replace_or_regenerate_generated_items(
        context,
        created.shopping_list.id,
        [
            GeneratedShoppingItemInput(
                display_name="Томаты",
                quantity_value="1",
                quantity_unit_normalized="kg",
                source_menu_entry_id=entry_id,
            )
        ],
        expected_version=created.shopping_list.version,
        idempotency_key="regen-1",
    )
    assert len(ok.items) == 1

    next_draft = weekly_store.create_draft_revision(
        context,
        published.series.id,
        expected_series_version=published.series.version,
        idempotency_key="menu-draft-2",
    )
    next_ready = weekly_store.replace_draft_entries(
        context,
        next_draft.revision.id,
        [
            WeeklyMenuEntryInput(
                local_date="2026-07-06",
                meal_slot=WeeklyMenuMealSlot.BREAKFAST,
                position=1,
                title="Каша",
            )
        ],
        expected_revision_version=next_draft.revision.version,
        idempotency_key="menu-replace-2",
    )

    with pytest.raises(ShoppingAccessError):
        store.replace_or_regenerate_generated_items(
            context,
            created.shopping_list.id,
            [
                GeneratedShoppingItemInput(
                    display_name="Томаты",
                    quantity_value="1",
                    quantity_unit_normalized="kg",
                    source_menu_entry_id=next_ready.entries[0].id,
                )
            ],
            expected_version=ok.shopping_list.version,
            idempotency_key="regen-2",
        )


def test_audit_reports_safe_corruption_counts(tmp_path):
    db_path = tmp_path / "audit.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    created = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Хлеб", quantity_value="1", quantity_unit_normalized="package"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="manual-1",
    )
    with _connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            f"UPDATE {SHOPPING_ITEMS_TABLE} SET checked_state = 7, quantity_unit_normalized = 'bad-unit', quantity_value = '1,5'"
        )

    audit = store.audit_schema()

    assert audit.item_count == 1
    assert audit.invalid_checked_count == 1
    assert audit.invalid_unit_count == 1
    assert audit.invalid_quantity_count == 1


def test_mutations_do_not_write_to_weekly_menu_or_users_profile_tables(tmp_path):
    db_path = tmp_path / "write-guard.db"
    weekly_store, published, context, household_id = _publish_menu_revision(db_path)
    store = HealBiteShoppingStore(db_path=db_path)
    created = store.create_shopping_list(
        context,
        household_id,
        week_start="2026-07-06",
        idempotency_key="list-1",
        source_menu_id=published.revision.id,
    )
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER deny_weekly_menu_update
            BEFORE UPDATE ON household_weekly_menus
            BEGIN
                SELECT RAISE(ABORT, 'weekly menu writes forbidden');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER deny_weekly_menu_entry_update
            BEFORE UPDATE ON household_weekly_menu_entries
            BEGIN
                SELECT RAISE(ABORT, 'weekly menu writes forbidden');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER deny_user_update
            BEFORE UPDATE ON users
            BEGIN
                SELECT RAISE(ABORT, 'users writes forbidden');
            END
            """
        )
    updated = store.replace_or_regenerate_generated_items(
        context,
        created.shopping_list.id,
        [
            GeneratedShoppingItemInput(display_name="Томаты", quantity_value="1", quantity_unit_normalized="kg")
        ],
        expected_version=created.shopping_list.version,
        idempotency_key="regen-1",
    )

    assert len(updated.items) == 1


def test_concurrent_activate_prevents_two_active_lists(tmp_path):
    db_path = tmp_path / "concurrency.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    first = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    second = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-2",
    )
    barrier = threading.Barrier(2)
    results: list[str] = []

    def _activate(list_id: str, version: int, key: str) -> None:
        local_store = HealBiteShoppingStore(db_path=db_path)
        barrier.wait()
        try:
            local_store.activate_shopping_list(
                context,
                list_id,
                expected_version=version,
                idempotency_key=key,
            )
            results.append("ok")
        except ShoppingConflictError:
            results.append("conflict")

    first_thread = threading.Thread(target=_activate, args=(first.shopping_list.id, first.shopping_list.version, "act-1"))
    second_thread = threading.Thread(target=_activate, args=(second.shopping_list.id, second.shopping_list.version, "act-2"))
    first_thread.start()
    second_thread.start()
    first_thread.join()
    second_thread.join()

    assert sorted(results) == ["conflict", "ok"]
    with _connect(db_path) as conn:
        active_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {SHOPPING_LISTS_TABLE} WHERE status = ?",
                (ShoppingListStatus.ACTIVE.value,),
            ).fetchone()[0]
        )
        assert active_count == 1
        activation_idempotency_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {SHOPPING_IDEMPOTENCY_TABLE} WHERE operation = ?",
                ("activate_list",),
            ).fetchone()[0]
        )
        assert activation_idempotency_count == 1


def test_random_scoped_ids_do_not_leak_existence(tmp_path):
    db_path = tmp_path / "not-found.db"
    _, _, store, personal, context = _seed_personal_household(db_path)
    created = store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    _ = created
    foreign_context = HouseholdContext(
        actor_user_id=999,
        household_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        household_member_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        role=HouseholdRole.OWNER,
        member_status=HouseholdMemberStatus.ACTIVE,
        household_status=HouseholdStatus.ACTIVE,
    )

    with pytest.raises((ShoppingAccessError, ShoppingNotFoundError)):
        store.get_shopping_list(foreign_context, created.shopping_list.id)
