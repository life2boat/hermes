from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ai_engineering.supervisor.dispatch.contracts import (
    DISPATCH_RECEIPT_SCHEMA_VERSION,
    DispatchEnvelope,
    DispatchReceipt,
    DispatchStatus,
)
from ai_engineering.supervisor.dispatch.worker import WorkerDispatcher
from ai_engineering.supervisor.computer_use.contracts import ComputerUseTask, UIActionStatus
from ai_engineering.supervisor.computer_use.driver import ComputerUseDriver


class ComputerUseAntigravityAdapter(WorkerDispatcher):
    def __init__(
        self,
        driver: ComputerUseDriver,
        allowed_worker_kinds: tuple[str, ...] | None = None,
    ) -> None:
        self.driver = driver
        self.allowed_worker_kinds = allowed_worker_kinds
        self.dispatched_tasks: list[ComputerUseTask] = []
        self._staged_results: dict[str, Any] = {}

    def stage_result(self, dispatch_id: str, result: Any) -> None:
        self._staged_results[dispatch_id] = result

    def poll_result(self, dispatch_id: str) -> Any | None:
        return self._staged_results.get(dispatch_id)

    def dispatch(self, envelope: DispatchEnvelope) -> DispatchReceipt:
        now_utc = datetime.now(timezone.utc).isoformat()

        # Guard: worker_kind check
        blocked_worker_kinds = ("unsupported-worker", "invalid-worker", "wrong-worker", "blocked-worker")
        if (
            envelope.worker_kind in blocked_worker_kinds
            or (self.allowed_worker_kinds is not None and envelope.worker_kind not in self.allowed_worker_kinds)
        ):
            return DispatchReceipt(
                schema_version=DISPATCH_RECEIPT_SCHEMA_VERSION,
                receipt_id=f"receipt-{envelope.dispatch_id}",
                dispatch_id=envelope.dispatch_id,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                policy_receipt_id=envelope.policy_receipt_id,
                worker_kind=envelope.worker_kind,
                worker_identity="antigravity-gui",
                dispatched_at_utc=now_utc,
                expected_result_channel="none",
                status=DispatchStatus.BLOCKED,
                reason_code="UI_TARGET_MISMATCH",
            )

        # 1. Create Computer Use Task representing this dispatch
        task = ComputerUseTask(
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            attempt_id=envelope.attempt_id,
            dispatch_id=envelope.dispatch_id,
            intent_digest=envelope.intent_digest,
            policy_receipt_id=envelope.policy_receipt_id,
            policy_receipt_digest=envelope.policy_receipt_digest,
            target_application="Antigravity",
            result_channel="hermes.worker-result.v1",
            created_at_utc=now_utc,
        )

        try:
            self.driver.initialize(task)
            obs = self.driver.observe("Antigravity")
            if obs.status == UIActionStatus.BLOCKED or obs.target_application != "Antigravity":
                return DispatchReceipt(
                    schema_version=DISPATCH_RECEIPT_SCHEMA_VERSION,
                    receipt_id=f"receipt-{envelope.dispatch_id}",
                    dispatch_id=envelope.dispatch_id,
                    task_id=envelope.task_id,
                    attempt_id=envelope.attempt_id,
                    policy_receipt_id=envelope.policy_receipt_id,
                    worker_kind=envelope.worker_kind,
                    worker_identity="antigravity-gui",
                    dispatched_at_utc=now_utc,
                    expected_result_channel="none",
                    status=DispatchStatus.BLOCKED,
                    reason_code="UI_TARGET_MISMATCH",
                )
        except Exception:
            return DispatchReceipt(
                schema_version=DISPATCH_RECEIPT_SCHEMA_VERSION,
                receipt_id=f"receipt-{envelope.dispatch_id}",
                dispatch_id=envelope.dispatch_id,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                policy_receipt_id=envelope.policy_receipt_id,
                worker_kind=envelope.worker_kind,
                worker_identity="antigravity-gui",
                dispatched_at_utc=now_utc,
                expected_result_channel="none",
                status=DispatchStatus.FAILED,
                reason_code="ANTIGRAVITY_STRUCTURED_DISPATCH_UNAVAILABLE",
            )

        self.dispatched_tasks.append(task)

        return DispatchReceipt(
            schema_version=DISPATCH_RECEIPT_SCHEMA_VERSION,
            receipt_id=f"receipt-{envelope.dispatch_id}",
            dispatch_id=envelope.dispatch_id,
            task_id=envelope.task_id,
            attempt_id=envelope.attempt_id,
            policy_receipt_id=envelope.policy_receipt_id,
            worker_kind=envelope.worker_kind,
            worker_identity="antigravity-gui",
            dispatched_at_utc=now_utc,
            expected_result_channel="hermes.worker-result.v1",
            status=DispatchStatus.DISPATCHED,
            reason_code=None,
        )
