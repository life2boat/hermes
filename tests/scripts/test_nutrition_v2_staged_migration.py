"""Regression and invariant test suite for HEALBITE_NUTRITION_V2_CANONICAL_MIGRATION_BRIDGE_V1.

Verifies:
1. 15-to-25 column staged migration on databases with existing data.
2. 25-to-25 column migration idempotency.
3. Partial interval column extension recovery.
4. Fail-closed rejection of incompatible schemas (bad types or unexpected columns).
5. Fail-closed rejection of corrupted databases.
6. Creation of full 25-column table when table is initially absent.
7. Unsafe path / path traversal rejection.
8. Multi-user data preservation and user_id isolation.
9. Zero DDL executed on unmigrated 15-column production schema during HealBiteNutritionDiary startup.
10. Runtime backwards compatibility: meal logging and summary querying work on 15-column schema.
11. Runtime capability: meal logging with intervals and interval summary querying on 25-column schema.
12. Target Telegram ID 248875361 admin revocation and /status security invariants.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.healbite_nutrition_diary import HealBiteNutritionDiary, NutritionRecord
from gateway.nutrition.diary_schema_v2 import (
    BASE_NUTRITION_LOG_COLUMNS,
    NUTRITION_DIARY_V2_MIGRATION_ID,
    NUTRITION_DIARY_V2_MIGRATION_SHA256,
    NUTRITION_LOG_TABLE,
    V2_NUTRITION_DIARY_COLUMNS,
    NutritionDiarySchemaClassification,
    classify_nutrition_diary_v2_schema,
    migrate_nutrition_diary_v2_schema,
)
from gateway.session import SessionSource
from gateway.slash_access import ADMIN_COMMANDS, policy_for_source
from scripts import healbite_schema_migrate

BASE_15_COLUMNS_SQL = f"""
CREATE TABLE {NUTRITION_LOG_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    meal_name TEXT NOT NULL,
    items_json TEXT NOT NULL,
    calories_kcal REAL,
    protein_g REAL,
    fat_g REAL,
    carbs_g REAL,
    confidence REAL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_summary TEXT NOT NULL,
    image_ref TEXT,
    qdrant_indexed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_{NUTRITION_LOG_TABLE}_user_occurred_at ON {NUTRITION_LOG_TABLE}(user_id, occurred_at);
CREATE INDEX idx_{NUTRITION_LOG_TABLE}_user_image_ref ON {NUTRITION_LOG_TABLE}(user_id, image_ref);
""".strip()


def _trusted_target(db_path: Path) -> healbite_schema_migrate.DatabaseTarget:
    metadata = db_path.lstat()
    return healbite_schema_migrate.DatabaseTarget(
        path=db_path,
        classification="synthetic_test_path",
        mode_before=f"{stat.S_IMODE(metadata.st_mode):04o}",
        owner_before=f"{metadata.st_uid}:{metadata.st_gid}",
        identity_before=(metadata.st_dev, metadata.st_ino),
    )


def _trusted_resolver(
    raw_path: str | None,
    synthetic_create: bool,
    _identity: healbite_schema_migrate.ProcessIdentity | None,
) -> healbite_schema_migrate.DatabaseTarget:
    assert raw_path is not None
    db_path = Path(raw_path)
    if not db_path.exists():
        if not synthetic_create:
            raise healbite_schema_migrate.MigrationError(
                healbite_schema_migrate.ExitClassification.MISSING_DATABASE,
                detail_code="DB_PATH_MISSING",
            )
        db_path.touch(mode=0o600)
        os.chmod(db_path, 0o600)
    return _trusted_target(db_path)


def _create_15_column_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(BASE_15_COLUMNS_SQL)
        conn.commit()


def _get_columns(
    conn: sqlite3.Connection, table: str = NUTRITION_LOG_TABLE
) -> list[tuple[Any, ...]]:
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def _get_column_names(
    conn: sqlite3.Connection, table: str = NUTRITION_LOG_TABLE
) -> list[str]:
    return [col[1] for col in _get_columns(conn, table)]


def test_nutrition_diary_v2_migration_15_to_25(tmp_path: Path) -> None:
    db_path = tmp_path / "healbite.db"
    _create_15_column_db(db_path)

    # Insert existing production-like rows
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {NUTRITION_LOG_TABLE}(
                user_id, source, meal_name, items_json, calories_kcal,
                protein_g, fat_g, carbs_g, confidence, occurred_at,
                raw_summary, image_ref, qdrant_indexed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                101,
                "photo",
                "Oatmeal",
                '["oats", "milk"]',
                350.0,
                12.0,
                6.0,
                60.0,
                0.95,
                "2026-10-19 08:00:00",
                "Oatmeal with milk",
                "img_001.jpg",
                1,
            ),
        )
        conn.commit()

    assert len(_get_column_names(sqlite3.connect(db_path))) == 15

    # Run migration targeting only nutrition_diary_v2
    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )

    assert result.exit_code == 0
    assert result.exit_classification == "SUCCESS"
    assert result.schema_changed is True
    assert len(result.phases) == 1
    assert result.phases[0].name == "nutrition_diary_v2"
    assert result.phases[0].schema_state == "CURRENT"

    # Verify 25 columns exist
    with sqlite3.connect(db_path) as conn:
        cols = _get_column_names(conn)
        assert len(cols) == 25
        for v2_name, _ in V2_NUTRITION_DIARY_COLUMNS:
            assert v2_name in cols

        # Verify existing data preserved and interval columns are NULL
        row = conn.execute(
            f"SELECT * FROM {NUTRITION_LOG_TABLE} WHERE user_id = 101"
        ).fetchone()
        assert row is not None
        assert row[1] == 101  # user_id
        assert row[3] == "Oatmeal"
        assert row[5] == 350.0
        # Check newly added v2 columns are NULL
        for idx in range(15, 25):
            assert row[idx] is None


def test_nutrition_diary_v2_migration_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "healbite.db"
    _create_15_column_db(db_path)

    # First migration: 15 -> 25
    r1 = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )
    assert r1.exit_code == 0
    assert r1.schema_changed is True

    # Second migration: 25 -> 25 (idempotent, no changes)
    r2 = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )
    assert r2.exit_code == 0
    assert r2.schema_changed is False
    assert r2.phases[0].schema_state == "CURRENT"
    assert r2.phases[0].changed is False


def test_nutrition_diary_v2_migration_partial_extension(tmp_path: Path) -> None:
    db_path = tmp_path / "healbite.db"
    _create_15_column_db(db_path)

    # Add partial interval columns manually
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"ALTER TABLE {NUTRITION_LOG_TABLE} ADD COLUMN calories_min REAL")
        conn.execute(f"ALTER TABLE {NUTRITION_LOG_TABLE} ADD COLUMN calories_max REAL")
        conn.commit()

    with sqlite3.connect(db_path) as conn:
        assert (
            classify_nutrition_diary_v2_schema(conn)
            is NutritionDiarySchemaClassification.KNOWN_COMPATIBLE_PARTIAL
        )
        assert len(_get_column_names(conn)) == 17

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )

    assert result.exit_code == 0
    assert result.schema_changed is True
    with sqlite3.connect(db_path) as conn:
        assert (
            classify_nutrition_diary_v2_schema(conn)
            is NutritionDiarySchemaClassification.CURRENT
        )
        assert len(_get_column_names(conn)) == 25


def test_nutrition_diary_v2_migration_incompatible_schema_fails_closed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "healbite.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create table with incompatible column type (calories_kcal TEXT instead of REAL)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE {NUTRITION_LOG_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                meal_name TEXT NOT NULL,
                items_json TEXT NOT NULL,
                calories_kcal TEXT,
                protein_g REAL,
                fat_g REAL,
                carbs_g REAL,
                confidence REAL,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                raw_summary TEXT NOT NULL,
                image_ref TEXT,
                qdrant_indexed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()

    with sqlite3.connect(db_path) as conn:
        assert (
            classify_nutrition_diary_v2_schema(conn)
            is NutritionDiarySchemaClassification.INCOMPATIBLE
        )

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )

    assert result.exit_code == healbite_schema_migrate.EXIT_SCHEMA_PRECONDITION
    assert result.exit_classification == "INCOMPATIBLE_SCHEMA"
    assert result.schema_changed is False


def test_nutrition_diary_v2_migration_absent_table(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE placeholder (id INT)")
        conn.commit()

    with sqlite3.connect(db_path) as conn:
        assert (
            classify_nutrition_diary_v2_schema(conn)
            is NutritionDiarySchemaClassification.ABSENT
        )

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )

    assert result.exit_code == 0
    assert result.schema_changed is True
    with sqlite3.connect(db_path) as conn:
        assert (
            classify_nutrition_diary_v2_schema(conn)
            is NutritionDiarySchemaClassification.CURRENT
        )
        assert len(_get_column_names(conn)) == 25


def test_nutrition_diary_v2_user_isolation_and_data_preservation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "healbite.db"
    _create_15_column_db(db_path)

    # Insert meals for user 201 and user 202
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {NUTRITION_LOG_TABLE}(
                user_id, source, meal_name, items_json, calories_kcal,
                protein_g, fat_g, carbs_g, confidence, occurred_at,
                raw_summary, image_ref, qdrant_indexed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                201,
                "photo",
                "Salad",
                '["greens"]',
                150.0,
                3.0,
                5.0,
                20.0,
                0.88,
                "2026-10-19 12:00:00",
                "Green Salad",
                "salad.jpg",
                1,
            ),
        )
        conn.execute(
            f"""
            INSERT INTO {NUTRITION_LOG_TABLE}(
                user_id, source, meal_name, items_json, calories_kcal,
                protein_g, fat_g, carbs_g, confidence, occurred_at,
                raw_summary, image_ref, qdrant_indexed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                202,
                "text",
                "Soup",
                '["broth"]',
                200.0,
                8.0,
                4.0,
                15.0,
                0.90,
                "2026-10-19 13:00:00",
                "Chicken Soup",
                None,
                0,
            ),
        )
        conn.commit()

    # Migrate to 25 columns
    healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        r201 = conn.execute(
            f"SELECT * FROM {NUTRITION_LOG_TABLE} WHERE user_id = 201"
        ).fetchall()
        r202 = conn.execute(
            f"SELECT * FROM {NUTRITION_LOG_TABLE} WHERE user_id = 202"
        ).fetchall()

        assert len(r201) == 1
        assert len(r202) == 1
        assert r201[0]["meal_name"] == "Salad"
        assert r201[0]["calories_kcal"] == 150.0
        assert r201[0]["image_ref"] == "salad.jpg"
        assert r202[0]["meal_name"] == "Soup"
        assert r202[0]["calories_kcal"] == 200.0
        assert r202[0]["image_ref"] is None


