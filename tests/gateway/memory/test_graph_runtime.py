import pytest
import sqlite3
import os
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock

from gateway.memory.graph_runtime import (
    GraphRuntimeMode,
    GraphRuntimeStatus,
    GraphRuntimeReasonCode,
    MemoryGraphRuntime,
    resolve_graph_context,
)
from gateway.memory.graph_query import GraphFactQuery
from gateway.memory.graph_store import migrate_memory_graph_store_schema

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

def test_CFG_01_no_env_value(monkeypatch):
    monkeypatch.delenv("MEMORY_GRAPH_MODE", raising=False)
    runtime = MemoryGraphRuntime.from_environment(None)
    assert runtime.mode == GraphRuntimeMode.DISABLED

def test_CFG_02_disabled(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_MODE", "disabled")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert runtime.mode == GraphRuntimeMode.DISABLED

def test_CFG_03_shadow(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_MODE", "shadow")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert runtime.mode == GraphRuntimeMode.SHADOW

def test_CFG_04_serve_blocked(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_MODE", "serve")
    runtime = MemoryGraphRuntime.from_environment(None)
    assert getattr(runtime, "_unsupported_mode", False)

@pytest.mark.asyncio
async def test_LIFE_02_start_shadow_valid(temp_db, monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_MODE", "shadow")
    runtime = MemoryGraphRuntime.from_environment(temp_db)
    started = await runtime.start()
    assert started is True
    assert runtime.status == GraphRuntimeStatus.RUNNING
    await runtime.stop()

@pytest.mark.asyncio
async def test_READ_01_disabled(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.DISABLED, db_path=temp_db)
    with sqlite3.connect(temp_db) as conn:
        res = resolve_graph_context(conn, runtime=runtime, user_id=1, query=GraphFactQuery())
        assert res.status == "DISABLED"
        assert res.convergence_scheduled is False

def test_QUEUE_01_same_user():
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=None)
    runtime.schedule_convergence(1)
    runtime.schedule_convergence(1)
    assert len(runtime._queue) == 1

def test_QUEUE_03_capacity_reached():
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=None, queue_capacity=2)
    runtime.schedule_convergence(1)
    runtime.schedule_convergence(2)
    res = runtime.schedule_convergence(3)
    assert res is False
    assert len(runtime._queue) == 2

@pytest.mark.asyncio
async def test_TX_01_request_read_caller_transaction(temp_db):
    runtime = MemoryGraphRuntime(mode=GraphRuntimeMode.SHADOW, db_path=temp_db)
    await runtime.start()
    with sqlite3.connect(temp_db) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        res = resolve_graph_context(conn, runtime=runtime, user_id=1, query=GraphFactQuery())
        # transaction should still be active
        assert conn.in_transaction
    await runtime.stop()

def test_SENTINEL_LEAKAGE():
    from gateway.memory.graph_runtime import GraphRuntimeReasonCode
    sentinel = "PR7_GRAPH_RUNTIME_SECRET_SENTINEL_314159"
    for item in GraphRuntimeReasonCode:
        assert sentinel not in item.value
