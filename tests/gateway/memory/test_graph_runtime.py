import pytest
import sqlite3
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock
import logging
import time

async def _wait_until(predicate, timeout=2.0, step=0.01):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Timeout waiting for condition")
        await asyncio.sleep(step)

from gateway.memory.graph_runtime import (
    GraphRuntimeMode,
    GraphRuntimeStatus,
    GraphRuntimeReasonCode,
    MemoryGraphRuntime,
    resolve_graph_context,
)
from gateway.memory.graph_query import GraphFactQuery
from gateway.memory.graph_convergence import GraphConvergenceIntegrityError

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test.sqlite"
    from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge
    bridge = HealBiteMemoryBridge(db_path, background_write=False)
    bridge.close()
    with sqlite3.connect(db_path) as conn:
        from gateway.memory.graph_store import migrate_memory_graph_store_schema
        migrate_memory_graph_store_schema(conn)
    return db_path

# CONFIG
def test_CONFIG_default_disabled(monkeypatch):
    monkeypatch.delenv("MEMORY_GRAPH_MODE", raising=False)
    runtime = MemoryGraphRuntime.from_environment(None)
    assert runtime.mode == GraphRuntimeMode.DISABLED

def test_CONFIG_disabled(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_MODE", "disabled")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert runtime.mode == GraphRuntimeMode.DISABLED

def test_CONFIG_shadow(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_MODE", "shadow")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert runtime.mode == GraphRuntimeMode.SHADOW

def test_CONFIG_serve_blocked(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_MODE", "serve")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert getattr(runtime, "_unsupported_mode", False)

def test_CONFIG_unknown_blocked(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_MODE", "invalid_value")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert getattr(runtime, "_unsupported_mode", False)

def test_CONFIG_bounded_queue(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_QUEUE_CAPACITY", "42")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert runtime.queue_capacity == 42

def test_CONFIG_bounded_max_attempts(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_CONVERGENCE_MAX_ATTEMPTS", "2")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert runtime.max_attempts == 2

# QUEUE
def test_QUEUE_dedupe():
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=None)
    runtime.schedule_convergence(1)
    runtime.schedule_convergence(1)
    assert len(runtime._queue) == 1

def test_QUEUE_capacity():
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=None, queue_capacity=2)
    runtime.schedule_convergence(1)
    runtime.schedule_convergence(2)
    res = runtime.schedule_convergence(3)
    assert res is False
    assert len(runtime._queue) == 2

def test_QUEUE_invalid_user():
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=None)
    assert runtime.schedule_convergence(None) is False
    assert runtime.schedule_convergence("1") is False
    assert runtime.schedule_convergence(1.0) is False
    assert runtime.schedule_convergence(True) is False
    assert runtime.schedule_convergence(-1) is False
    assert len(runtime._queue) == 0

@pytest.mark.asyncio
async def test_QUEUE_schedule_after_stop_rejected():
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=None)
    await runtime.stop()
    assert runtime.schedule_convergence(1) is False
    assert len(runtime._queue) == 0

# LIFECYCLE
@pytest.mark.asyncio
async def test_LIFE_disabled_start(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.DISABLED, db_path=temp_db)
    started = await runtime.start()
    assert started is False
    assert runtime.status == GraphRuntimeStatus.DISABLED

@pytest.mark.asyncio
async def test_LIFE_valid_shadow_start(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    started = await runtime.start()
    assert started is True
    assert runtime.status == GraphRuntimeStatus.RUNNING
    await runtime.stop()

@pytest.mark.asyncio
async def test_LIFE_double_start(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    await runtime.start()
    started = await runtime.start()
    assert started is True
    assert runtime.status == GraphRuntimeStatus.RUNNING
    await runtime.stop()

@pytest.mark.asyncio
async def test_LIFE_stop(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    await runtime.start()
    await runtime.stop()
    assert runtime.status == GraphRuntimeStatus.NOT_STARTED
    assert len(runtime._queue) == 0

@pytest.mark.asyncio
async def test_LIFE_double_stop(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    await runtime.start()
    await runtime.stop()
    await runtime.stop()
    assert runtime.status == GraphRuntimeStatus.NOT_STARTED

@pytest.mark.asyncio
async def test_LIFE_missing_db():
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=Path("/tmp/nonexistent.sqlite"))
    started = await runtime.start()
    assert started is False
    assert runtime.status == GraphRuntimeStatus.BLOCKED

@pytest.mark.asyncio
async def test_LIFE_absent_schema(tmp_path):
    db = tmp_path / "empty.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE dummy(id INT)")
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=db)
    started = await runtime.start()
    assert started is False
    assert runtime.status == GraphRuntimeStatus.BLOCKED

@pytest.mark.asyncio
async def test_LIFE_partial_schema(tmp_path):
    db = tmp_path / "empty.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE _memory_graph_meta (schema_version INTEGER)")
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=db)
    started = await runtime.start()
    assert started is False
    assert runtime.status == GraphRuntimeStatus.BLOCKED

@pytest.mark.asyncio
async def test_LIFE_incompatible_schema(tmp_path):
    db = tmp_path / "empty.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE _memory_graph_meta (schema_version INTEGER)")
        conn.execute("INSERT INTO _memory_graph_meta VALUES (999)")
        conn.execute("CREATE TABLE memory_graph_nodes (x INT)")
        conn.execute("CREATE TABLE memory_graph_edges (x INT)")
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=db)
    started = await runtime.start()
    assert started is False
    assert runtime.status == GraphRuntimeStatus.BLOCKED

