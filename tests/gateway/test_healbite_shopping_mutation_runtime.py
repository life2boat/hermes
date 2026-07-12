from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, new_household_member_id
from gateway.healbite_households import (
    HealBiteHouseholdStore,
    HouseholdContext,
    HouseholdMemberStatus,
    HouseholdRole,
    HouseholdStatus,
)
from gateway.healbite_runtime_resources import borrowed_runtime_resource
from gateway.healbite_shopping import (
    GeneratedShoppingItemInput,
    HealBiteShoppingStore,
    ManualShoppingItemInput,
    ShoppingConflictError,
    ShoppingStateError,
)
from gateway.healbite_shopping_runtime import (
    HealBiteShoppingRuntimeService,
    ShoppingRuntimeNotFoundError,
    ShoppingRuntimeStateError,
)
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore


WEEK = "2026-07-06"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_user(db_path: Path, user_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, f"user-{user_id}"),
        )


def _seed_active_list(db_path: Path, actor_user_id: int = 101):
    _insert_user(db_path, actor_user_id)
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(actor_user_id)
    context = households.resolve_actor_context(actor_user_id)
    HealBiteWeeklyMenuStore(db_path=db_path).initialize_schema()
    store = HealBiteShoppingStore(db_path=db_path)
    store.initialize_schema()
    draft = store.create_shopping_list(
        context,
        personal.household.id,
        week_start=WEEK,
        idempotency_key="create-list",
    )
    active = store.activate_shopping_list(
        context,
        draft.shopping_list.id,
        expected_version=draft.shopping_list.version,
        idempotency_key="activate-list",
    )
    return personal, context, store, active


def _runtime(db_path: Path, *actor_ids: int) -> HealBiteShoppingRuntimeService:
    return HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(
            enabled=True,
            allowlist=frozenset(actor_ids),
            configuration_valid=True,
        ),
        db_path=db_path,
    )


