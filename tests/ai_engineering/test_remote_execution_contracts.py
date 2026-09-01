"""Unit tests for SSH-ready remote execution contracts and serialization."""

from __future__ import annotations

import pytest

from ai_engineering.execution.host_contracts import ExecutionMode
from ai_engineering.execution.remote_contracts import (
    REMOTE_CONTRACT_VERSION,
    ReconciliationOutcome,
    RemoteBlockingReason,
    RemoteExecutionError,
    RemoteExecutionHostIdentity,
    RemoteHostPlatform,
    RemoteHostState,
    RemoteProcessIdentity,
    RemoteReconciliationRequest,
    RemoteReconciliationResult,
    RemoteSessionIdentity,
    SshExecutionConfig,
)


def test_ssh_mode_contract():
    assert ExecutionMode.SSH == "SSH"


def test_remote_host_identity_serialization():
    ident = RemoteExecutionHostIdentity(
        execution_host_id="host-ssh-1",
        mode=ExecutionMode.SSH,
        host_alias="server-prod-eval",
        remote_platform=RemoteHostPlatform.LINUX,
        architecture="x86_64",
        trust_domain="eval-domain",
    )
    d = ident.to_dict()
    assert d["execution_host_id"] == "host-ssh-1"
    assert d["mode"] == "SSH"
    assert d["host_alias"] == "server-prod-eval"

    restored = RemoteExecutionHostIdentity.from_dict(d)
    assert restored == ident


def test_ssh_config_opaque_refs_and_verification():
    cfg = SshExecutionConfig(
        host_alias="remote-eval",
        port=2222,
        username_ref="ref://auth/user/eval",
        credential_ref="ref://vault/ssh/key-eval",
        known_host_ref="ref://known_hosts/eval",
    )
    assert cfg.credential_ref.startswith("ref://")
    assert cfg.known_host_ref.startswith("ref://")
    assert cfg.verification_required is True

    # Invalid credential ref rejected
    with pytest.raises(RemoteExecutionError):
        SshExecutionConfig(
            host_alias="remote-eval",
            credential_ref="raw_private_key_contents",  # Forbidden raw secret
        )

    # Invalid known_hosts ref rejected
    with pytest.raises(RemoteExecutionError):
        SshExecutionConfig(
            host_alias="remote-eval",
            known_host_ref="raw_unverified_host",
        )


def test_remote_session_and_process_identity():
    session = RemoteSessionIdentity(
        session_id="sess-01",
        execution_host_id="host-ssh-1",
        execution_epoch=1,
    )
    assert session.session_id == "sess-01"

    proc = RemoteProcessIdentity(
        execution_id="exec-01",
        run_id="run-01",
        workspace_id="ws-01",
        execution_host_id="host-ssh-1",
        session_id="sess-01",
        remote_process_id="pid-12345",
        execution_epoch=1,
    )
    d_proc = proc.to_dict()
    assert d_proc["remote_process_id"] == "pid-12345"
    assert d_proc["execution_epoch"] == 1


def test_reconciliation_request_and_result():
    req = RemoteReconciliationRequest(
        execution_id="exec-01",
        run_id="run-01",
        execution_host_id="host-ssh-1",
        session_id="sess-01",
        execution_epoch=1,
    )
    res = RemoteReconciliationResult(
        execution_id="exec-01",
        run_id="run-01",
        execution_host_id="host-ssh-1",
        session_id="sess-01",
        execution_epoch=1,
        outcome=ReconciliationOutcome.CONFIRMED_LIVE,
        process_confirmed_live=True,
        process_confirmed_exited=False,
        exit_code=None,
        evidence="Process confirmed active via reconciliation",
        reconciled_at="2026-09-01T00:00:00Z",
    )
    assert res.outcome == ReconciliationOutcome.CONFIRMED_LIVE
    assert res.process_confirmed_live is True
