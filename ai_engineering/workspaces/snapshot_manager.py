"""Snapshot management, normalization, read-only capture, and in-memory registry."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import threading
from typing import Callable, Sequence
import uuid

from ai_engineering.execution.run_contracts import AgentRunIdentity, RunState
from ai_engineering.workspaces.diff_artifacts import compute_diff_digest, generate_diff_artifact
from ai_engineering.workspaces.snapshot_contracts import (
    DiffArtifact,
    SnapshotBlockingReason,
    SnapshotPhase,
    WorkspaceSnapshot,
    WorkspaceSnapshotError,
    validate_repository_relative_path,
)
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity, WorktreeLease

_PHASE_ORDER: dict[SnapshotPhase, int] = {
    SnapshotPhase.PRE_EXECUTION: 0,
    SnapshotPhase.POST_EXECUTION: 1,
    SnapshotPhase.POST_VALIDATION: 2,
    SnapshotPhase.FINAL: 3,
}


def normalize_git_status(status_lines: Sequence[str]) -> tuple[str, ...]:
    """Normalize and deterministically sort git porcelain v1 status lines."""
    normalized: list[str] = []
    for line in status_lines:
        if not line or not line.strip():
            continue
        # Porcelain v1 lines start with 2 characters of status code, then a space, then the path.
        if len(line) >= 3 and line[2] == " ":
            code = line[:2]
            path_part = line[3:].strip()
            if " -> " in path_part:
                orig, target = path_part.split(" -> ", 1)
                orig_clean = validate_repository_relative_path(orig.strip('"'))
                target_clean = validate_repository_relative_path(target.strip('"'))
                normalized.append(f"{code} {orig_clean} -> {target_clean}")
            else:
                clean_path = validate_repository_relative_path(path_part.strip('"'))
                normalized.append(f"{code} {clean_path}")
        else:
            parts = line.strip().split(maxsplit=1)
            if len(parts) >= 2:
                code, path_part = parts[0], parts[1]
                clean_path = validate_repository_relative_path(path_part.strip('"'))
                normalized.append(f"{code} {clean_path}")
            else:
                normalized.append(line.strip())
    return tuple(sorted(set(normalized)))


class SnapshotRegistry:
    """In-memory thread-safe registry for immutable workspace snapshots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[str, WorkspaceSnapshot] = {}
        self._by_workspace: dict[str, list[WorkspaceSnapshot]] = {}

    def record(self, snapshot: WorkspaceSnapshot) -> None:
        """Record an immutable WorkspaceSnapshot with phase progression checks."""
        if not isinstance(snapshot, WorkspaceSnapshot):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                "Expected WorkspaceSnapshot instance",
            )

        with self._lock:
            # Idempotency / Collision check
            if snapshot.snapshot_id in self._snapshots:
                existing = self._snapshots[snapshot.snapshot_id]
                if existing == snapshot:
                    return  # Exact idempotent registration
                raise WorkspaceSnapshotError(
                    SnapshotBlockingReason.WORKSPACE_SNAPSHOT_COLLISION.value,
                    f"Snapshot ID collision with divergent content: {snapshot.snapshot_id}",
                )

            # Phase progression check for the workspace
            workspace_history = self._by_workspace.get(snapshot.workspace_id, [])
            if workspace_history:
                last_snapshot = workspace_history[-1]
                last_phase_order = _PHASE_ORDER[last_snapshot.phase]
                current_phase_order = _PHASE_ORDER[snapshot.phase]

                # Disallow regression (e.g. POST_EXECUTION after FINAL)
                if current_phase_order <= last_phase_order:
                    raise WorkspaceSnapshotError(
                        SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PHASE_INVALID.value,
                        f"Invalid snapshot phase progression for workspace {snapshot.workspace_id}: "
                        f"{last_snapshot.phase.value} (order {last_phase_order}) -> {snapshot.phase.value} (order {current_phase_order})",
                    )

                # Disallow recording any snapshot after FINAL (no resurrection)
                if last_snapshot.phase == SnapshotPhase.FINAL:
                    raise WorkspaceSnapshotError(
                        SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PHASE_INVALID.value,
                        f"Workspace {snapshot.workspace_id} is already in FINAL phase, cannot record new snapshot",
                    )

            # Record
            self._snapshots[snapshot.snapshot_id] = snapshot
            if snapshot.workspace_id not in self._by_workspace:
                self._by_workspace[snapshot.workspace_id] = []
            self._by_workspace[snapshot.workspace_id].append(snapshot)

    def get(self, snapshot_id: str) -> WorkspaceSnapshot | None:
        """Retrieve snapshot by snapshot_id."""
        with self._lock:
            return self._snapshots.get(snapshot_id)

    def list_for_workspace(self, workspace_id: str) -> tuple[WorkspaceSnapshot, ...]:
        """List all snapshots for a workspace in deterministic order."""
        with self._lock:
            history = self._by_workspace.get(workspace_id, [])
            return tuple(history)

    def latest_for_phase(self, workspace_id: str, phase: SnapshotPhase) -> WorkspaceSnapshot | None:
        """Retrieve the latest snapshot recorded for a specific phase in a workspace."""
        with self._lock:
            history = self._by_workspace.get(workspace_id, [])
            for snap in reversed(history):
                if snap.phase == phase:
                    return snap
            return None


