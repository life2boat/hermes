from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.hermes_isolated_rollback_drill import (
    check_qdrant_state,
    create_sqlite_snapshot,
    inspect_container_image,
)


def test_create_sqlite_snapshot_consistent_and_permissions(tmp_path: Path):
    live_db = tmp_path / "live.db"
    with sqlite3.connect(live_db) as conn:
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT);")
        conn.execute("INSERT INTO test (val) VALUES ('alpha'), ('beta');")
        conn.commit()

    snapshot_db = tmp_path / "snapshot.db"
    create_sqlite_snapshot(live_db, snapshot_db)

    assert snapshot_db.is_file()
    # Check permissions 0600 (on POSIX systems)
    if os.name != "nt":
        stat = snapshot_db.stat()
        mode_str = oct(stat.st_mode)[-4:]
        assert mode_str == "0600"

    with sqlite3.connect(snapshot_db) as conn:
        rows = conn.execute("SELECT val FROM test ORDER BY id;").fetchall()
        assert rows == [("alpha",), ("beta",)]
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        assert integrity == "ok"


def test_check_qdrant_state_handles_errors_gracefully():
    state = check_qdrant_state("http://127.0.0.1:9999")
    assert "ERROR" in state["health"]
    assert isinstance(state["collections"], dict)


def test_inspect_container_image_extracts_digest_and_revision():
    inspect_output = [
        {
            "Image": (
                "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            ),
            "ImageManifestDescriptor": {
                "digest": (
                    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
                )
            },
            "Config": {"Labels": {"org.opencontainers.image.revision": "abc123rev"}},
        }
    ]
    with patch("subprocess.check_output", return_value=json.dumps(inspect_output)):
        digest, rev = inspect_container_image("hermes-bot")
        assert (
            digest
            == "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        )
        assert rev == "abc123rev"
