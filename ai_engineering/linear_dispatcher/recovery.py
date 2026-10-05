from dataclasses import dataclass
from typing import Optional
from ai_engineering.linear_dispatcher.contracts import TaskState

@dataclass
class RecoveryContext:
    task_id: str
    state: TaskState
    claim_owner: str
    claim_token: str
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
