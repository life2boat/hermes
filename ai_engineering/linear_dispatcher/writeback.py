"""Linear writeback and persistence verification service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    LinearTask,
)


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    task_id: str
    execution_status: str
    claim_owner: str
    branch: str
    pr_number: int
    pr_url: str
    base_sha: str
    head_sha: str
    validation_status: str
    ci_status: str
    ci_head_sha: str
    sha_match: str
    production_changes: int = 0
    db_changes: int = 0
    qdrant_changes: int = 0
    deployment: int = 0
    merge: int = 0
    execution_id: str = ""

    def to_markdown(self) -> str:
        exec_id = self.execution_id or self.task_id
        return f"""### Hermes Autonomous Loop Execution Evidence

- **EXECUTION_ID:** `{exec_id}`
- **EXECUTION_STATUS:** `{self.execution_status}`
- **CLAIM_OWNER:** `{self.claim_owner}`
- **BRANCH:** `{self.branch}`
- **PR:** [#{self.pr_number}]({self.pr_url})
- **BASE_SHA:** `{self.base_sha}`
- **HEAD_SHA:** `{self.head_sha}`
- **VALIDATION:** `{self.validation_status}`
- **CI:** `{self.ci_status}`
- **CI_HEAD_SHA:** `{self.ci_head_sha}`
- **SHA_MATCH:** `{self.sha_match}`
- **PRODUCTION_CHANGES:** `{self.production_changes}`
- **DB_CHANGES:** `{self.db_changes}`
- **QDRANT_CHANGES:** `{self.qdrant_changes}`
- **DEPLOYMENT:** `{self.deployment}`
- **MERGE:** `{self.merge}`
"""


@runtime_checkable
class ILinearClient(Protocol):
    def add_comment(self, issue_id: str, body: str) -> bool:
        ...

    def update_issue(self, issue_id: str, fields: dict[str, Any]) -> bool:
        ...

    def get_issue(self, issue_id: str) -> LinearTask | None:
        ...

    def get_issue_comments(self, issue_id: str) -> list[str]:
        ...


class WritebackService:
    """Manages writing execution evidence back to Linear and verifying persistence."""

    def __init__(self, linear_client: ILinearClient) -> None:
        self._client = linear_client

    def writeback_and_verify(
        self,
        task: LinearTask,
        evidence: ExecutionEvidence,
    ) -> tuple[bool, BlockReasonCode | None]:
        """Write evidence comment and verify by re-reading from Linear."""
        md_text = evidence.to_markdown()

        # Step 0: Idempotency check with structured receipt verification
        comments = self._client.get_issue_comments(task.id)
        refetched_task = self._client.get_issue(task.id)
        exec_id = getattr(evidence, "execution_id", "") or evidence.task_id
        receipt_marker = f"EXECUTION_ID:** `{exec_id}`"
        sha_marker = f"HEAD_SHA:** `{evidence.head_sha}`"
        already_has_receipt = (
            any(
                (receipt_marker in c or sha_marker in c or (evidence.head_sha in c and "evidence" in c.lower()))
                for c in comments
            )
            or (
                refetched_task
                and (
                    receipt_marker in refetched_task.description
                    or sha_marker in refetched_task.description
                    or (evidence.head_sha in refetched_task.description and "evidence" in refetched_task.description.lower())
                )
            )
            if refetched_task
            else False
        )
        if refetched_task and already_has_receipt:
            if refetched_task.state.lower() in ("done", "completed", "closed"):
                return True, None
            # Receipt present but not terminal: transition to Done and reread
            ok = self._client.update_issue(task.id, {"state": "Done"})
            if not ok:
                return False, BlockReasonCode.REREAD_VERIFICATION_FAILED
            refetched_task = self._client.get_issue(task.id)
            if (
                not refetched_task
                or refetched_task.state.lower() not in ("done", "completed", "closed")
            ):
                return False, BlockReasonCode.REREAD_VERIFICATION_FAILED
            return True, None

        # Step 1: Add comment with evidence
        ok = self._client.add_comment(task.id, md_text)
        if not ok:
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        # Step 2: Also update description to ensure evidence is prominently visible
        new_desc = f"{task.description}\n\n{md_text}"
        ok_desc = self._client.update_issue(task.id, {"description": new_desc})
        if not ok_desc:
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        # Step 3: Re-read issue and comments to confirm persistence
        refetched_task = self._client.get_issue(task.id)
        if not refetched_task:
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        comments = self._client.get_issue_comments(task.id)
        persisted = any(
            (receipt_marker in c or sha_marker in c) and "Execution Evidence" in c
            for c in comments
        ) or (
            (
                receipt_marker in refetched_task.description
                or sha_marker in refetched_task.description
            )
            and "Execution Evidence" in refetched_task.description
        )
        if not persisted:
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        # Step 4: After persistence is strictly confirmed, transition issue to Done and verify
        ok_done = self._client.update_issue(task.id, {"state": "Done"})
        if not ok_done:
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        refetched_final = self._client.get_issue(task.id)
        if (
            not refetched_final
            or refetched_final.state.lower() not in ("done", "completed", "closed")
        ):
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        return True, None
