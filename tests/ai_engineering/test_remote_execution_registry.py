"""Unit tests for RemoteExecutionRegistry."""

from __future__ import annotations

import pytest

from ai_engineering.execution.remote_contracts import (
    RemoteBlockingReason,
    RemoteExecutionError,
    RemoteOutputChunk,
    RemoteProcessIdentity,
    RemoteSessionIdentity,
)
from ai_engineering.execution.remote_registry import RemoteExecutionRegistry


def test_registry_session_and_process_idempotency_and_collision():
    reg = RemoteExecutionRegistry()
    s1 = RemoteSessionIdentity(session_id="s1", execution_host_id="h1", execution_epoch=1)
    reg.register_session(s1)
    reg.register_session(s1)  # Idempotent

    s2 = RemoteSessionIdentity(session_id="s1", execution_host_id="h2", execution_epoch=1)
    with pytest.raises(RemoteExecutionError):
        reg.register_session(s2)  # Collision


def test_registry_output_chunks_ordering():
    reg = RemoteExecutionRegistry()
    c0 = RemoteOutputChunk("e1", "s1", 1, "stdout", 0, "first chunk")
    c1 = RemoteOutputChunk("e1", "s1", 1, "stdout", 1, "second chunk")
    c_stale = RemoteOutputChunk("e1", "s1", 1, "stdout", 0, "stale duplicate chunk")

    reg.record_output_chunk(c0)
    reg.record_output_chunk(c1)
    reg.record_output_chunk(c_stale)  # Stale sequence ignored

    chunks = reg.get_output_chunks("e1", "stdout")
    assert len(chunks) == 2
    assert chunks[0].data == "first chunk"
    assert chunks[1].data == "second chunk"