def test_runtime_zero_ddl_on_15_column_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "healbite.db"
    _create_15_column_db(db_path)

    # Initializing HealBiteNutritionDiary must NOT alter 15-column schema
    diary = HealBiteNutritionDiary(db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        cols = _get_column_names(conn)
        # MUST remain strictly 15 columns — 0 implicit DDL on startup!
        assert len(cols) == 15
        assert "calories_min" not in cols

    # Logging a meal on unmigrated 15-column schema must succeed without error
    record = NutritionRecord(
        is_food=True,
        meal_name="Apple",
        items=[{"name": "Apple", "grams": 150.0}],
        calories_kcal=75.0,
        protein_g=0.5,
        fat_g=0.3,
        carbs_g=19.0,
        confidence=0.99,
        raw_summary="1 apple",
        display_name="Apple",
        calories_min=70.0,
        calories_max=80.0,
    )
    sqlite_id, is_dup = diary.save_record(
        user_id=301,
        source="photo",
        record=record,
        image_ref=None,
        occurred_at=None,
    )
    assert sqlite_id is not None and sqlite_id > 0
    assert is_dup is False

    # Querying get_summary on 15-column schema must succeed without KeyError or IndexError
    summary = diary.get_summary(user_id=301)
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == 75.0
    assert summary["has_intervals"] is False
    assert summary["intervals"] is None

    diary.close()


def test_runtime_on_25_column_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "healbite.db"
    _create_15_column_db(db_path)

    # Run staged migration to 25 columns
    healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )

    diary = HealBiteNutritionDiary(db_path=db_path)

    # Log a meal with interval uncertainty
    record = NutritionRecord(
        is_food=True,
        meal_name="Ribeye Steak",
        items=[{"name": "Steak", "grams": 250.0}],
        calories_kcal=500.0,
        protein_g=55.0,
        fat_g=30.0,
        carbs_g=0.0,
        confidence=0.92,
        raw_summary="Ribeye Steak",
        display_name="Ribeye Steak",
        calories_min=450.0,
        calories_max=550.0,
        protein_min=50.0,
        protein_max=60.0,
        fat_min=27.0,
        fat_max=33.0,
        carbs_min=0.0,
        carbs_max=0.0,
        uncertainty_model_type="hipe_visual_scale_anchor",
        is_approximate_range=True,
    )
    sqlite_id, is_dup = diary.save_record(
        user_id=401,
        source="photo",
        record=record,
        image_ref=None,
        occurred_at=None,
    )
    assert sqlite_id is not None and sqlite_id > 0
    assert is_dup is False

    # Verify interval values stored in 25-column database
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {NUTRITION_LOG_TABLE} WHERE id = ?", (sqlite_id,)
        ).fetchone()
        assert row is not None
        assert row["calories_min"] == 450.0
        assert row["calories_max"] == 550.0
        assert row["protein_min"] == 50.0
        assert row["uncertainty_model_type"] == "hipe_visual_scale_anchor"
        assert row["is_approximate_range"] == 1

    # Query get_summary: intervals are properly populated
    summary = diary.get_summary(user_id=401)
    assert summary["entry_count"] == 1
    assert summary["has_intervals"] is True
    assert summary["intervals"] is not None
    assert summary["intervals"]["calories_kcal"]["min"] == 450.0
    assert summary["intervals"]["calories_kcal"]["center"] == 500.0
    assert summary["intervals"]["calories_kcal"]["max"] == 550.0

    diary.close()


