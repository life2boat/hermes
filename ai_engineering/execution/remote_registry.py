"""In-memory thread-safe registry for RemoteSessionIdentity, RemoteProcessIdentity, and output streams."""

from __future__ import annotations

import threading

from ai_engineering.execution.remote_contracts import (
    RemoteBlockingReason,
    RemoteExecutionError,
    RemoteOutputChunk,
    RemoteProcessIdentity,
    RemoteReconciliationResult,
    RemoteSessionIdentity,
)


class RemoteExecutionRegistry:
    """In-memory thread-safe registry tracking remote sessions, processes, and chunk streams."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, RemoteSessionIdentity] = {}
        self._processes: dict[str, RemoteProcessIdentity] = {}
        self._reconciliations: dict[str, RemoteReconciliationResult] = {}
        self._output_chunks: dict[str, list[RemoteOutputChunk]] = {}

    def register_session(self, session: RemoteSessionIdentity) -> None:
        if not isinstance(session, RemoteSessionIdentity):
            raise RemoteExecutionError(
                RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "Expected RemoteSessionIdentity instance",
            )
        with self._lock:
            if session.session_id in self._sessions:
                existing = self._sessions[session.session_id]
                if existing == session:
                    return
                raise RemoteExecutionError(
                    RemoteBlockingReason.REMOTE_SESSION_INVALID.value,
                    f"Session collision with divergent identity: {session.session_id}",
                )
            self._sessions[session.session_id] = session

    def register_process(self, proc: RemoteProcessIdentity) -> None:
        if not isinstance(proc, RemoteProcessIdentity):
            raise RemoteExecutionError(
                RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "Expected RemoteProcessIdentity instance",
            )
        with self._lock:
            if proc.execution_id in self._processes:
                existing = self._processes[proc.execution_id]
                if existing == proc:
                    return
                raise RemoteExecutionError(
                    RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                    f"Process collision with divergent identity: {proc.execution_id}",
                )
            self._processes[proc.execution_id] = proc

    def record_output_chunk(self, chunk: RemoteOutputChunk) -> None:
        with self._lock:
            key = f"{chunk.execution_id}:{chunk.stream}"
            if key not in self._output_chunks:
                self._output_chunks[key] = []
            chunks = self._output_chunks[key]
            # Check monotonic sequence
            if chunks:
                last_seq = chunks[-1].sequence_number
                if chunk.sequence_number == last_seq and chunks[-1] == chunk:
                    return  # Idempotent duplicate
                if chunk.sequence_number <= last_seq:
                    # Stale / out of order chunk rejected
                    return
            chunks.append(chunk)

    def record_reconciliation(self, result: RemoteReconciliationResult) -> None:
        with self._lock:
            self._reconciliations[result.execution_id] = result

    def get_session(self, session_id: str) -> RemoteSessionIdentity | None:
        with self._lock:
            return self._sessions.get(session_id)

    def get_process(self, execution_id: str) -> RemoteProcessIdentity | None:
        with self._lock:
            return self._processes.get(execution_id)

    def get_output_chunks(self, execution_id: str, stream: str) -> tuple[RemoteOutputChunk, ...]:
        with self._lock:
            key = f"{execution_id}:{stream}"
            return tuple(self._output_chunks.get(key, []))

    def get_reconciliation(self, execution_id: str) -> RemoteReconciliationResult | None:
        with self._lock:
            return self._reconciliations.get(execution_id)
