"""In-memory thread-safe registry for ExecutionRequest and ExecutionResult records."""

from __future__ import annotations

import threading

from ai_engineering.execution.host_contracts import (
    ExecutionHostError,
    ExecutionRequest,
    ExecutionResult,
    HostBlockingReason,
)


class ExecutionRegistry:
    """In-memory domain registry tracking command execution requests and outcomes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, ExecutionRequest] = {}
        self._results: dict[str, ExecutionResult] = {}
        self._by_run: dict[str, list[ExecutionResult]] = {}

    def record_request(self, request: ExecutionRequest) -> None:
        """Record an immutable execution request with idempotency and collision checks."""
        if not isinstance(request, ExecutionRequest):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "Expected ExecutionRequest instance",
            )

        with self._lock:
            if request.execution_id in self._requests:
                existing = self._requests[request.execution_id]
                if existing == request:
                    return  # Exact idempotent registration
                raise ExecutionHostError(
                    HostBlockingReason.EXECUTION_ID_COLLISION.value,
                    f"Execution ID collision with divergent request: {request.execution_id}",
                )
            self._requests[request.execution_id] = request

    def record_result(self, result: ExecutionResult) -> None:
        """Record an execution outcome."""
        if not isinstance(result, ExecutionResult):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "Expected ExecutionResult instance",
            )

        with self._lock:
            self._results[result.execution_id] = result
            if result.run_id not in self._by_run:
                self._by_run[result.run_id] = []
            self._by_run[result.run_id].append(result)

    def get_request(self, execution_id: str) -> ExecutionRequest | None:
        with self._lock:
            return self._requests.get(execution_id)

    def get_result(self, execution_id: str) -> ExecutionResult | None:
        with self._lock:
            return self._results.get(execution_id)

    def list_results_for_run(self, run_id: str) -> tuple[ExecutionResult, ...]:
        with self._lock:
            history = self._by_run.get(run_id, [])
            return tuple(history)