def _add_member(
    db_path: Path,
    household_id: str,
    user_id: int,
    role: HouseholdRole,
) -> HouseholdContext:
    _insert_user(db_path, user_id)
    member_id = new_household_member_id()
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, display_name, member_type, role,
                 status, age_band, created_at, updated_at, version)
            VALUES (?, ?, ?, ?, 'linked_adult', ?, 'active', NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)
            """,
            (member_id, household_id, user_id, f"user-{user_id}", role.value),
        )
    return HouseholdContext(
        actor_user_id=user_id,
        household_id=household_id,
        household_member_id=member_id,
        role=role,
        member_status=HouseholdMemberStatus.ACTIVE,
        household_status=HouseholdStatus.ACTIVE,
    )


def _add(runtime: HealBiteShoppingRuntimeService, version: int, key: str = "add"):
    return runtime.add_manual_shopping_item(
        101,
        WEEK,
        "Milk",
        "1",
        "l",
        key,
        version,
    )


def test_public_runtime_add_toggle_delete_and_terminal_replay(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, _context, _store, active = _seed_active_list(db_path)
    runtime = _runtime(db_path, 101)

    current = runtime.get_current_shopping_list(101, WEEK)
    assert current is not None
    added = _add(runtime, active.shopping_list.version)
    assert len(added.items) == 1
    item = added.items[0]
    assert item.origin.value == "manual"

    replay = _add(runtime, active.shopping_list.version)
    assert replay == added

    checked = runtime.set_shopping_item_checked(101, item.id, True, "check", item.version)
    assert checked.items[0].checked_state is True
    assert checked.items[0].version == item.version + 1
    assert checked.shopping_list.version == added.shopping_list.version + 1

    checked_replay = runtime.set_shopping_item_checked(101, item.id, True, "check", item.version)
    assert checked_replay == checked

    deleted = runtime.delete_shopping_item(
        101,
        item.id,
        "delete",
        checked.items[0].version,
    )
    assert deleted.items == ()
    deleted_replay = runtime.delete_shopping_item(
        101,
        item.id,
        "delete",
        checked.items[0].version,
    )
    assert deleted_replay == deleted


def test_add_validation_payload_conflict_and_stale_version(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, _context, _store, active = _seed_active_list(db_path)
    runtime = _runtime(db_path, 101)

    with pytest.raises(ShoppingRuntimeStateError):
        runtime.add_manual_shopping_item(101, WEEK, " ", "1", "l", "bad", active.shopping_list.version)
    with pytest.raises(ShoppingRuntimeStateError):
        runtime.add_manual_shopping_item(101, WEEK, "Milk", "-1", "l", "bad-q", active.shopping_list.version)
    with pytest.raises(ShoppingRuntimeStateError):
        runtime.add_manual_shopping_item(101, WEEK, "Milk", "1", "bogus", "bad-u", active.shopping_list.version)

    added = _add(runtime, active.shopping_list.version, "same")
    with pytest.raises(ShoppingRuntimeStateError):
        runtime.add_manual_shopping_item(
            101, WEEK, "Bread", "1", "piece", "same", active.shopping_list.version
        )
    with pytest.raises(ShoppingRuntimeStateError):
        _add(runtime, active.shopping_list.version, "stale")
    assert len(added.items) == 1


def test_identical_manual_items_remain_distinct(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, _context, _store, active = _seed_active_list(db_path)
    runtime = _runtime(db_path, 101)
    first = _add(runtime, active.shopping_list.version, "first")
    second = _add(runtime, first.shopping_list.version, "second")
    assert len(second.items) == 2
    assert second.items[0].id != second.items[1].id


def test_toggle_uses_item_version_and_preserves_other_fields(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, _context, _store, active = _seed_active_list(db_path)
    runtime = _runtime(db_path, 101)
    added = _add(runtime, active.shopping_list.version)
    before = added.items[0]

    checked = runtime.set_shopping_item_checked(101, before.id, True, "on", before.version)
    after = checked.items[0]
    assert (
        after.display_name,
        after.quantity_value,
        after.quantity_unit_normalized,
        after.origin,
    ) == (
        before.display_name,
        before.quantity_value,
        before.quantity_unit_normalized,
        before.origin,
    )
    with pytest.raises(ShoppingRuntimeStateError):
        runtime.set_shopping_item_checked(101, before.id, False, "stale", before.version)

    unchecked = runtime.set_shopping_item_checked(101, after.id, False, "off", after.version)
    assert unchecked.items[0].checked_state is False


def test_clear_all_items_is_idempotent_and_keeps_list(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, _context, _store, active = _seed_active_list(db_path)
    runtime = _runtime(db_path, 101)
    first = _add(runtime, active.shopping_list.version, "a")
    second = runtime.add_manual_shopping_item(
        101, WEEK, "Bread", "1", "piece", "b", first.shopping_list.version
    )

    cleared = runtime.clear_shopping_list(
        101, WEEK, "all_items", "clear", second.shopping_list.version
    )
    assert cleared.items == ()
    assert cleared.shopping_list.id == second.shopping_list.id
    assert cleared.shopping_list.version == second.shopping_list.version + 1
    replay = runtime.clear_shopping_list(
        101, WEEK, "all_items", "clear", second.shopping_list.version
    )
    assert replay == cleared

    empty_clear = runtime.clear_shopping_list(
        101, WEEK, "all_items", "clear-empty", cleared.shopping_list.version
    )
    assert empty_clear.shopping_list.version == cleared.shopping_list.version
    with pytest.raises(ShoppingRuntimeStateError):
        runtime.clear_shopping_list(
            101, WEEK, "checked_items_only", "unsupported", cleared.shopping_list.version
        )


def test_delete_generated_item_without_regeneration(tmp_path):
    db_path = tmp_path / "shopping.db"
    personal, context, store, active = _seed_active_list(db_path)
    generated = store.replace_or_regenerate_generated_items(
        context,
        active.shopping_list.id,
        [
            # A standalone list permits generated items without source lineage.
            GeneratedShoppingItemInput(
                display_name="Rice",
                quantity_value="1",
                quantity_unit_normalized="kg",
            )
        ],
        expected_version=active.shopping_list.version,
        idempotency_key="generated",
    )
    runtime = _runtime(db_path, 101)

    result = runtime.delete_shopping_item(
        101,
        generated.items[0].id,
        "delete-generated",
        generated.items[0].version,
    )
    assert result.items == ()
    assert result.shopping_list.household_id == personal.household.id


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (HouseholdRole.ADULT_ADMIN, True),
        (HouseholdRole.ADULT_MEMBER, True),
        (HouseholdRole.DEPENDENT, False),
    ],
)
def test_confirmed_role_policy_is_preserved(tmp_path, role, allowed):
    db_path = tmp_path / f"{role.value}.db"
    personal, _context, _store, active = _seed_active_list(db_path)
    _add_member(db_path, personal.household.id, 202, role)
    runtime = _runtime(db_path, 101, 202)

    if allowed:
        result = runtime.add_manual_shopping_item(
            202, WEEK, "Milk", "1", "l", f"add-{role.value}", active.shopping_list.version
        )
        assert len(result.items) == 1
    else:
        with pytest.raises(ShoppingRuntimeNotFoundError):
            runtime.add_manual_shopping_item(
                202, WEEK, "Milk", "1", "l", "denied", active.shopping_list.version
            )


def test_foreign_and_random_item_references_are_equivalent(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, _context, _store, active = _seed_active_list(db_path)
    runtime = _runtime(db_path, 101, 202)
    added = _add(runtime, active.shopping_list.version)
    _seed_active_list(db_path, 202)

    errors = []
    for item_id in (
        added.items[0].id,
        "33333333-3333-4333-8333-333333333333",
    ):
        with pytest.raises(ShoppingRuntimeNotFoundError) as excinfo:
            runtime.delete_shopping_item(202, item_id, f"delete-{len(errors)}", 1)
        errors.append((type(excinfo.value), str(excinfo.value)))
    assert errors[0] == errors[1]


def test_delete_rollback_is_atomic_when_idempotency_write_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "shopping.db"
    _personal, context, store, active = _seed_active_list(db_path)
    added = store.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput(display_name="Milk", quantity_value="1", quantity_unit_normalized="l"),
        expected_list_version=active.shopping_list.version,
        idempotency_key="add",
    )
    item = added.items[0]
    original = store._store_idempotency

    def fail_terminal(*args, **kwargs):
        if kwargs["shopping_item_id"] is None:
            raise ShoppingStateError("fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_store_idempotency", fail_terminal)
    with pytest.raises(ShoppingStateError, match="fault"):
        store.delete_item(context, item.id, expected_item_version=item.version, idempotency_key="delete")

    persisted = store.get_shopping_list(context, added.shopping_list.id)
    assert [value.id for value in persisted.items] == [item.id]
    assert persisted.shopping_list.version == added.shopping_list.version


def test_clear_rollback_is_atomic_when_idempotency_write_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "shopping.db"
    _personal, context, store, active = _seed_active_list(db_path)
    added = store.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput(display_name="Milk", quantity_value="1", quantity_unit_normalized="l"),
        expected_list_version=active.shopping_list.version,
        idempotency_key="add",
    )

    def fail_terminal(*args, **kwargs):
        raise ShoppingStateError("fault")

    monkeypatch.setattr(store, "_store_idempotency", fail_terminal)
    with pytest.raises(ShoppingStateError, match="fault"):
        store.clear_shopping_list(
            context,
            added.shopping_list.id,
            clear_mode="all_items",
            expected_list_version=added.shopping_list.version,
            idempotency_key="clear",
        )

    persisted = HealBiteShoppingStore(db_path=db_path).get_shopping_list(context, added.shopping_list.id)
    assert len(persisted.items) == 1
    assert persisted.shopping_list.version == added.shopping_list.version


def test_concurrent_same_key_add_has_one_effective_mutation(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, context, store, active = _seed_active_list(db_path)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        local = HealBiteShoppingStore(db_path=db_path)
        barrier.wait()
        try:
            results.append(
                local.add_manual_item(
                    context,
                    active.shopping_list.id,
                    ManualShoppingItemInput(display_name="Milk", quantity_value="1", quantity_unit_normalized="l"),
                    expected_list_version=active.shopping_list.version,
                    idempotency_key="same-key",
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    persisted = store.get_shopping_list(context, active.shopping_list.id)
    assert len(persisted.items) == 1


def test_concurrent_different_key_add_has_one_controlled_conflict(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, context, store, active = _seed_active_list(db_path)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker(key: str):
        local = HealBiteShoppingStore(db_path=db_path)
        barrier.wait()
        try:
            results.append(
                local.add_manual_item(
                    context,
                    active.shopping_list.id,
                    ManualShoppingItemInput(display_name=key, quantity_value="1", quantity_unit_normalized="piece"),
                    expected_list_version=active.shopping_list.version,
                    idempotency_key=key,
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(key,)) for key in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ShoppingConflictError)
    assert len(store.get_shopping_list(context, active.shopping_list.id).items) == 1


def test_other_household_and_week_remain_unchanged_by_clear(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, context, store, active = _seed_active_list(db_path)
    current = store.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput(display_name="Milk", quantity_value="1", quantity_unit_normalized="l"),
        expected_list_version=active.shopping_list.version,
        idempotency_key="current",
    )
    other_draft = store.create_shopping_list(
        context,
        context.household_id,
        week_start="2026-07-13",
        idempotency_key="other-list",
    )
    other = store.add_manual_item(
        context,
        other_draft.shopping_list.id,
        ManualShoppingItemInput(display_name="Bread", quantity_value="1", quantity_unit_normalized="piece"),
        expected_list_version=other_draft.shopping_list.version,
        idempotency_key="other-item",
    )

    store.clear_shopping_list(
        context,
        current.shopping_list.id,
        clear_mode="all_items",
        expected_list_version=current.shopping_list.version,
        idempotency_key="clear",
    )

    preserved = store.get_shopping_list(context, other.shopping_list.id)
    assert len(preserved.items) == 1
    assert preserved.items[0].display_name == "Bread"


def test_toggle_delete_race_has_one_consistent_terminal_outcome(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, context, store, active = _seed_active_list(db_path)
    added = store.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput(display_name="Milk", quantity_value="1", quantity_unit_normalized="l"),
        expected_list_version=active.shopping_list.version,
        idempotency_key="add",
    )
    item = added.items[0]
    barrier = threading.Barrier(2)
    outcomes = []

    def toggle():
        barrier.wait()
        try:
            outcomes.append(
                ("toggle", HealBiteShoppingStore(db_path=db_path).set_item_checked(
                    context,
                    item.id,
                    True,
                    expected_item_version=item.version,
                    idempotency_key="toggle",
                ))
            )
        except Exception as exc:
            outcomes.append(("toggle_error", exc))

    def delete():
        barrier.wait()
        try:
            outcomes.append(
                ("delete", HealBiteShoppingStore(db_path=db_path).delete_item(
                    context,
                    item.id,
                    expected_item_version=item.version,
                    idempotency_key="delete",
                ))
            )
        except Exception as exc:
            outcomes.append(("delete_error", exc))

    threads = [threading.Thread(target=toggle), threading.Thread(target=delete)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    persisted = store.get_shopping_list(context, added.shopping_list.id)
    assert len([kind for kind, _ in outcomes if not kind.endswith("_error")]) == 1
    if persisted.items:
        assert persisted.items[0].checked_state is True
        assert isinstance(next(value for kind, value in outcomes if kind == "delete_error"), ShoppingConflictError)
    else:
        assert isinstance(next(value for kind, value in outcomes if kind == "toggle_error"), Exception)


def test_clear_add_race_serializes_without_partial_state(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, context, store, active = _seed_active_list(db_path)
    seeded = store.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput(display_name="Seed", quantity_value="1", quantity_unit_normalized="piece"),
        expected_list_version=active.shopping_list.version,
        idempotency_key="seed",
    )
    barrier = threading.Barrier(2)
    successes = []
    errors = []

    def add():
        barrier.wait()
        try:
            successes.append(
                HealBiteShoppingStore(db_path=db_path).add_manual_item(
                    context,
                    seeded.shopping_list.id,
                    ManualShoppingItemInput(display_name="New", quantity_value="1", quantity_unit_normalized="piece"),
                    expected_list_version=seeded.shopping_list.version,
                    idempotency_key="add-race",
                )
            )
        except Exception as exc:
            errors.append(exc)

    def clear():
        barrier.wait()
        try:
            successes.append(
                HealBiteShoppingStore(db_path=db_path).clear_shopping_list(
                    context,
                    seeded.shopping_list.id,
                    clear_mode="all_items",
                    expected_list_version=seeded.shopping_list.version,
                    idempotency_key="clear-race",
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add), threading.Thread(target=clear)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ShoppingConflictError)
    persisted = store.get_shopping_list(context, seeded.shopping_list.id)
    assert len(persisted.items) in (0, 2)
    assert persisted.shopping_list.version == seeded.shopping_list.version + 1


def test_clear_toggle_race_serializes_without_item_resurrection(tmp_path):
    db_path = tmp_path / "shopping.db"
    _personal, context, store, active = _seed_active_list(db_path)
    added = store.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput(display_name="Milk", quantity_value="1", quantity_unit_normalized="l"),
        expected_list_version=active.shopping_list.version,
        idempotency_key="add",
    )
    item = added.items[0]
    barrier = threading.Barrier(2)
    successes = []
    errors = []

    def toggle():
        barrier.wait()
        try:
            successes.append(
                HealBiteShoppingStore(db_path=db_path).set_item_checked(
                    context,
                    item.id,
                    True,
                    expected_item_version=item.version,
                    idempotency_key="toggle-race",
                )
            )
        except Exception as exc:
            errors.append(exc)

    def clear():
        barrier.wait()
        try:
            successes.append(
                HealBiteShoppingStore(db_path=db_path).clear_shopping_list(
                    context,
                    added.shopping_list.id,
                    clear_mode="all_items",
                    expected_list_version=added.shopping_list.version,
                    idempotency_key="clear-race",
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=toggle), threading.Thread(target=clear)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(errors) == 1
    persisted = store.get_shopping_list(context, added.shopping_list.id)
    assert persisted.items == () or persisted.items[0].checked_state is True



class _BeforeShoppingStoreFactory:
    def __init__(self, db_path: Path, callback, store_type=HealBiteShoppingStore) -> None:
        self.db_path = db_path
        self.callback = callback
        self.store_type = store_type
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.callback()
        return borrowed_runtime_resource(self.store_type(db_path=self.db_path))


def _shopping_state(db_path: Path, shopping_list_id: str) -> tuple[int, int, int, int]:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT l.version,
                   COUNT(i.id) AS item_count,
                   COALESCE(SUM(i.checked_state), 0) AS checked_count,
                   (SELECT COUNT(*) FROM household_shopping_idempotency) AS idempotency_count
            FROM household_shopping_lists l
            LEFT JOIN household_shopping_items i ON i.shopping_list_id = l.id
            WHERE l.id = ?
            GROUP BY l.id
            """,
            (shopping_list_id,),
        ).fetchone()
    assert row is not None
    return tuple(int(value) for value in row)


