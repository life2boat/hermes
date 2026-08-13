from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from gateway.memory.analytics import MemoryAnalyticsLogger
from gateway.memory.schema import (
    FACTS_CREATE_SQL,
    FACTS_TABLE,
    MEMORY_CONVERGENCE_MIGRATION_SHA256,
    META_TABLE,
    OUTBOX_TABLE,
    MemorySchemaClassification,
    classify_memory_convergence_schema,
    migrate_memory_convergence_schema,
    validate_memory_convergence_schema,
    validate_memory_convergence_staged_transition,
)
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge
from scripts import healbite_schema_migrate


LEGACY_FACTS_SQL = f"""
CREATE TABLE {FACTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    entity TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    trust_score REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _production_migration_module():
    pytest.importorskip("pwd", reason="production authority wrapper is Linux-only")
    from scripts import hermes_production_staged_migrate

    return hermes_production_staged_migrate


def _legacy_facts(
    conn: sqlite3.Connection,
    rows: list[tuple[int, str]] | None = None,
) -> None:
    conn.execute(LEGACY_FACTS_SQL)
    conn.executemany(
        f"INSERT INTO {FACTS_TABLE}(user_id, entity, key, value) "
        "VALUES (?, 'profile', 'goal', ?)",
        rows or [],
    )


def _migrate_memory(conn: sqlite3.Connection) -> tuple[object, ...]:
    if conn.in_transaction:
        conn.commit()
    phases, _changed = healbite_schema_migrate._migrate_borrowed_connection(
        conn,
        selected=("memory_convergence",),
    )
    return phases


def _assert_database_health(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _outbox_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        f"SELECT user_id, fact_id, operation, fact_revision, state "
        f"FROM {OUTBOX_TABLE} ORDER BY user_id, fact_id"
    ).fetchall()


def _schema_dump(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(conn.iterdump())


def test_t01_t03_registry_identity_order_and_digest_are_deterministic() -> None:
    first = healbite_schema_migrate.migration_registry_manifest()
    second = healbite_schema_migrate.migration_registry_manifest()
    names = [item["component"] for item in first]

    assert names == [
        "household",
        "weekly",
        "shopping",
        "inventory",
        "fridge_menu",
        "memory_convergence",
    ]
    assert names.count("memory_convergence") == 1
    assert first == second
    assert first[-1]["migration_sha256"] == MEMORY_CONVERGENCE_MIGRATION_SHA256
    canonical = json.dumps(first, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "87c7ccbc5fb5a4afa0437daa2375ae93958a8295c4164d6f0f6b196acdf45e0f"
    )


def test_m01_t04_fresh_empty_database_is_absent_then_current() -> None:
    with sqlite3.connect(":memory:") as conn:
        assert classify_memory_convergence_schema(conn) is MemorySchemaClassification.ABSENT
        phases = _migrate_memory(conn)
        assert phases[0].changed is True
        assert classify_memory_convergence_schema(conn) is MemorySchemaClassification.CURRENT
        assert conn.execute(f"SELECT COUNT(*) FROM {FACTS_TABLE}").fetchone()[0] == 0
        assert _outbox_rows(conn) == []
        _assert_database_health(conn)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param([], 0, id="M02-zero-facts"),
        pytest.param([(101, "synthetic-one")], 1, id="M03-one-fact"),
        pytest.param(
            [(101, "synthetic-a"), (101, "synthetic-b"), (101, "synthetic-c")],
            3,
            id="M04-representative-facts",
        ),
        pytest.param(
            [(101, "synthetic-owner-a"), (202, "synthetic-owner-b")],
            2,
            id="M05-multiple-owners",
        ),
    ],
)
def test_m02_m05_t08_t12_legacy_seed_is_exact_and_private(
    rows: list[tuple[int, str]], expected: int
) -> None:
    with sqlite3.connect(":memory:") as conn:
        _legacy_facts(conn, rows)
        conn.execute("CREATE TABLE unrelated_rows(value TEXT NOT NULL)")
        conn.execute("INSERT INTO unrelated_rows VALUES ('preserved')")
        _migrate_memory(conn)

        intents = _outbox_rows(conn)
        assert len(intents) == expected
        assert all(row[2:] == ("UPSERT", 1, "PENDING") for row in intents)
        assert {row[0] for row in intents} == {row[0] for row in rows}
        assert conn.execute("SELECT value FROM unrelated_rows").fetchone()[0] == "preserved"
        outbox_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({OUTBOX_TABLE})")
        }
        assert not {"value", "fact_text", "embedding", "prompt", "provider_response"} & outbox_columns
        _assert_database_health(conn)


def test_m06_representative_revisions_seed_canonical_revision() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(FACTS_CREATE_SQL)
        conn.executemany(
            f"INSERT INTO {FACTS_TABLE}(user_id, entity, key, value, vector_revision) "
            "VALUES (?, 'profile', 'goal', 'synthetic', ?)",
            [(101, 1), (202, 4), (303, 9)],
        )
        assert classify_memory_convergence_schema(conn) is MemorySchemaClassification.KNOWN_COMPATIBLE_PARTIAL
        _migrate_memory(conn)
        assert [(row[0], row[3]) for row in _outbox_rows(conn)] == [
            (101, 1),
            (202, 4),
            (303, 9),
        ]


def test_staged_transition_accepts_only_exact_legacy_seed() -> None:
    with sqlite3.connect(":memory:") as before, sqlite3.connect(":memory:") as after:
        _legacy_facts(before, [(101, "synthetic-a"), (202, "synthetic-b")])
        before.commit()
        before.backup(after)
        migrate_memory_convergence_schema(after, now=0.0)
        validate_memory_convergence_staged_transition(
            before,
            after,
            seed_timestamp=0.0,
        )


def test_staged_transition_rejects_noncanonical_outbox_backfill() -> None:
    with sqlite3.connect(":memory:") as before, sqlite3.connect(":memory:") as after:
        _legacy_facts(before, [(101, "synthetic")])
        before.commit()
        before.backup(after)
        migrate_memory_convergence_schema(after, now=0.0)
        after.execute(
            f"INSERT INTO {OUTBOX_TABLE}(user_id, fact_id, operation, fact_revision, "
            "state, next_attempt_at, created_at, updated_at) "
            "VALUES (202, 999, 'DELETE', 1, 'PENDING', 0, 0, 0)"
        )
        with pytest.raises(sqlite3.DatabaseError, match="seed count is invalid"):
            validate_memory_convergence_staged_transition(
                before,
                after,
                seed_timestamp=0.0,
            )


def test_m07_m09_t05_known_partial_and_missing_index_are_additive() -> None:
    with sqlite3.connect(":memory:") as conn:
        migrate_memory_convergence_schema(conn, now=0)
        conn.execute(f"DROP INDEX idx_{OUTBOX_TABLE}_ready")
        assert classify_memory_convergence_schema(conn) is MemorySchemaClassification.KNOWN_COMPATIBLE_PARTIAL
        _migrate_memory(conn)
        assert classify_memory_convergence_schema(conn) is MemorySchemaClassification.CURRENT


@pytest.mark.parametrize("kind", ["facts", "outbox", "meta"])
def test_m08_m10_m11_t07_malformed_schema_fails_closed(kind: str) -> None:
    with sqlite3.connect(":memory:") as conn:
        if kind == "facts":
            conn.execute(f"CREATE TABLE {FACTS_TABLE}(id TEXT PRIMARY KEY, user_id TEXT)")
        else:
            conn.execute(LEGACY_FACTS_SQL)
            conn.execute(
                f"CREATE TABLE {OUTBOX_TABLE if kind == 'outbox' else META_TABLE} "
                "(singleton_id INTEGER PRIMARY KEY)"
            )
        assert classify_memory_convergence_schema(conn) is MemorySchemaClassification.INCOMPATIBLE
        before = _schema_dump(conn)
        with pytest.raises((healbite_schema_migrate.MigrationError, sqlite3.DatabaseError)):
            _migrate_memory(conn)
        assert _schema_dump(conn) == before


def test_m12_m13_t06_t15_t16_current_rerun_has_zero_mutation() -> None:
    with sqlite3.connect(":memory:") as conn:
        _legacy_facts(conn, [(101, "synthetic")])
        first = _migrate_memory(conn)
        before = _schema_dump(conn)
        second = _migrate_memory(conn)
        assert first[0].changed is True
        assert second[0].changed is False
        assert _schema_dump(conn) == before
        assert len(_outbox_rows(conn)) == 1


def test_m14_m17_t19_t21_interruption_rolls_back_and_restart_converges() -> None:
    with sqlite3.connect(":memory:") as conn:
        _legacy_facts(conn, [(101, "synthetic")])
        conn.commit()
        before = _schema_dump(conn)

        def stop_after_component(_name: str, _conn: sqlite3.Connection) -> None:
            raise RuntimeError("synthetic interruption")

        with pytest.raises(RuntimeError, match="synthetic interruption"):
            healbite_schema_migrate._migrate_borrowed_connection(
                conn,
                selected=("memory_convergence",),
                component_hook=stop_after_component,
            )
        assert _schema_dump(conn) == before
        _migrate_memory(conn)
        assert len(_outbox_rows(conn)) == 1
        _assert_database_health(conn)


def test_m18_t20_seed_failure_never_commits_schema_or_completion(monkeypatch) -> None:
    original = migrate_memory_convergence_schema

    def fail_seed(conn: sqlite3.Connection, *, now: float) -> None:
        original(
            conn,
            now=now,
            before_seed=lambda: (_ for _ in ()).throw(RuntimeError("synthetic seed failure")),
        )

    with sqlite3.connect(":memory:") as conn:
        _legacy_facts(conn, [(101, "synthetic")])
        before = _schema_dump(conn)
        monkeypatch.setattr(
            healbite_schema_migrate,
            "migrate_memory_convergence_schema",
            fail_seed,
        )
        with pytest.raises(RuntimeError, match="synthetic seed failure"):
            _migrate_memory(conn)
        assert _schema_dump(conn) == before


def test_m15_t29_memory_and_second_component_keep_canonical_order(tmp_path: Path) -> None:
    production_migration = _production_migration_module()
    db_path = tmp_path / "combined.sqlite"
    with sqlite3.connect(db_path) as conn:
        healbite_schema_migrate._migrate_borrowed_connection(
            conn, selected=healbite_schema_migrate.ALL_COMPONENTS
        )
        conn.execute("DROP INDEX idx_user_inventory_user_name")
        conn.execute(f"DROP INDEX idx_{OUTBOX_TABLE}_ready")
    states = production_migration._read_component_schema_states(db_path)
    components = [item["component"] for item in production_migration._target_migration_registry()]
    effective = production_migration._derive_effective_mutation_components(states, components)
    assert effective == ["fridge_menu", "memory_convergence"]


def test_m16_t17_t18_t30_scope_drift_fails_before_first_ddl(tmp_path: Path) -> None:
    production_migration = _production_migration_module()
    db_path = tmp_path / "scope.sqlite"
    with sqlite3.connect(db_path) as conn:
        healbite_schema_migrate._migrate_borrowed_connection(
            conn, selected=healbite_schema_migrate.ALL_COMPONENTS
        )
        conn.execute(f"DROP INDEX idx_{OUTBOX_TABLE}_ready")
    before = db_path.read_bytes()
    with pytest.raises(
        production_migration.ProductionGateError,
        match="EFFECTIVE_MUTATION_COMPONENTS_MISMATCH",
    ):
        production_migration._assert_effective_mutation_contract(db_path, [])
    assert db_path.read_bytes() == before


def test_m19_t25_old_code_accepts_new_additive_schema() -> None:
    with sqlite3.connect(":memory:") as conn:
        migrate_memory_convergence_schema(conn, now=0)
        conn.execute(
            f"INSERT INTO {FACTS_TABLE}(user_id, entity, key, value) "
            "VALUES (101, 'profile', 'goal', 'synthetic')"
        )
        row = conn.execute(
            f"SELECT id, user_id, entity, key, value, source, trust_score, created_at, updated_at "
            f"FROM {FACTS_TABLE}"
        ).fetchone()
        assert row is not None and row[1] == 101


def test_m20_t26_t27_target_runtime_requires_staged_schema_and_does_not_write(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as conn:
        _legacy_facts(conn, [])
    disabled_analytics = MemoryAnalyticsLogger(legacy, enabled=False)
    with pytest.raises(sqlite3.DatabaseError, match="staged migration is required"):
        HealBiteMemoryBridge(
            legacy,
            analytics_logger=disabled_analytics,
            background_write=False,
            ensure_schema_on_init=False,
        )

    current = tmp_path / "current.sqlite"
    with sqlite3.connect(current) as conn:
        migrate_memory_convergence_schema(conn, now=0)
    before = current.read_bytes()
    bridge = HealBiteMemoryBridge(
        current,
        analytics_logger=MemoryAnalyticsLogger(current, enabled=False),
        background_write=False,
        ensure_schema_on_init=False,
    )
    bridge.close()
    assert current.read_bytes() == before


def test_t13_t14_migration_contract_has_no_qdrant_or_provider_dependency() -> None:
    import gateway.memory.schema as schema_module

    names = set(schema_module.__dict__)
    assert not any("qdrant" in name.lower() or "provider" in name.lower() for name in names)


def test_t22_t24_additive_migration_preserves_integrity_fk_and_unrelated_rows() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))")
        conn.execute("INSERT INTO parent VALUES (1)")
        conn.execute("INSERT INTO child VALUES (1)")
        _legacy_facts(conn, [(101, "synthetic")])
        _migrate_memory(conn)
        assert conn.execute("SELECT * FROM child").fetchall() == [(1,)]
        _assert_database_health(conn)


def test_t28_additive_rollback_contract_preserves_old_authority_rows() -> None:
    with sqlite3.connect(":memory:") as conn:
        _legacy_facts(conn, [(101, "synthetic")])
        before = conn.execute(f"SELECT * FROM {FACTS_TABLE}").fetchall()
        _migrate_memory(conn)
        after = conn.execute(
            f"SELECT id, user_id, entity, key, value, source, trust_score, created_at, updated_at "
            f"FROM {FACTS_TABLE}"
        ).fetchall()
        assert after == before
        validate_memory_convergence_schema(conn)
