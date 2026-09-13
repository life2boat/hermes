"""Pull Request Provider contract and implementations for Hermes Autonomous Supervisor (Task 7.8).

Defines:
- PullRequestIdentity (hermes.pull-request-identity.v1)
- PullRequestReceipt (hermes.pull-request-receipt.v1)
- MergeReceipt (hermes.merge-receipt.v1)
- Protocol PullRequestProvider
- GitHubPullRequestProvider (real REST API provider, strictly no --admin)
- FakeGitHubPullRequestBackend (deterministic offline test provider)
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

PULL_REQUEST_IDENTITY_SCHEMA_VERSION = "hermes.pull-request-identity.v1"
PULL_REQUEST_RECEIPT_SCHEMA_VERSION = "hermes.pull-request-receipt.v1"
MERGE_RECEIPT_SCHEMA_VERSION = "hermes.merge-receipt.v1"

_SECRET_PATTERN = re.compile(
    r"(ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9_.-]+|token\s+[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)


def scrub_secrets(text: str) -> str:
    """Redact authorization tokens and secret tokens from strings."""
    if not text:
        return ""
    return _SECRET_PATTERN.sub("[REDACTED]", str(text))


class PRProviderError(Exception):
    """Base exception for PR provider operations."""
    code: str = "PR_PROVIDER_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        safe_message = scrub_secrets(message)
        super().__init__(safe_message)
        if code:
            self.code = code


class PRProviderUnavailableError(PRProviderError):
    code: str = "PR_PROVIDER_UNAVAILABLE"


class PRNotFoundError(PRProviderError):
    code: str = "PR_NOT_FOUND"


class PRHeadMismatchError(PRProviderError):
    code: str = "PR_HEAD_MISMATCH"


class PRConflictError(PRProviderError):
    code: str = "PR_CONFLICT"


class PRMergeFailedError(PRProviderError):
    code: str = "PR_MERGE_FAILED"


class PRValidationFailedError(PRProviderError):
    code: str = "PR_VALIDATION_FAILED"


class PRAuthorityDeniedError(PRProviderError):
    code: str = "PR_AUTHORITY_DENIED"


@dataclass(frozen=True, slots=True)
class PullRequestIdentity:
    schema_version: str
    repository: str
    pr_number: int
    pr_node_id: str
    head_branch: str
    head_sha: str
    base_branch: str
    base_sha: str
    title: str
    body: str
    is_draft: bool
    state: str
    mergeable: bool | None
    mergeable_state: str
    merged: bool
    merged_at: str | None
    merge_commit_sha: str | None
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True, slots=True)
class PullRequestReceipt:
    schema_version: str
    receipt_id: str
    run_id: str
    task_id: str
    policy_receipt_id: str
    repository: str
    pr_number: int
    head_branch: str
    head_sha: str
    base_branch: str
    base_sha: str
    action: str
    created_at_utc: str
    receipt_digest: str


@dataclass(frozen=True, slots=True)
class MergeReceipt:
    schema_version: str
    receipt_id: str
    run_id: str
    task_id: str
    policy_receipt_id: str
    repository: str
    pr_number: int
    head_sha: str
    base_sha: str
    merged_commit_sha: str
    merge_method: str
    merged_at_utc: str
    receipt_digest: str


def compute_pr_receipt_digest(data: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(data), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_merge_receipt_digest(data: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(data), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@runtime_checkable
class PullRequestProvider(Protocol):
    async def get_pr(self, repository: str, pr_number: int) -> PullRequestIdentity:
        ...

    async def find_existing_pr(
        self, repository: str, head_branch: str, base_branch: str
    ) -> PullRequestIdentity | None:
        ...

    async def create_pr(
        self,
        repository: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool = False,
        head_sha: str | None = None,
    ) -> PullRequestIdentity:
        ...

    async def merge_pr(
        self,
        repository: str,
        pr_number: int,
        exact_head_sha: str,
        commit_title: str | None = None,
        commit_message: str | None = None,
        merge_method: str = "squash",
    ) -> MergeReceipt:
        ...


class FakeGitHubPullRequestBackend(PullRequestProvider):
    """In-memory fake implementation of GitHub PR API for deterministic offline testing."""

    def __init__(self) -> None:
        self._prs: dict[int, PullRequestIdentity] = {}
        self._next_pr_number: int = 100
        self.latest_receipt: PullRequestReceipt | None = None
        self.latest_merge_receipt: MergeReceipt | None = None

    async def get_pr(self, repository: str, pr_number: int) -> PullRequestIdentity:
        if pr_number not in self._prs:
            raise PRNotFoundError(f"PR #{pr_number} not found in {repository}", code="PR_NOT_FOUND")
        return self._prs[pr_number]

    async def find_existing_pr(
        self, repository: str, head_branch: str, base_branch: str
    ) -> PullRequestIdentity | None:
        clean_head = head_branch.split(":")[-1]
        for pr in self._prs.values():
            if pr.repository == repository and pr.state == "open":
                pr_clean_head = pr.head_branch.split(":")[-1]
                if pr_clean_head == clean_head and pr.base_branch == base_branch:
                    return pr
        return None

    async def create_pr(
        self,
        repository: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool = False,
        head_sha: str | None = None,
    ) -> PullRequestIdentity:
        existing = await self.find_existing_pr(repository, head_branch, base_branch)
        if existing:
            return existing

        pr_num = self._next_pr_number
        self._next_pr_number += 1
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if head_sha is None:
            head_sha = hashlib.sha256(f"fake-head-{repository}-{head_branch}-{pr_num}".encode()).hexdigest()[:40]
        base_sha = hashlib.sha256(f"fake-base-{repository}-{base_branch}".encode()).hexdigest()[:40]

        pr = PullRequestIdentity(
            schema_version=PULL_REQUEST_IDENTITY_SCHEMA_VERSION,
            repository=repository,
            pr_number=pr_num,
            pr_node_id=f"PR_{pr_num}",
            head_branch=head_branch,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
            title=title,
            body=body,
            is_draft=draft,
            state="open",
            mergeable=True,
            mergeable_state="clean",
            merged=False,
            merged_at=None,
            merge_commit_sha=None,
            created_at_utc=now,
            updated_at_utc=now,
        )
        self._prs[pr_num] = pr
        return pr

    async def merge_pr(
        self,
        repository: str,
        pr_number: int,
        exact_head_sha: str,
        commit_title: str | None = None,
        commit_message: str | None = None,
        merge_method: str = "squash",
    ) -> MergeReceipt:
        pr = await self.get_pr(repository, pr_number)

        if pr.state != "open":
            raise PRMergeFailedError(f"PR #{pr_number} is {pr.state}, cannot merge", code="PR_NOT_OPEN")

        if pr.is_draft:
            raise PRMergeFailedError(f"PR #{pr_number} is draft, cannot merge", code="PR_IS_DRAFT")

        if pr.mergeable is False or pr.mergeable_state == "dirty":
            raise PRConflictError(f"PR #{pr_number} has merge conflicts", code="PR_CONFLICT")

        if exact_head_sha != pr.head_sha:
            raise PRHeadMismatchError(
                f"Head SHA mismatch for PR #{pr_number}: expected {exact_head_sha}, found {pr.head_sha}",
                code="PR_HEAD_MISMATCH",
            )

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        merged_sha = hashlib.sha256(f"merge-{pr_number}-{exact_head_sha}-{now}".encode()).hexdigest()[:40]

        updated_pr = PullRequestIdentity(
            schema_version=pr.schema_version,
            repository=pr.repository,
            pr_number=pr.pr_number,
            pr_node_id=pr.pr_node_id,
            head_branch=pr.head_branch,
            head_sha=pr.head_sha,
            base_branch=pr.base_branch,
            base_sha=pr.base_sha,
            title=pr.title,
            body=pr.body,
            is_draft=pr.is_draft,
            state="closed",
            mergeable=None,
            mergeable_state="clean",
            merged=True,
            merged_at=now,
            merge_commit_sha=merged_sha,
            created_at_utc=pr.created_at_utc,
            updated_at_utc=now,
        )
        self._prs[pr_number] = updated_pr

        receipt_data = {
            "schema_version": MERGE_RECEIPT_SCHEMA_VERSION,
            "run_id": "",
            "task_id": "",
            "policy_receipt_id": "",
            "repository": repository,
            "pr_number": pr_number,
            "head_sha": exact_head_sha,
            "base_sha": pr.base_sha,
            "merged_commit_sha": merged_sha,
            "merge_method": merge_method,
            "merged_at_utc": now,
        }
        r_dg = compute_merge_receipt_digest(receipt_data)
        receipt = MergeReceipt(
            schema_version=MERGE_RECEIPT_SCHEMA_VERSION,
            receipt_id=f"merge-rcpt-{pr_number}",
            run_id="",
            task_id="",
            policy_receipt_id="",
            repository=repository,
            pr_number=pr_number,
            head_sha=exact_head_sha,
            base_sha=pr.base_sha,
            merged_commit_sha=merged_sha,
            merge_method=merge_method,
            merged_at_utc=now,
            receipt_digest=r_dg,
        )
        self.latest_merge_receipt = receipt
        return receipt

    def set_pr(self, pr: PullRequestIdentity) -> None:
        self._prs[pr.pr_number] = pr

    def simulate_head_change(self, pr_number: int, new_sha: str) -> None:
        pr = self._prs[pr_number]
        self._prs[pr_number] = PullRequestIdentity(
            schema_version=pr.schema_version,
            repository=pr.repository,
            pr_number=pr.pr_number,
            pr_node_id=pr.pr_node_id,
            head_branch=pr.head_branch,
            head_sha=new_sha,
            base_branch=pr.base_branch,
            base_sha=pr.base_sha,
            title=pr.title,
            body=pr.body,
            is_draft=pr.is_draft,
            state=pr.state,
            mergeable=pr.mergeable,
            mergeable_state=pr.mergeable_state,
            merged=pr.merged,
            merged_at=pr.merged_at,
            merge_commit_sha=pr.merge_commit_sha,
            created_at_utc=pr.created_at_utc,
            updated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    def simulate_conflict(self, pr_number: int) -> None:
        pr = self._prs[pr_number]
        self._prs[pr_number] = PullRequestIdentity(
            schema_version=pr.schema_version,
            repository=pr.repository,
            pr_number=pr.pr_number,
            pr_node_id=pr.pr_node_id,
            head_branch=pr.head_branch,
            head_sha=pr.head_sha,
            base_branch=pr.base_branch,
            base_sha=pr.base_sha,
            title=pr.title,
            body=pr.body,
            is_draft=pr.is_draft,
            state=pr.state,
            mergeable=False,
            mergeable_state="dirty",
            merged=pr.merged,
            merged_at=pr.merged_at,
            merge_commit_sha=pr.merge_commit_sha,
            created_at_utc=pr.created_at_utc,
            updated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    def simulate_main_advanced(self, pr_number: int, new_base_sha: str) -> None:
        pr = self._prs[pr_number]
        self._prs[pr_number] = PullRequestIdentity(
            schema_version=pr.schema_version,
            repository=pr.repository,
            pr_number=pr.pr_number,
            pr_node_id=pr.pr_node_id,
            head_branch=pr.head_branch,
            head_sha=pr.head_sha,
            base_branch=pr.base_branch,
            base_sha=new_base_sha,
            title=pr.title,
            body=pr.body,
            is_draft=pr.is_draft,
            state=pr.state,
            mergeable=pr.mergeable,
            mergeable_state=pr.mergeable_state,
            merged=pr.merged,
            merged_at=pr.merged_at,
            merge_commit_sha=pr.merge_commit_sha,
            created_at_utc=pr.created_at_utc,
            updated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )


class GitHubPullRequestProvider(PullRequestProvider):
    """Real GitHub Pull Request Provider using REST API.
    Strictly NO --admin in merge calls!
    All exceptions and logs are secret-scrubbed.
    """

    def __init__(
        self,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.latest_receipt: PullRequestReceipt | None = None
        self.latest_merge_receipt: MergeReceipt | None = None

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-autonomous-supervisor/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _sync_request(self, method: str, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = self._get_headers()
        body = json.dumps(data).encode("utf-8") if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8")) if resp_bytes else {}
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            safe_err = scrub_secrets(f"HTTP {e.code} {e.reason}: {err_body}")

            if e.code == 404:
                raise PRNotFoundError(safe_err, code="PR_NOT_FOUND") from None
            elif e.code == 405:
                raise PRConflictError(safe_err, code="PR_CONFLICT") from None
            elif e.code == 409:
                raise PRHeadMismatchError(safe_err, code="PR_HEAD_MISMATCH") from None
            else:
                raise PRProviderError(safe_err, code=f"HTTP_{e.code}") from None
        except Exception as e:
            safe_err = scrub_secrets(f"Network error: {type(e).__name__}: {str(e)}")
            raise PRProviderUnavailableError(safe_err, code="PR_PROVIDER_UNAVAILABLE") from None

    def _parse_pr(self, data: dict[str, Any], repository: str) -> PullRequestIdentity:
        head_data = data.get("head", {})
        base_data = data.get("base", {})
        return PullRequestIdentity(
            schema_version=PULL_REQUEST_IDENTITY_SCHEMA_VERSION,
            repository=repository,
            pr_number=data["number"],
            pr_node_id=data.get("node_id", ""),
            head_branch=head_data.get("ref", ""),
            head_sha=head_data.get("sha", ""),
            base_branch=base_data.get("ref", ""),
            base_sha=base_data.get("sha", ""),
            title=data.get("title", ""),
            body=data.get("body") or "",
            is_draft=bool(data.get("draft", False)),
            state=data.get("state", "open"),
            mergeable=data.get("mergeable"),
            mergeable_state=data.get("mergeable_state") or "unknown",
            merged=bool(data.get("merged", False)),
            merged_at=data.get("merged_at"),
            merge_commit_sha=data.get("merge_commit_sha"),
            created_at_utc=data.get("created_at") or "",
            updated_at_utc=data.get("updated_at") or "",
        )

    async def get_pr(self, repository: str, pr_number: int) -> PullRequestIdentity:
        url = f"{self.base_url}/repos/{repository}/pulls/{pr_number}"
        data = await asyncio.to_thread(self._sync_request, "GET", url)
        return self._parse_pr(data, repository)

    async def find_existing_pr(
        self, repository: str, head_branch: str, base_branch: str
    ) -> PullRequestIdentity | None:
        clean_head = head_branch.split(":")[-1]
        url = f"{self.base_url}/repos/{repository}/pulls?state=open&head={clean_head}&base={base_branch}"
        prs = await asyncio.to_thread(self._sync_request, "GET", url)
        if isinstance(prs, list) and prs:
            return self._parse_pr(prs[0], repository)
        return None

    async def create_pr(
        self,
        repository: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool = False,
        head_sha: str | None = None,
    ) -> PullRequestIdentity:
        url = f"{self.base_url}/repos/{repository}/pulls"
        payload = {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
            "draft": draft,
        }
        data = await asyncio.to_thread(self._sync_request, "POST", url, payload)
        return self._parse_pr(data, repository)

    async def merge_pr(
        self,
        repository: str,
        pr_number: int,
        exact_head_sha: str,
        commit_title: str | None = None,
        commit_message: str | None = None,
        merge_method: str = "squash",
    ) -> MergeReceipt:
        url = f"{self.base_url}/repos/{repository}/pulls/{pr_number}/merge"
        payload: dict[str, Any] = {
            "sha": exact_head_sha,
            "merge_method": merge_method,
        }
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message

        resp = await asyncio.to_thread(self._sync_request, "PUT", url, payload)

        if not resp.get("merged"):
            raise PRMergeFailedError(
                scrub_secrets(f"GitHub merge failed: {resp.get('message', 'PR not merged')}"),
                code="PR_MERGE_FAILED",
            )

        merged_sha = resp.get("sha", "")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        receipt_data = {
            "schema_version": MERGE_RECEIPT_SCHEMA_VERSION,
            "run_id": "",
            "task_id": "",
            "policy_receipt_id": "",
            "repository": repository,
            "pr_number": pr_number,
            "head_sha": exact_head_sha,
            "base_sha": "",
            "merged_commit_sha": merged_sha,
            "merge_method": merge_method,
            "merged_at_utc": now,
        }
        r_dg = compute_merge_receipt_digest(receipt_data)
        receipt = MergeReceipt(
            schema_version=MERGE_RECEIPT_SCHEMA_VERSION,
            receipt_id=f"merge-rcpt-{pr_number}",
            run_id="",
            task_id="",
            policy_receipt_id="",
            repository=repository,
            pr_number=pr_number,
            head_sha=exact_head_sha,
            base_sha="",
            merged_commit_sha=merged_sha,
            merge_method=merge_method,
            merged_at_utc=now,
            receipt_digest=r_dg,
        )
        self.latest_merge_receipt = receipt
        return receipt