def _invoke_runtime_mutation(
    runtime: HealBiteShoppingRuntimeService,
    operation: str,
    *,
    list_version: int,
    item_id: str,
    item_version: int,
    key: str,
):
    if operation == "add":
        return runtime.add_manual_shopping_item(
            101, WEEK, "New", "1", "piece", key, list_version
        )
    if operation == "toggle":
        return runtime.set_shopping_item_checked(101, item_id, True, key, item_version)
    if operation == "delete":
        return runtime.delete_shopping_item(101, item_id, key, item_version)
    if operation == "clear":
        return runtime.clear_shopping_list(
            101, WEEK, "all_items", key, list_version
        )
    raise AssertionError(f"unsupported synthetic operation: {operation}")


@pytest.mark.parametrize("operation", ["add", "toggle", "delete", "clear"])
@pytest.mark.parametrize("revocation_target", ["membership", "household"])
def test_runtime_mutations_revalidate_authorization_after_begin(
    tmp_path,
    operation,
    revocation_target,
):
    db_path = tmp_path / f"{operation}-{revocation_target}.db"
    personal, context, store, active = _seed_active_list(db_path)
    seeded = store.add_manual_item(
        context,
        active.shopping_list.id,
        ManualShoppingItemInput(
            display_name="Seed",
            quantity_value="1",
            quantity_unit_normalized="piece",
        ),
        expected_list_version=active.shopping_list.version,
        idempotency_key="seed",
    )
    item = seeded.items[0]
    before = _shopping_state(db_path, seeded.shopping_list.id)

    def revoke():
        with _connect(db_path) as conn:
            if revocation_target == "membership":
                conn.execute(
                    "UPDATE household_members SET status='disabled' WHERE id=?",
                    (context.household_member_id,),
                )
            else:
                conn.execute(
                    "UPDATE households SET status='disabled' WHERE id=?",
                    (personal.household.id,),
                )

    factory = _BeforeShoppingStoreFactory(db_path, revoke)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(
            enabled=True,
            allowlist=frozenset({101}),
            configuration_valid=True,
        ),
        db_path=db_path,
        shopping_store_factory=factory,
    )

    with pytest.raises(ShoppingRuntimeNotFoundError):
        _invoke_runtime_mutation(
            runtime,
            operation,
            list_version=seeded.shopping_list.version,
            item_id=item.id,
            item_version=item.version,
            key=f"revoked-{operation}",
        )

    assert factory.calls == 1
    assert _shopping_state(db_path, seeded.shopping_list.id) == before


