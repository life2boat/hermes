from typing import Protocol, runtime_checkable

from ai_engineering.supervisor.dispatch.contracts import DispatchEnvelope, DispatchReceipt

@runtime_checkable
class WorkerDispatcher(Protocol):
    def dispatch(self, envelope: DispatchEnvelope) -> DispatchReceipt:
        """Dispatch a task envelope to a worker asynchronously/offline.
        Returns a DispatchReceipt indicating successful delivery or failure to deliver."""
        ...

    def poll_result(self, dispatch_id: str) -> object | None:
        """Poll for worker result for a given dispatch_id."""
        ...

class FakeWorkerDispatcher(WorkerDispatcher):
    def __init__(self, fail_to_dispatch: bool = False, worker_identity: str = "fake-worker-1", expected_channel: str = "fake_queue") -> None:
        self.fail_to_dispatch = fail_to_dispatch
        self.worker_identity = worker_identity
        self.expected_channel = expected_channel
        self.dispatched_envelopes: list[DispatchEnvelope] = []
        self._staged_results: dict[str, object] = {}

    def stage_result(self, dispatch_id: str, result: object) -> None:
        """Stage a simulated worker result for a dispatch_id."""
        self._staged_results[dispatch_id] = result

    def poll_result(self, dispatch_id: str) -> object | None:
        """Retrieve staged result if available."""
        return self._staged_results.get(dispatch_id)

    def dispatch(self, envelope: DispatchEnvelope) -> DispatchReceipt:
        from ai_engineering.supervisor.dispatch.contracts import DispatchReceipt, DispatchStatus
        from datetime import datetime, timezone
        
        status = DispatchStatus.FAILED if self.fail_to_dispatch else DispatchStatus.DISPATCHED
        reason_code = "DISPATCH_UNAVAILABLE" if self.fail_to_dispatch else None

        if not self.fail_to_dispatch:
            self.dispatched_envelopes.append(envelope)

        return DispatchReceipt(
            schema_version="hermes.dispatch-receipt.v1",
            receipt_id=f"receipt-{envelope.dispatch_id}",
            dispatch_id=envelope.dispatch_id,
            task_id=envelope.task_id,
            attempt_id=envelope.attempt_id,
            policy_receipt_id=envelope.policy_receipt_id,
            worker_kind=envelope.worker_kind,
            worker_identity=self.worker_identity,
            dispatched_at_utc=datetime.now(timezone.utc).isoformat(),
            expected_result_channel=self.expected_channel,
            status=status,
            reason_code=reason_code
        )

class AntigravityAdapter(WorkerDispatcher):
    """Adapter for Antigravity worker integration via Python SDK.
    Since we are currently implementing Task 4 and computer use (Task 5) is out of scope,
    if no structured dispatch is available, it reports ANTIGRAVITY_STRUCTURED_DISPATCH_UNAVAILABLE."""
    def dispatch(self, envelope: DispatchEnvelope) -> DispatchReceipt:
        from ai_engineering.supervisor.dispatch.contracts import DispatchReceipt, DispatchStatus
        from datetime import datetime, timezone
        # Per prompt instructions: 
        # "The real Antigravity adapter must report: ANTIGRAVITY_STRUCTURED_DISPATCH_UNAVAILABLE 
        # when no supported integration is available. It must never silently pretend a task was dispatched."
        return DispatchReceipt(
            schema_version="hermes.dispatch-receipt.v1",
            receipt_id=f"receipt-{envelope.dispatch_id}",
            dispatch_id=envelope.dispatch_id,
            task_id=envelope.task_id,
            attempt_id=envelope.attempt_id,
            policy_receipt_id=envelope.policy_receipt_id,
            worker_kind=envelope.worker_kind,
            worker_identity="antigravity-sdk-worker",
            dispatched_at_utc=datetime.now(timezone.utc).isoformat(),
            expected_result_channel="none",
            status=DispatchStatus.FAILED,
            reason_code="ANTIGRAVITY_STRUCTURED_DISPATCH_UNAVAILABLE"
        )

    def poll_result(self, dispatch_id: str) -> object | None:
        return None
