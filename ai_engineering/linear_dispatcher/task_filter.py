"""Task filtering, selection, and deterministic ordering for Linear tasks."""

from __future__ import annotations

from typing import Sequence

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    DispatcherConfig,
    LinearTask,
)
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager

FINISHED_STATES = frozenset(["done", "canceled", "cancelled", "completed"])


def is_eligible_task(
    task: LinearTask,
    config: DispatcherConfig,
) -> tuple[bool, BlockReasonCode | None]:
    """Check if task satisfies the autonomous execution contract.

    Rule: Task title is NEVER used as a security boundary.
    Only explicit labels define eligibility.
    """
    if task.state.strip().lower() in FINISHED_STATES:
        return False, None

    labels_lower = {l.strip().lower() for l in task.labels}

    if config.require_auto_label and "agent:auto" not in labels_lower:
        return False, BlockReasonCode.MISSING_AGENT_AUTO_LABEL

    if config.require_shadow_label and "agent:shadow" not in labels_lower:
        return False, BlockReasonCode.MISSING_AGENT_SHADOW_LABEL

    return True, None


def priority_rank(priority: int) -> int:
    """Map Linear priority to rank where lower number means higher priority.

    In Linear:
    1 = Urgent
    2 = High
    3 = Medium
    4 = Low
    0 = None
    """
    if priority in (1, 2, 3, 4):
        return priority
    return 5  # No priority ranks lowest


def sort_tasks(tasks: Sequence[LinearTask]) -> list[LinearTask]:
    """Sort tasks deterministically by: priority -> createdAt -> issue ID."""
    return sorted(
        tasks,
        key=lambda t: (
            priority_rank(t.priority),
            t.created_at,
            t.id,
        ),
    )


def select_next_task(
    tasks: Sequence[LinearTask],
    lease_manager: LeaseManager,
    config: DispatcherConfig,
    now_iso: str | None = None,
) -> LinearTask | None:
    """Find the next eligible issue with no active foreign lease."""
    eligible: list[LinearTask] = []
    for task in tasks:
        is_ok, _ = is_eligible_task(task, config)
        if not is_ok:
            continue
        # Check lease status
        if lease_manager.is_held_by_foreign_worker(task.id, config.worker_id, now_iso=now_iso):
            continue
        eligible.append(task)

    if not eligible:
        return None

    sorted_eligible = sort_tasks(eligible)
    return sorted_eligible[0]