def test_target_admin_revocation_and_status_command_security() -> None:
    """Proves target user 248875361 is strictly denied admin privileges and /status is admin-only."""
    target_id = "248875361"
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="dummy",
                extra={
                    "allow_admin_from": ["999999"],
                    "group_allow_admin_from": ["999999"],
                    "user_allowed_commands": ["help", "summary"],
                },
            )
        }
    )

    # 1. Target user in DM scope is denied admin
    target_source_dm = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=target_id,
        chat_id=target_id,
        user_name="TargetUser",
        chat_type="dm",
    )
    policy_dm = policy_for_source(config, target_source_dm)
    assert policy_dm.is_admin(target_id) is False
    assert "status" in ADMIN_COMMANDS
    assert policy_dm.can_run(target_id, "status") is False

    # 2. Target user in Group scope is denied admin
    target_source_grp = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=target_id,
        chat_id="-100999888",
        user_name="TargetUser",
        chat_type="group",
    )
    policy_grp = policy_for_source(config, target_source_grp)
    assert policy_grp.is_admin(target_id) is False
    assert policy_grp.can_run(target_id, "status") is False

    # 3. Owner user retains admin access
    owner_source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="999999",
        chat_id="999999",
        user_name="OwnerUser",
        chat_type="dm",
    )
    owner_policy = policy_for_source(config, owner_source)
    assert owner_policy.is_admin("999999") is True
    assert owner_policy.can_run("999999", "status") is True


