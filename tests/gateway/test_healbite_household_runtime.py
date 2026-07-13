
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gateway.healbite_household_runtime import (
    HouseholdRuntimeBridge,
    HouseholdRuntimeStatus,
    build_household_runtime_bridge,
)
from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, HOUSEHOLDS_TABLE
from gateway.healbite_households import HealBiteHouseholdStore, HouseholdFeatureConfig
from gateway.platforms.telegram import (
    HEALBITE_PLACEHOLDER_REPLY,
    HEALBITE_REPLY_KEYBOARD_ACTIONS,
    HEALBITE_REPLY_KEYBOARD_ROWS,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE users (
                {identity_column} INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _insert_user(db_path: Path, user_id: int, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(f"INSERT INTO users ({identity_column}, username) VALUES (?, ?)", (int(user_id), "synthetic"))


def _tables(db_path: Path) -> set[str]:
    with _connect(db_path) as conn:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _count(db_path: Path, table: str) -> int:
    with _connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _household_counts(db_path: Path) -> tuple[int, int]:
    tables = _tables(db_path)
    if HOUSEHOLDS_TABLE not in tables or HOUSEHOLD_MEMBERS_TABLE not in tables:
        return (0, 0)
    return (_count(db_path, HOUSEHOLDS_TABLE), _count(db_path, HOUSEHOLD_MEMBERS_TABLE))


def _config(*, enabled: bool = True, allowlist: set[int] | None = None, valid: bool = True) -> HouseholdFeatureConfig:
    return HouseholdFeatureConfig(
        enabled=enabled,
        allowlist=frozenset({101} if allowlist is None else allowlist),
        allowlist_valid=valid,
    )


class _CountingStoreFactory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self) -> HealBiteHouseholdStore:
        self.calls += 1
        return HealBiteHouseholdStore(db_path=self.db_path, ensure_schema_on_init=False)


def test_feature_disabled_fast_fails_without_opening_store(tmp_path):
    db_path = tmp_path / "healbite.db"
    factory = _CountingStoreFactory(db_path)
    bridge = HouseholdRuntimeBridge(config=_config(enabled=False), db_path=db_path, store_factory=factory)

    assert bridge.feature_state.enabled is False
    assert bridge.feature_state.allowlist_count == 1
    assert bridge.resolve_existing_context_for_actor(101).status is HouseholdRuntimeStatus.DISABLED
    assert bridge.resolve_or_create_context_for_internal_actor(101).status is HouseholdRuntimeStatus.DISABLED
    assert factory.calls == 0
    assert not db_path.exists()


def test_empty_allowlist_fast_fails_without_opening_store(tmp_path):
    db_path = tmp_path / "healbite.db"
    factory = _CountingStoreFactory(db_path)
    bridge = HouseholdRuntimeBridge(config=_config(allowlist=set()), db_path=db_path, store_factory=factory)

    result = bridge.resolve_existing_context_for_actor(101)

    assert result.status is HouseholdRuntimeStatus.NOT_ALLOWLISTED
    assert factory.calls == 0
    assert not db_path.exists()


def test_non_allowlisted_actor_fast_fails_without_schema_or_store(tmp_path):
    db_path = tmp_path / "healbite.db"
    factory = _CountingStoreFactory(db_path)
    bridge = HouseholdRuntimeBridge(config=_config(allowlist={202}), db_path=db_path, store_factory=factory)

    assert bridge.resolve_existing_context_for_actor(101).status is HouseholdRuntimeStatus.NOT_ALLOWLISTED
    assert bridge.resolve_or_create_context_for_internal_actor(101).status is HouseholdRuntimeStatus.NOT_ALLOWLISTED
    assert factory.calls == 0
    assert not db_path.exists()


@pytest.mark.parametrize("bad_actor", [None, True, 0, -1, "not-an-int", 9223372036854775808])
def test_invalid_actor_fast_fails_without_opening_store(tmp_path, bad_actor):
    db_path = tmp_path / "healbite.db"
    factory = _CountingStoreFactory(db_path)
    bridge = HouseholdRuntimeBridge(config=_config(), db_path=db_path, store_factory=factory)

    assert bridge.resolve_existing_context_for_actor(bad_actor).status is HouseholdRuntimeStatus.INVALID_ACTOR
    assert bridge.resolve_or_create_context_for_internal_actor(bad_actor).status is HouseholdRuntimeStatus.INVALID_ACTOR
    assert factory.calls == 0
    assert not db_path.exists()


def test_invalid_allowlist_config_fast_fails_and_repr_omits_raw_allowlist(tmp_path):
    db_path = tmp_path / "healbite.db"
    factory = _CountingStoreFactory(db_path)
    bridge = HouseholdRuntimeBridge(
        config=HouseholdFeatureConfig(enabled=True, allowlist=frozenset({101}), allowlist_valid=False),
        db_path=db_path,
        store_factory=factory,
    )

    result = bridge.resolve_existing_context_for_actor(101)

    assert result.status is HouseholdRuntimeStatus.INVALID_CONFIG
    assert bridge.feature_state.configuration_valid is False
    rendered = repr(bridge)
    assert "allowlist_count=1" in rendered
    assert "101" not in rendered
    assert factory.calls == 0
    assert not db_path.exists()


def test_allowlisted_existing_resolution_does_not_create_household_schema(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    factory = _CountingStoreFactory(db_path)
    bridge = HouseholdRuntimeBridge(config=_config(), db_path=db_path, store_factory=factory)

    result = bridge.resolve_existing_context_for_actor(101)

    assert result.status is HouseholdRuntimeStatus.SCHEMA_UNAVAILABLE
    assert factory.calls == 1
    assert HOUSEHOLDS_TABLE not in _tables(db_path)
    assert HOUSEHOLD_MEMBERS_TABLE not in _tables(db_path)


def test_store_can_skip_schema_init_for_runtime_bridge(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)

    HealBiteHouseholdStore(db_path=db_path, ensure_schema_on_init=False)

    assert HOUSEHOLDS_TABLE not in _tables(db_path)
    assert HOUSEHOLD_MEMBERS_TABLE not in _tables(db_path)


def test_resolve_existing_actor_context_returns_existing_rows_without_writes(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    created = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    before = _household_counts(db_path)
    factory = _CountingStoreFactory(db_path)
    bridge = HouseholdRuntimeBridge(config=_config(), db_path=db_path, store_factory=factory)

    result = bridge.resolve_existing_context_for_actor(101)

    assert result.status is HouseholdRuntimeStatus.RESOLVED
    assert result.created is False
    assert result.context is not None
    assert result.context.household_id == created.household.id
    assert _household_counts(db_path) == before
    assert factory.calls == 1


def test_resolve_or_create_creates_once_for_allowlisted_internal_actor(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    HealBiteHouseholdStore(db_path=db_path).ensure_schema()
    bridge = HouseholdRuntimeBridge(config=_config(), db_path=db_path)

    first = bridge.resolve_or_create_context_for_internal_actor(101)
    after_first = _household_counts(db_path)
    second = bridge.resolve_or_create_context_for_internal_actor(101)

    assert first.status is HouseholdRuntimeStatus.CREATED
    assert first.created is True
    assert second.status is HouseholdRuntimeStatus.RESOLVED
    assert second.created is False
    assert _household_counts(db_path) == after_first == (1, 1)
    assert first.context is not None and second.context is not None
    assert first.context.household_id == second.context.household_id


def test_existing_only_missing_household_is_not_created(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    HealBiteHouseholdStore(db_path=db_path).ensure_schema()
    bridge = HouseholdRuntimeBridge(config=_config(), db_path=db_path)

    result = bridge.resolve_existing_context_for_actor(101)

    assert result.status is HouseholdRuntimeStatus.HOUSEHOLD_NOT_FOUND
    assert _household_counts(db_path) == (0, 0)


@pytest.mark.parametrize("table,column", [(HOUSEHOLDS_TABLE, "status"), (HOUSEHOLD_MEMBERS_TABLE, "status")])
def test_disabled_existing_household_or_member_denies_access_without_writes(tmp_path, table, column):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    personal = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    before = _household_counts(db_path)
    with _connect(db_path) as conn:
        if table == HOUSEHOLDS_TABLE:
            conn.execute(f"UPDATE {table} SET {column} = 'disabled' WHERE id = ?", (personal.household.id,))
        else:
            conn.execute(f"UPDATE {table} SET {column} = 'disabled' WHERE id = ?", (personal.member.id,))
    bridge = HouseholdRuntimeBridge(config=_config(), db_path=db_path)

    result = bridge.resolve_existing_context_for_actor(101)

    assert result.status is HouseholdRuntimeStatus.ACCESS_DENIED
    assert _household_counts(db_path) == before


def test_actor_not_found_and_integrity_fail_closed_without_household_writes(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    HealBiteHouseholdStore(db_path=db_path).ensure_schema()
    bridge = HouseholdRuntimeBridge(config=_config(), db_path=db_path)

    missing = bridge.resolve_existing_context_for_actor(101)

    assert missing.status is HouseholdRuntimeStatus.ACTOR_NOT_FOUND
    assert _household_counts(db_path) == (0, 0)

    unsupported = tmp_path / "unsupported.db"
    with _connect(unsupported) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
        conn.execute("INSERT INTO users (id, username) VALUES (101, 'synthetic')")
    HealBiteHouseholdStore(db_path=unsupported).ensure_schema()
    bad_bridge = HouseholdRuntimeBridge(config=_config(), db_path=unsupported)

    assert bad_bridge.resolve_existing_context_for_actor(101).status is HouseholdRuntimeStatus.INTEGRITY_ERROR
    assert _household_counts(unsupported) == (0, 0)


def test_builder_uses_environment_feature_config_and_has_no_schema_side_effects(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    env = {
        "HEALBITE_HOUSEHOLDS_ENABLED": "true",
        "HEALBITE_HOUSEHOLDS_ALLOWLIST": "101",
    }

    bridge = build_household_runtime_bridge(env=env, db_path=db_path)

    assert bridge.feature_state.enabled is True
    assert bridge.feature_state.allowlist_count == 1
    assert HOUSEHOLDS_TABLE not in _tables(db_path)
    assert HOUSEHOLD_MEMBERS_TABLE not in _tables(db_path)


def test_gateway_composition_builder_does_not_initialize_household_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    monkeypatch.setenv("HEALBITE_DB_PATH", str(db_path))
    monkeypatch.setenv("HEALBITE_HOUSEHOLDS_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_HOUSEHOLDS_ALLOWLIST", "101")

    from gateway.run import _build_healbite_household_runtime_bridge

    bridge = _build_healbite_household_runtime_bridge()

    assert bridge.feature_state.enabled is True
    assert bridge.feature_state.allowlist_count == 1
    assert HOUSEHOLDS_TABLE not in _tables(db_path)
    assert HOUSEHOLD_MEMBERS_TABLE not in _tables(db_path)


def test_existing_telegram_keyboard_contract_routes_family_through_its_feature_gate():
    profile = "\U0001f464 \u041c\u043e\u0439 \u043f\u0440\u043e\u0444\u0438\u043b\u044c"
    diary = "\U0001f34e \u0414\u043d\u0435\u0432\u043d\u0438\u043a \u0435\u0434\u044b"
    weekly_menu = "\U0001f4cb \u041c\u0435\u043d\u044e \u043d\u0430 \u043d\u0435\u0434\u0435\u043b\u044e"
    shopping = "\U0001f6d2 \u0421\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u043a\u0443\u043f\u043e\u043a"
    weight = "\u2696\ufe0f \u0422\u0440\u0435\u043a\u0435\u0440 \u0432\u0435\u0441\u0430"
    water = "\U0001f4a7 \u0422\u0440\u0435\u043a\u0435\u0440 \u0432\u043e\u0434\u044b"
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467 \u0421\u0435\u043c\u044c\u044f"
    weekly_report = "\U0001f4c8 \u041e\u0442\u0447\u0435\u0442 \u0437\u0430 \u043d\u0435\u0434\u0435\u043b\u044e"
    restrictions = "\u2699\ufe0f \u041e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u044f"
    help_button = "\u2753 \u041f\u043e\u043c\u043e\u0449\u044c"

    assert HEALBITE_REPLY_KEYBOARD_ROWS == [
        [profile, diary],
        [weekly_menu, shopping],
        [weight, water],
        [family, weekly_report],
        [restrictions, help_button],
    ]
    assert HEALBITE_REPLY_KEYBOARD_ACTIONS[weekly_menu] == "/weekly_menu"
    assert HEALBITE_REPLY_KEYBOARD_ACTIONS[shopping] == "/shopping"
    assert HEALBITE_REPLY_KEYBOARD_ACTIONS[family] == "/family"
    assert HEALBITE_PLACEHOLDER_REPLY == "\u0412 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u043a\u0435"
