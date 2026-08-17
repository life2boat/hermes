import sqlite3
import subprocess
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from ai_engineering.production_collectors import (
    DockerRuntimeCollector,
    QdrantReadOnlyCollector,
    SecretSourceStructuralCollector,
    SqliteReadOnlyCollector,
    collect_production_attestation,
)
from ai_engineering.production_runtime_attestation import (
    CollectorStatus,
    ComparisonStatus,
    ProductionRuntimeAttestationError,
    compare_production_runtime,
    create_intended_state,
    create_collector_result,
)


@pytest.fixture
def mock_subprocess_run():
    with patch("subprocess.run") as mock_run:
        yield mock_run


@pytest.fixture
def mock_urlopen():
    with patch("urllib.request.urlopen") as mock_url:
        yield mock_url


# --- Docker Collector Tests ---
def test_docker_healthy(mock_subprocess_run):
    # Simulate first inspect for general props
    proc1 = MagicMock()
    proc1.returncode = 0
    proc1.stdout = "true|5|ghcr.io/life2boat/hermes:latest|healbite-s72-family-invite-main|hermes-bot"
    
    # Simulate second inspect for health
    proc2 = MagicMock()
    proc2.returncode = 0
    proc2.stdout = "healthy"
    
    # Simulate third inspect for mounts
    proc3 = MagicMock()
    proc3.returncode = 0
    proc3.stdout = "/host/path::/home/hermes/healbite.db||"
    
    mock_subprocess_run.side_effect = [proc1, proc2, proc3]

    collector = DockerRuntimeCollector("hermes-bot", "/home/hermes/healbite.db")
    result = collector.collect()
    
    assert result.status == CollectorStatus.AVAILABLE
    assert result.observations["running"] is True
    assert result.observations["restart_count"] == 5
    assert result.observations["compose_project"] == "healbite-s72-family-invite-main"
    assert result.observations["compose_service"] == "hermes-bot"
    assert result.observations["health_status"] == "healthy"
    assert result.observations["db_mount_matches_expected"] is True


def test_docker_stopped(mock_subprocess_run):
    proc1 = MagicMock()
    proc1.returncode = 0
    proc1.stdout = "false|1|ghcr.io/life2boat/hermes:latest|healbite-s72-family-invite-main|hermes-bot"
    proc2 = MagicMock()
    proc2.returncode = 0
    proc2.stdout = "none"
    mock_subprocess_run.side_effect = [proc1, proc2]

    collector = DockerRuntimeCollector("hermes-bot")
    result = collector.collect()
    
    assert result.status == CollectorStatus.AVAILABLE
    assert result.observations["running"] is False


def test_docker_timeout(mock_subprocess_run):
    mock_subprocess_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
    
    collector = DockerRuntimeCollector("hermes-bot")
    result = collector.collect()
    assert result.status == CollectorStatus.UNAVAILABLE
    assert "error" not in result.observations


def test_docker_secret_redaction(mock_subprocess_run):
    pass


# --- SQLite Collector Tests ---
def test_sqlite_healthy(tmp_path):
    db_path = tmp_path / "test.db"
    # Create valid DB
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE t1 (id INT PRIMARY KEY)")
        conn.execute("INSERT INTO t1 VALUES (1)")
    
    collector = SqliteReadOnlyCollector(str(db_path))
    result = collector.collect()
    
    assert result.status == CollectorStatus.AVAILABLE
    assert result.observations["sqlite_open_read_only"] is True
    assert result.observations["integrity"] == "ok"
    assert result.observations["foreign_key_violations"] == 0


def test_sqlite_fk_violation(tmp_path):
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE p (id INT PRIMARY KEY)")
        conn.execute("CREATE TABLE c (id INT, p_id INT REFERENCES p(id))")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO c VALUES (1, 999)") # Violation
    
    collector = SqliteReadOnlyCollector(str(db_path))
    result = collector.collect()
    
    assert result.status == CollectorStatus.AVAILABLE
    assert result.observations["sqlite_open_read_only"] is True
    assert result.observations["foreign_key_violations"] > 0


def test_sqlite_cannot_open_readonly(tmp_path):
    # db doesn't exist
    db_path = tmp_path / "missing.db"
    collector = SqliteReadOnlyCollector(str(db_path))
    result = collector.collect()
    assert result.status == CollectorStatus.UNAVAILABLE
    assert "error" not in result.observations


