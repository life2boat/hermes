from __future__ import annotations

import contextlib
import importlib.util
import io
import sqlite3
import sys
from pathlib import Path

from gateway.memory.analytics import MemoryAnalyticsLogger

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "memory_analytics_report.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("memory_analytics_report", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
memory_analytics_report = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(memory_analytics_report)


def test_memory_analytics_report_prints_summary(tmp_path, monkeypatch):
    db_path = tmp_path / "analytics.sqlite"
    logger = MemoryAnalyticsLogger(db_path, background_write=False)
    logger.log_search(user_id=1, source="qdrant", found_count=1, processing_time_ms=10.0)
    logger.log_search(user_id=1, source="like", found_count=1, processing_time_ms=30.0)
    logger.log_injection(user_id=None, source="context_files", found_count=2, processing_time_ms=4.0)
    logger.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_analytics_report.py", "--db-path", str(db_path)],
    )

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = memory_analytics_report.main()

    report = output.getvalue()
    assert exit_code == 0
    assert "Total Memory Searches: 2" in report
    assert "Qdrant Hit Rate (%): 50.00" in report
    assert "SQLite Fallback Rate (%): 50.00" in report
    assert "Average Search Latency (ms): 20.00" in report
    assert "Average Facts Injected per LLM Call: 2.00" in report


def test_memory_analytics_report_supports_hours_and_user_filters(tmp_path, monkeypatch):
    db_path = tmp_path / "analytics.sqlite"
    logger = MemoryAnalyticsLogger(db_path, background_write=False)
    logger.log_search(user_id=1, source="qdrant", found_count=1, processing_time_ms=10.0)
    logger.log_search(user_id=2, source="like", found_count=1, processing_time_ms=30.0)
    logger.log_injection(user_id=1, source="context_files", found_count=3, processing_time_ms=4.0)
    logger.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE memory_analytics_logs SET created_at = '2000-01-01 00:00:00' WHERE user_id = 2"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_analytics_report.py",
            "--db-path",
            str(db_path),
            "--hours",
            "24",
            "--user-id",
            "1",
        ],
    )

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = memory_analytics_report.main()

    report = output.getvalue()
    assert exit_code == 0
    assert "Total Memory Searches: 1" in report
    assert "Qdrant Hit Rate (%): 100.00" in report
    assert "SQLite Fallback Rate (%): 0.00" in report
    assert "Scope: last 24h, user_id=1" in report