def test_revoked_actor_cannot_replay_successful_idempotency_result(tmp_path):
    db_path = tmp_path / "replay.db"
    _personal, context, store, active = _seed_active_list(db_path)
    runtime = _runtime(db_path, 101)
    added = _add(runtime, active.shopping_list.version, "successful")
    before = _shopping_state(db_path, active.shopping_list.id)

    def revoke():
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE household_members SET status='disabled' WHERE id=?",
                (context.household_member_id,),
            )

    revoked_runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(
            enabled=True,
            allowlist=frozenset({101}),
            configuration_valid=True,
        ),
        db_path=db_path,
        shopping_store_factory=_BeforeShoppingStoreFactory(db_path, revoke),
    )
    with pytest.raises(ShoppingRuntimeNotFoundError):
        _add(revoked_runtime, active.shopping_list.version, "successful")
    assert _shopping_state(db_path, active.shopping_list.id) == before
    assert len(added.items) == 1


def test_runtime_household_mismatch_is_denied_before_mutation(tmp_path):
    db_path = tmp_path / "mismatch.db"
    personal, _context, store, active = _seed_active_list(db_path)
    member_context = _add_member(
        db_path,
        personal.household.id,
        303,
        HouseholdRole.ADULT_MEMBER,
    )
    _insert_user(db_path, 202)
    other_household = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(202)
    before = _shopping_state(db_path, active.shopping_list.id)

    def move_actor():
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE household_members SET household_id=? WHERE id=?",
                (other_household.household.id, member_context.household_member_id),
            )

    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(
            enabled=True,
            allowlist=frozenset({303}),
            configuration_valid=True,
        ),
        db_path=db_path,
        shopping_store_factory=_BeforeShoppingStoreFactory(db_path, move_actor),
    )
    with pytest.raises(ShoppingRuntimeNotFoundError):
        runtime.add_manual_shopping_item(
            303,
            WEEK,
            "Foreign",
            "1",
            "piece",
            "mismatch",
            active.shopping_list.version,
        )
    assert _shopping_state(db_path, active.shopping_list.id) == before
    assert store.get_shopping_list(
        HouseholdContext(
            actor_user_id=101,
            household_id=personal.household.id,
            household_member_id=personal.member.id,
            role=HouseholdRole.OWNER,
            member_status=HouseholdMemberStatus.ACTIVE,
            household_status=HouseholdStatus.ACTIVE,
        ),
        active.shopping_list.id,
    ).items == ()


