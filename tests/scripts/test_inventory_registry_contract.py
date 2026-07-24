from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from gateway.healbite_inventory import (
    INVENTORY_SCHEMA_MIGRATION_ID,
    INVENTORY_SCHEMA_MIGRATION_SHA256,
    INVENTORY_SCHEMA_SQL,
)
from scripts import healbite_schema_migrate as schema_migrate
from scripts import hermes_production_staged_migrate as production
from scripts import hermes_staged_schema_migrate as staged


def _resolver(
    raw_path: str | None,
    _synthetic_create: bool,
    _identity: schema_migrate.ProcessIdentity | None,
) -> schema_migrate.DatabaseTarget:
    assert raw_path is not None
    path = Path(raw_path)
    metadata = path.lstat()
    return schema_migrate.DatabaseTarget(
        path=path,
        classification="synthetic_registry_test_path",
        mode_before=f"{metadata.st_mode & 0o7777:04o}",
        owner_before=f"{metadata.st_uid}:{metadata.st_gid}",
        identity_before=(metadata.st_dev, metadata.st_ino),
    )


def _fresh_db(tmp_path: Path) -> Path:
    path = tmp_path / "registry.sqlite"
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    return path


def _inventory_schema_object_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name LIKE 'healbite_inventory_%' "
                "OR name LIKE 'idx_healbite_inventory_%' "
                "OR name LIKE 'sqlite_autoindex_healbite_inventory_%'"
            ).fetchone()[0]
        )


def test_registry_has_deterministic_inventory_identity_and_order() -> None:
    registry = schema_migrate._component_registry()
    assert tuple(component.name for component in registry) == (
        "household",
        "weekly",
        "shopping",
        "inventory",
    )
    manifest = schema_migrate.migration_registry_manifest()
    assert tuple(item["component"] for item in manifest) == (
        "household",
        "weekly",
        "shopping",
        "inventory",
    )
    assert manifest[-1] == {
        "component": "inventory",
        "migration_id": INVENTORY_SCHEMA_MIGRATION_ID,
        "migration_sha256": INVENTORY_SCHEMA_MIGRATION_SHA256,
    }
    assert hashlib.sha256(
        (INVENTORY_SCHEMA_SQL.strip() + "\n").encode("utf-8")
    ).hexdigest() == INVENTORY_SCHEMA_MIGRATION_SHA256


def test_registry_rejects_duplicate_component() -> None:
    registry = schema_migrate._component_registry()
    with pytest.raises(
        schema_migrate.MigrationError,
        match="MIGRATION_COMPONENT_DUPLICATE",
    ):
        schema_migrate._validate_component_registry((*registry, registry[-1]))


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("household,unknown", "COMPONENTS_UNKNOWN"),
        ("household,household", "COMPONENTS_DUPLICATE"),
    ),
)
def test_public_component_selection_rejects_unknown_and_duplicate(
    raw: str,
    expected: str,
) -> None:
    with pytest.raises(schema_migrate.MigrationError, match=expected):
        schema_migrate._parse_components(raw)


def test_inventory_migration_is_atomic_and_idempotent(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        connection.commit()

    observed: list[str] = []

    def fail_inventory(name: str, _connection: sqlite3.Connection) -> None:
        observed.append(name)
        if name == "inventory":
            raise RuntimeError("synthetic inventory failure")

    failed = schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_resolver,
        _component_hook=fail_inventory,
    )
    assert observed == ["household", "weekly", "shopping", "inventory"]
    assert failed.exit_classification == "MIGRATION_FAILED"
    assert failed.migration_commit_state == "ROLLED_BACK"
    with sqlite3.connect(db_path) as connection:
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        } == {"legacy_rows"}

    first = schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_resolver,
    )
    second = schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_resolver,
    )
    assert first.exit_classification == "SUCCESS"
    assert second.exit_classification == "SUCCESS"
    assert second.schema_changed is False
    assert _inventory_schema_object_count(db_path) == 8


def test_target_plan_payload_uses_canonical_registry() -> None:
    payload = staged._target_schema_payload()
    assert payload["components"] == [
        "household",
        "inventory",
        "shopping",
        "weekly",
    ]
    assert payload["migrations"] == schema_migrate.migration_registry_manifest()
    assert payload["migrations"][-1]["component"] == "inventory"
    assert production.PLAN_VERSION == 5
    assert "MIGRATION_REGISTRY" in production.PLAN_FIELDS


def test_target_plan_omits_absent_registry_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = schema_migrate._component_registry()
    monkeypatch.setattr(
        schema_migrate,
        "_component_registry",
        lambda: original[:-1],
    )
    payload = staged._target_schema_payload()
    assert payload["components"] == ["household", "shopping", "weekly"]
    assert all(
        item["component"] != "inventory"
        for item in payload["migrations"]
    )