# PRIVACY
@pytest.mark.asyncio
async def test_PRIVACY_exception_sentinel(temp_db, caplog):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    await runtime.start()
    
    with patch("gateway.memory.graph_runtime.converge_user_graph") as mock_conv:
        mock_conv.side_effect = ValueError("PR7_GRAPH_RUNTIME_SECRET_SENTINEL_314159")
        runtime.schedule_convergence(1)
        await _wait_until(lambda: runtime.status == GraphRuntimeStatus.DEGRADED)
        
    await runtime.stop()
    assert runtime.status == GraphRuntimeStatus.DEGRADED
    
    for record in caplog.records:
        assert "PR7_GRAPH_RUNTIME_SECRET_SENTINEL_314159" not in record.message
        assert "PR7_GRAPH_RUNTIME_SECRET_SENTINEL_314159" not in (getattr(record, "exc_text", "") or "")

# READ / TRANSACTION
@pytest.mark.asyncio
async def test_READ_disabled(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.DISABLED, db_path=temp_db)
    with sqlite3.connect(temp_db) as conn:
        res = resolve_graph_context(conn, runtime=runtime, user_id=1, query=GraphFactQuery())
        assert res.status == "DISABLED"
        assert res.convergence_scheduled is False

@pytest.mark.asyncio
async def test_TX_request_owned_connection(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    await runtime.start()
    with sqlite3.connect(temp_db) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        res = resolve_graph_context(conn, runtime=runtime, user_id=1, query=GraphFactQuery())
        assert conn.in_transaction
    await runtime.stop()

# WORKER and DATA MOCKS
@pytest.mark.asyncio
async def test_WORKER_integrity_block_counters(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    await runtime.start()
    
    with patch("gateway.memory.graph_runtime.converge_user_graph") as mock_conv:
        mock_conv.side_effect = GraphConvergenceIntegrityError("Test")
        runtime.schedule_convergence(1)
        await _wait_until(lambda: runtime._counters["integrity_block_count"] == 1)
        
    await runtime.stop()
    assert runtime._counters["integrity_block_count"] == 1
    assert runtime.status == GraphRuntimeStatus.DEGRADED

@pytest.mark.asyncio
async def test_WORKER_churn_exhausted_counters(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    await runtime.start()
    
    with patch("gateway.memory.graph_runtime.converge_user_graph") as mock_conv:
        class DummyRes:
            status = MagicMock()
            status.name = "SOURCE_CHURN_RETRY_EXHAUSTED"
        mock_conv.return_value = DummyRes()
        runtime.schedule_convergence(1)
        await _wait_until(lambda: runtime._counters["churn_exhausted_count"] == 1)
        
    await runtime.stop()
    assert runtime._counters["churn_exhausted_count"] == 1