def test_nutrition_diary_v2_migration_corrupt_database(tmp_path: Path) -> None:
    db_path = tmp_path / "corrupt.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 1024)

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        components="nutrition_diary_v2",
        _target_resolver=_trusted_resolver,
    )
    assert result.exit_code != 0
    assert result.schema_changed is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX path validation requires POSIX platform")
def test_nutrition_diary_v2_migration_unsafe_path() -> None:
    result = healbite_schema_migrate.run_migration(
        db_path="/tmp/../etc/passwd",
        components="nutrition_diary_v2",
    )
    assert result.exit_code == healbite_schema_migrate.EXIT_UNSAFE_PATH
    assert result.exit_classification == "UNSAFE_PATH"


def test_nutrition_diary_v2_migration_manifest_digest_integrity() -> None:
    assert NUTRITION_DIARY_V2_MIGRATION_ID == "healbite-nutrition-diary-schema-v2"
    assert len(NUTRITION_DIARY_V2_MIGRATION_SHA256) == 64
    assert NUTRITION_DIARY_V2_MIGRATION_SHA256 != "0" * 64

    manifest = healbite_schema_migrate.migration_registry_manifest()
    entry = next((e for e in manifest if e["component"] == "nutrition_diary_v2"), None)
    assert entry is not None
    assert entry["migration_id"] == NUTRITION_DIARY_V2_MIGRATION_ID
    assert entry["migration_sha256"] == NUTRITION_DIARY_V2_MIGRATION_SHA256