def test_sqlite_attempted_writable_mode_rejected(tmp_path):
    # Our implementation explicitly uses ?mode=ro. If someone tried to write, what happens?
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE t1 (id INT PRIMARY KEY)")
    
    # Let's verify we can't write to it using the URI we construct
    import urllib.parse
    uri_path = urllib.parse.quote(db_path.as_posix())
    uri = f"file:{uri_path}?mode=ro"
    
    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        with sqlite3.connect(uri, uri=True) as conn:
            conn.execute("INSERT INTO t1 VALUES (2)")


# --- Qdrant Collector Tests ---
def test_qdrant_healthy(mock_urlopen):
    resp1 = MagicMock()
    resp1.__enter__.return_value = resp1
    resp1.status = 200
    
    resp2 = MagicMock()
    resp2.__enter__.return_value = resp2
    resp2.status = 200
    resp2.read.return_value = b'{"result": {"status": "green"}}'
    
    mock_urlopen.side_effect = [resp1, resp2]
    
    collector = QdrantReadOnlyCollector("http://qdrant", "my_collection")
    result = collector.collect()
    
    assert result.status == CollectorStatus.AVAILABLE
    assert result.observations["reachable"] is True
    assert result.observations["collection_exists"] is True
    assert result.observations["collection_status"] == "green"


def test_qdrant_unavailable(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")
    
    collector = QdrantReadOnlyCollector("http://qdrant", "my_collection")
    result = collector.collect()
    
    assert result.status == CollectorStatus.UNAVAILABLE
    assert "error" not in result.observations


def test_qdrant_credential_requirement(mock_urlopen):
    # Qdrant requires credentials and returns 401
    mock_urlopen.side_effect = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    
    collector = QdrantReadOnlyCollector("http://qdrant", "my_collection")
    result = collector.collect()
    
    assert result.status == CollectorStatus.UNAVAILABLE
    assert "error" not in result.observations


# --- Orchestrator & Comparison Tests ---
def test_raw_collector_output_sanitization(mock_subprocess_run):
    proc1 = MagicMock()
    proc1.returncode = 0
    proc1.stdout = "true|0|image-token-1234567890abcdef|proj|svc"
    proc2 = MagicMock()
    proc2.returncode = 0
    proc2.stdout = "none"
    mock_subprocess_run.side_effect = [proc1, proc2]
    
    class DummyCollector:
        collector_id = "dummy"
        def collect(self):
            return create_collector_result("dummy", "AVAILABLE", {"api_key": "123456"})

    att = collect_production_attestation("target", [DummyCollector()])
    obs = att.collectors[0].observations
    
    assert obs["api_key"] == "<REDACTED>"


def test_comparison_match():
    # Construct an attestation
    class DummyCollector:
        collector_id = "test_col"
        def collect(self):
            return create_collector_result("test_col", "AVAILABLE", {"val": 1})

    att = collect_production_attestation("prod", [DummyCollector()])
    
    intended = create_intended_state(target="prod", expected_observations={"test_col": {"val": 1}})
    comp = compare_production_runtime(intended, att)
    
    assert comp.status == ComparisonStatus.MATCH


def test_comparison_drift():
    class DummyCollector:
        collector_id = "test_col"
        def collect(self):
            return create_collector_result("test_col", "AVAILABLE", {"val": 2})

    att = collect_production_attestation("prod", [DummyCollector()])
    
    intended = create_intended_state(target="prod", expected_observations={"test_col": {"val": 1}})
    comp = compare_production_runtime(intended, att)
    
    assert comp.status == ComparisonStatus.DRIFT
    assert "test_col.val" in comp.drifted_observations


def test_comparison_insufficient():
    class DummyCollector:
        collector_id = "test_col"
        def collect(self):
            return create_collector_result("test_col", "UNAVAILABLE", {})

    att = collect_production_attestation("prod", [DummyCollector()])
    
    intended = create_intended_state(target="prod", expected_observations={"test_col": {"val": 1}})
    comp = compare_production_runtime(intended, att)
    
    assert comp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE
    assert "test_col.val" in comp.missing_observations


def test_secret_source_structural(tmp_path):
    sec = tmp_path / "secret.env"
    sec.write_text("API_KEY=123")
    
    collector = SecretSourceStructuralCollector(str(sec))
    res = collector.collect()
    
    assert res.status == CollectorStatus.AVAILABLE
    assert res.observations["approved_source_exists"] is True
    assert "approved_source_mode" in res.observations
    assert "API_KEY" not in str(res.observations)
