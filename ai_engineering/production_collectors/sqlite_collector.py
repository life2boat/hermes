import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

from ai_engineering.production_runtime_attestation import (
    CollectorResult,
    CollectorStatus,
    create_collector_result,
)


class SqliteReadOnlyCollector:
    """Collects structural SQLite state safely using explicit read-only mode."""

    collector_id = "sqlite_read_only"

    def __init__(self, canonical_path: str) -> None:
        self.canonical_path = canonical_path

    def collect(self) -> CollectorResult:
        try:
            db_path = Path(self.canonical_path).resolve()
            
            # 1. Structural file check
            if not db_path.exists():
                return create_collector_result(
                    self.collector_id,
                    CollectorStatus.UNAVAILABLE,
                    {},
                )
            if not db_path.is_file():
                return create_collector_result(
                    self.collector_id,
                    CollectorStatus.UNAVAILABLE,
                    {},
                )

            # 2. Open read-only explicitly via URI
            # Convert to absolute POSIX-style URI for sqlite3
            uri_path = urllib.parse.quote(db_path.as_posix())
            uri = f"file:{uri_path}?mode=ro"

            observations: dict[str, Any] = {
                "sqlite_open_read_only": False,
                "integrity": "unknown",
                "foreign_key_violations": -1,
            }

            # We strictly avoid writes
            try:
                with sqlite3.connect(uri, uri=True, timeout=5) as conn:
                    # Test we can actually read
                    conn.execute("SELECT 1").fetchall()
                    observations["sqlite_open_read_only"] = True
                    
                    # Run PRAGMA quick_check
                    try:
                        cursor = conn.execute("PRAGMA quick_check")
                        result = cursor.fetchone()
                        if result:
                            observations["integrity"] = result[0].lower()
                    except sqlite3.Error:
                        observations["integrity"] = "error"
                        
                    # Run PRAGMA foreign_key_check
                    try:
                        cursor = conn.execute("PRAGMA foreign_key_check")
                        violations = cursor.fetchall()
                        observations["foreign_key_violations"] = len(violations)
                    except sqlite3.Error:
                        observations["foreign_key_violations"] = -1
                        
            except sqlite3.OperationalError:
                # E.g. permissions error, lock, or can't open
                return create_collector_result(
                    self.collector_id,
                    CollectorStatus.UNAVAILABLE,
                    {},
                )

            return create_collector_result(
                self.collector_id, CollectorStatus.AVAILABLE, observations
            )

        except Exception:
            return create_collector_result(
                self.collector_id, CollectorStatus.UNAVAILABLE, {}
            )
