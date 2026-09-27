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

    def to_markdown(self) -> str:
        return f"""### Hermes Autonomous Loop Execution Evidence

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

        # Step 1: Add comment with evidence
        ok = self._client.add_comment(task.id, md_text)
        if not ok:
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        # Step 2: Also update description to ensure evidence is prominently visible
        new_desc = f"{task.description}\n\n{md_text}"
        self._client.update_issue(task.id, {"description": new_desc})

        # Step 3: Re-read issue and comments to confirm persistence
        refetched_task = self._client.get_issue(task.id)
        if not refetched_task:
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        comments = self._client.get_issue_comments(task.id)
        # Verify evidence signature exists in comments or description
        persisted = any(evidence.head_sha in c for c in comments) or (evidence.head_sha in refetched_task.description)
        if not persisted:
            return False, BlockReasonCode.REREAD_VERIFICATION_FAILED

        # Step 4: After persistence is strictly confirmed, transition issue to Done
        self._client.update_issue(task.id, {"state": "Done"})
        return True, None
