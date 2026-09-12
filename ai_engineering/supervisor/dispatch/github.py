from typing import Protocol, runtime_checkable
from dataclasses import dataclass
try:
    from enum import StrEnum
except ImportError:
    from enum import Enum
    class StrEnum(str, Enum):
        pass

class CIState(StrEnum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"

@dataclass(frozen=True, slots=True)
class PRState:
    pr_number: int
    head_branch: str
    base_branch: str
    head_sha: str
    mergeable: bool
    merge_state_status: str

@dataclass(frozen=True, slots=True)
class CIDetailedState:
    head_sha: str
    overall_state: CIState
    check_conclusions: dict[str, str]

@runtime_checkable
class GitHubProvider(Protocol):
    def get_pr_state(self, repository: str, pr_number: int) -> PRState | None:
        ...

    def get_ci_state(self, repository: str, pr_number: int, head_sha: str) -> CIDetailedState | None:
        ...

    def merge_pr(self, repository: str, pr_number: int, merge_method: str = "squash") -> str | None:
        ...

class FakeGitHubProvider(GitHubProvider):
    def __init__(self):
        self.prs: dict[int, PRState] = {}
        self.cis: dict[tuple[int, str], CIDetailedState] = {}

    def add_pr(self, repository: str, pr_number: int, pr: PRState):
        self.prs[pr_number] = pr

    def add_ci(self, repository: str, pr_number: int, head_sha: str, ci: CIDetailedState):
        self.cis[(pr_number, head_sha)] = ci

    def get_pr_state(self, repository: str, pr_number: int) -> PRState | None:
        return self.prs.get(pr_number)

    def get_ci_state(self, repository: str, pr_number: int, head_sha: str) -> CIDetailedState | None:
        return self.cis.get((pr_number, head_sha))

    def merge_pr(self, repository: str, pr_number: int, merge_method: str = "squash") -> str | None:
        pr = self.prs.get(pr_number)
        if pr is None:
            return None
        import hashlib
        return hashlib.sha256(f"merge-{repository}-{pr_number}-{pr.head_sha}-{merge_method}".encode("utf-8")).hexdigest()[:40]
