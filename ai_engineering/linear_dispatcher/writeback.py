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

    @staticmethod
    def is_authentic_receipt(
        text: str | None,
        head_sha: str | None = None,
        exec_id: str | None = None,
    ) -> bool:
        if not text:
            return False
        header_marker = "### Hermes Autonomous Loop Execution Evidence"
        if header_marker not in text:
            return False

        # Parse key-value bullet points
        fields: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- **") and ":** `" in line and line.endswith("`"):
                parts = line[4:-1].split(":** `", 1)
                if len(parts) == 2:
                    fields[parts[0]] = parts[1]

        # Require successful status and reject failure markers
        if fields.get("EXECUTION_STATUS") != "PASS":
            return False
        if fields.get("VALIDATION") != "PASS":
            return False
        if fields.get("CI") != "PASS":
            return False
        if fields.get("SHA_MATCH") != "YES":
            return False

        # Verify boundary constraints: 0 mutations, 0 deployments
        for boundary_key in ("PRODUCTION_CHANGES", "DB_CHANGES", "QDRANT_CHANGES", "DEPLOYMENT", "MERGE"):
            if fields.get(boundary_key) != "0":
                return False

        # Require authentic non-empty execution ID matching expectation
        receipt_exec_id = fields.get("EXECUTION_ID")
        if not receipt_exec_id:
            return False
        if exec_id and receipt_exec_id != exec_id:
            return False

        # Require authentic head SHA matching expectation
        receipt_head_sha = fields.get("HEAD_SHA")
        if not receipt_head_sha:
            return False
        if head_sha and receipt_head_sha != head_sha:
            return False

        # Reject contradictory failure/blocked indications in text
        lower_text = text.lower()
        if any(bad in lower_text for bad in ("execution_status:** `fail", "execution_status:** `blocked", "status: fail", "status: blocked")):
            return False

        return True

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

        already_has_receipt = (
            any(self.is_authentic_receipt(c, evidence.head_sha, exec_id) for c in comments)
            or (refetched_task and self.is_authentic_receipt(refetched_task.description, evidence.head_sha, exec_id))
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
        persisted = (
            any(self.is_authentic_receipt(c, evidence.head_sha, exec_id) for c in comments)
            or self.is_authentic_receipt(refetched_task.description, evidence.head_sha, exec_id)
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