class WorkspaceSnapshotManager:
    """Manager for capturing read-only WorkspaceSnapshots and DiffArtifacts."""

    def __init__(
        self,
        canonical_repo_path: str | Path | None = None,
        registry: SnapshotRegistry | None = None,
        git_executor: Callable[[list[str], str], tuple[int, str, str]] | None = None,
    ) -> None:
        self.canonical_repo_path = Path(canonical_repo_path).resolve() if canonical_repo_path else None
        self.registry = registry or SnapshotRegistry()
        self.git_executor = git_executor or self._default_git_executor

    @staticmethod
    def _default_git_executor(cmd: list[str], cwd: str) -> tuple[int, str, str]:
        res = subprocess.run(
            ["git"] + cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode, res.stdout, res.stderr

    def capture_snapshot(
        self,
        workspace: WorkspaceIdentity,
        run: AgentRunIdentity,
        phase: SnapshotPhase,
        lease: WorktreeLease | None = None,
        run_state: RunState = RunState.LIVE,
        snapshot_id: str | None = None,
        now: str | None = None,
    ) -> WorkspaceSnapshot:
        """Capture a point-in-time WorkspaceSnapshot using read-only Git operations."""
        # 1. Canonical checkout protection
        wt_path = Path(workspace.worktree_path).resolve()
        if self.canonical_repo_path is not None:
            if wt_path == self.canonical_repo_path:
                raise WorkspaceSnapshotError(
                    SnapshotBlockingReason.WORKSPACE_SNAPSHOT_CANONICAL_FORBIDDEN.value,
                    f"Candidate workspace snapshot forbidden on canonical checkout: {workspace.worktree_path}",
                )

        # 2. Identity binding validations
        if workspace.task_id != run.task_id:
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Workspace task_id {workspace.task_id} does not match run task_id {run.task_id}",
            )
        if workspace.workspace_id != run.workspace_id:
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Workspace workspace_id {workspace.workspace_id} does not match run workspace_id {run.workspace_id}",
            )
        if workspace.candidate_id is not None and run.candidate_id is not None and workspace.candidate_id != run.candidate_id:
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Workspace candidate_id {workspace.candidate_id} does not match run candidate_id {run.candidate_id}",
            )

        # 3. Worktree Lease binding (if lease provided)
        if lease is not None:
            if lease.workspace_id != workspace.workspace_id:
                raise WorkspaceSnapshotError(
                    SnapshotBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                    f"Lease workspace_id {lease.workspace_id} != {workspace.workspace_id}",
                )
            if lease.owner_run_id != run.run_id:
                raise WorkspaceSnapshotError(
                    SnapshotBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                    f"Lease owner_run_id {lease.owner_run_id} != {run.run_id}",
                )
            if lease.task_id != run.task_id:
                raise WorkspaceSnapshotError(
                    SnapshotBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                    f"Lease task_id {lease.task_id} != {run.task_id}",
                )
            if not lease.is_active():
                raise WorkspaceSnapshotError(
                    SnapshotBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                    f"Lease is not active: {lease.state.value}",
                )

        # 4. Stale run fencing
        if run_state != RunState.LIVE:
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.STALE_RUN_EVENT.value,
                f"Cannot capture snapshot for non-live run {run.run_id} (state={run_state.value})",
            )

        # 5. Read-only Git inspection
        cwd_str = str(wt_path)

        # Head SHA
        rc, head_out, err = self.git_executor(["rev-parse", "HEAD"], cwd_str)
        if rc != 0:
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Failed to get HEAD in {cwd_str}: {err}",
            )
        head_sha = head_out.strip()

        # Git status
        rc, status_out, _ = self.git_executor(["status", "--porcelain=v1"], cwd_str)
        status_lines = [line for line in status_out.splitlines() if line.strip()]
        norm_status = normalize_git_status(status_lines)

        # Git diff binary
        rc, diff_out, _ = self.git_executor(["diff", "--binary", "--no-ext-diff", f"{workspace.base_sha}...HEAD"], cwd_str)
        diff_digest = compute_diff_digest(diff_out)

        # Git diff stat
        rc, stat_out, _ = self.git_executor(["diff", "--stat", f"{workspace.base_sha}...HEAD"], cwd_str)
        diff_stat = stat_out.strip()

        # Changed paths from diff
        rc, name_out, _ = self.git_executor(["diff", "--name-only", f"{workspace.base_sha}...HEAD"], cwd_str)
        changed_from_diff = [line.strip() for line in name_out.splitlines() if line.strip()]

        # Changed paths from untracked / uncommitted in status
        changed_from_status: list[str] = []
        for line in status_lines:
            if len(line) >= 3 and line[2] == " ":
                path_part = line[3:].strip().strip('"')
            else:
                parts = line.strip().split(maxsplit=1)
                path_part = parts[1].strip('"') if len(parts) >= 2 else line.strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1].strip('"')
            changed_from_status.append(path_part)

        all_changed = set()
        for p in changed_from_diff + changed_from_status:
            all_changed.add(validate_repository_relative_path(p))
        sorted_changed = tuple(sorted(all_changed))

        clean = (len(norm_status) == 0 and len(sorted_changed) == 0)

        snap = WorkspaceSnapshot(
            snapshot_id=snapshot_id or f"snap-{uuid.uuid4().hex[:16]}",
            workspace_id=workspace.workspace_id,
            task_id=workspace.task_id,
            candidate_id=workspace.candidate_id,
            run_id=run.run_id,
            base_sha=workspace.base_sha.lower(),
            head_sha=head_sha.lower(),
            branch=workspace.branch,
            worktree_path=str(workspace.worktree_path),
            execution_epoch=run.execution_epoch,
            phase=phase,
            captured_at=now or datetime.now(timezone.utc).isoformat(),
            git_status=norm_status,
            changed_paths=sorted_changed,
            diff_stat=diff_stat,
            diff_digest=diff_digest,
            clean=clean,
        )

        # Record in registry
        self.registry.record(snap)
        return snap

    def create_diff_artifact(
        self,
        workspace: WorkspaceIdentity,
        run: AgentRunIdentity,
        lease: WorktreeLease | None = None,
        artifact_id: str | None = None,
        now: str | None = None,
    ) -> DiffArtifact:
        """Create a standalone DiffArtifact representing worktree changes relative to base SHA."""
        wt_path = Path(workspace.worktree_path).resolve()
        if self.canonical_repo_path is not None and wt_path == self.canonical_repo_path:
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_CANONICAL_FORBIDDEN.value,
                f"Diff artifact creation forbidden on canonical checkout: {workspace.worktree_path}",
            )

        if lease is not None and not lease.is_active():
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                f"Lease is not active: {lease.state.value}",
            )

        cwd_str = str(wt_path)
        rc, head_out, _ = self.git_executor(["rev-parse", "HEAD"], cwd_str)
        head_sha = head_out.strip()

        rc, diff_out, _ = self.git_executor(["diff", "--binary", "--no-ext-diff", f"{workspace.base_sha}...HEAD"], cwd_str)
        rc, stat_out, _ = self.git_executor(["diff", "--stat", f"{workspace.base_sha}...HEAD"], cwd_str)
        rc, name_out, _ = self.git_executor(["diff", "--name-only", f"{workspace.base_sha}...HEAD"], cwd_str)
        changed_paths = [line.strip() for line in name_out.splitlines() if line.strip()]

        # Binary files detection via numstat
        rc, numstat_out, _ = self.git_executor(["diff", "--numstat", f"{workspace.base_sha}...HEAD"], cwd_str)
        binary_files: list[str] = []
        for line in numstat_out.splitlines():
            parts = line.strip().split(maxsplit=2)
            if len(parts) == 3 and parts[0] == "-" and parts[1] == "-":
                binary_files.append(parts[2].strip('"'))

        return generate_diff_artifact(
            workspace_id=workspace.workspace_id,
            base_sha=workspace.base_sha.lower(),
            head_sha=head_sha.lower(),
            changed_paths=changed_paths,
            diff_stat=stat_out.strip(),
            raw_diff=diff_out,
            binary_files=binary_files,
            candidate_id=workspace.candidate_id,
            artifact_id=artifact_id,
            now=now,
        )
