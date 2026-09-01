"""Unit tests for RemoteExecutionLifecycle state machine."""

from __future__ import annotations

import pytest

from ai_engineering.execution.remote_contracts import (
    ReconciliationOutcome,
    RemoteBlockingReason,
    RemoteExecutionError,
    RemoteExecutionState,
    RemoteProcessIdentity,
    RemoteReconciliationResult,
)
from ai_engineering.execution.remote_state import RemoteExecutionLifecycle


def _make_proc_identity() -> RemoteProcessIdentity:
    return RemoteProcessIdentity(
        execution_id="exec-01",
        run_id="run-01",
        workspace_id="ws-01",
        execution_host_id="host-ssh-1",
        session_id="sess-01",
        remote_process_id="pid-100",
        execution_epoch=1,
    )


def test_remote_state_progression():
    proc = _make_proc_identity()
    lc = RemoteExecutionLifecycle(proc)
    assert lc.state == RemoteExecutionState.CREATED

    lc.transition_to(RemoteExecutionState.LIVE, session_id="sess-01", execution_epoch=1)
    assert lc.state == RemoteExecutionState.LIVE


def test_disconnect_and_unverifiable_semantics():
    proc = _make_proc_identity()
    lc = RemoteExecutionLifecycle(proc, initial_state=RemoteExecutionState.LIVE)

    # Disconnect transitions to DISCONNECTED and sets blocker, does not fabricate EXITED
    lc.transition_to(RemoteExecutionState.DISCONNECTED, session_id="sess-01", execution_epoch=1)
    assert lc.state == RemoteExecutionState.DISCONNECTED
    assert lc.exit_code is None
    assert RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value in lc.blockers


def test_stale_epoch_or_session_event_rejected():
    proc = _make_proc_identity()
    lc = RemoteExecutionLifecycle(proc, initial_state=RemoteExecutionState.LIVE)

    # Stale session rejected
    with pytest.raises(RemoteExecutionError) as exc:
        lc.transition_to(RemoteExecutionState.EXITED, session_id="old-sess", execution_epoch=1, exit_code=0)
    assert exc.value.code == RemoteBlockingReason.STALE_RUN_EVENT.value

    # Stale epoch rejected
    with pytest.raises(RemoteExecutionError) as exc2:
        lc.transition_to(RemoteExecutionState.EXITED, session_id="sess-01", execution_epoch=999, exit_code=0)
    assert exc2.value.code == RemoteBlockingReason.STALE_RUN_EVENT.value


def test_reconciliation_application():
    proc = _make_proc_identity()
    lc = RemoteExecutionLifecycle(proc, initial_state=RemoteExecutionState.UNVERIFIABLE)

    res_live = RemoteReconciliationResult(
        execution_id="exec-01",
        run_id="run-01",
        execution_host_id="host-ssh-1",
        session_id="sess-01",
        execution_epoch=1,
        outcome=ReconciliationOutcome.CONFIRMED_LIVE,
        process_confirmed_live=True,
        process_confirmed_exited=False,
        exit_code=None,
        evidence="Process verified running",
        reconciled_at="2026-09-01T00:00:00Z",
    )
    lc.apply_reconciliation(res_live)
    assert lc.state == RemoteExecutionState.LIVE
    assert RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value not in lc.blockers