def test_mutation_first_serializes_before_membership_revocation(tmp_path):
    db_path = tmp_path / "mutation-first.db"
    _personal, context, _store, active = _seed_active_list(db_path)
    authorized = threading.Event()
    revocation_started = threading.Event()
    results = []
    errors = []

    class PausingShoppingStore(HealBiteShoppingStore):
        def _revalidate_shopping_actor_for_write(self, conn, runtime_context, *, operation):
            auth = super()._revalidate_shopping_actor_for_write(
                conn,
                runtime_context,
                operation=operation,
            )
            authorized.set()
            assert revocation_started.wait(timeout=5)
            time.sleep(0.1)
            return auth

    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(
            enabled=True,
            allowlist=frozenset({101}),
            configuration_valid=True,
        ),
        db_path=db_path,
        shopping_store_factory=lambda: borrowed_runtime_resource(
            PausingShoppingStore(db_path=db_path)
        ),
    )

    def mutate():
        try:
            results.append(_add(runtime, active.shopping_list.version, "mutation-first"))
        except Exception as exc:
            errors.append(exc)

    def revoke():
        assert authorized.wait(timeout=5)
        revocation_started.set()
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE household_members SET status='disabled' WHERE id=?",
                (context.household_member_id,),
            )

    mutation_thread = threading.Thread(target=mutate)
    revocation_thread = threading.Thread(target=revoke)
    mutation_thread.start()
    revocation_thread.start()
    mutation_thread.join(timeout=10)
    revocation_thread.join(timeout=10)

    assert not mutation_thread.is_alive()
    assert not revocation_thread.is_alive()
    assert errors == []
    assert len(results) == 1
    with _connect(db_path) as conn:
        member_status = conn.execute(
            "SELECT status FROM household_members WHERE id=?",
            (context.household_member_id,),
        ).fetchone()[0]
    assert member_status == "disabled"
    assert _shopping_state(db_path, active.shopping_list.id)[1] == 1



def test_authorization_query_failure_rolls_back_without_partial_state(tmp_path, monkeypatch):
    db_path = tmp_path / "authorization-query-failure.db"
    _personal, context, store, active = _seed_active_list(db_path)
    before = _shopping_state(db_path, active.shopping_list.id)

    def fail_query(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic authorization query failure")

    monkeypatch.setattr(
        store._household_store,
        "_resolve_existing_actor_context_on_connection",
        fail_query,
    )
    with pytest.raises(sqlite3.OperationalError, match="synthetic authorization query failure"):
        store.add_manual_item(
            context,
            active.shopping_list.id,
            ManualShoppingItemInput(
                display_name="Blocked",
                quantity_value="1",
                quantity_unit_normalized="piece",
            ),
            expected_list_version=active.shopping_list.version,
            idempotency_key="query-failure",
        )

    assert _shopping_state(db_path, active.shopping_list.id) == before
