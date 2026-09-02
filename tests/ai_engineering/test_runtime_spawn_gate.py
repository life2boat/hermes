"""PR-13 spawn authorization gate tests (fail-closed before spawn)."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ai_engineering.contracts import EffectClass, StopBoundary
from ai_engineering.runtime.runtime_contracts import AgentRuntimeError, RuntimeBlockingReason, RuntimeMode
from ai_engineering.runtime.runtime_policy import RuntimePolicy
from ai_engineering.runtime.spawn_gate import authorize_spawn
from tests.ai_engineering.runtime_fixture_helpers import (
    RUN_ID,
    WORKSPACE_ID,
    make_local_fixture,
    make_request,
)


def _authorize(fx, request, **overrides):
    params = dict(
        policy=overrides.pop("policy", fx.runtime.policy),
        intent=overrides.pop("intent", fx.intent),
        authority=overrides.pop("authority", fx.authority),
        workspace=overrides.pop("workspace", fx.workspace),
        run_record=overrides.pop("run_record", fx.run_registry.get_run(RUN_ID)),
        host=overrides.pop("host", fx.runtime._resolve_host_identity(request)),
        candidate=overrides.pop("candidate", fx.candidate),
        workspace_manager=fx.workspace_manager,
        clock=overrides.pop("clock", None),
    )
    params.update(overrides)
    return authorize_spawn(request, **params)


class TestActivationPolicyGate:
    def test_disabled_policy_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path, mode=RuntimeMode.DISABLED)
        request = make_request(fx)
        decision = _authorize(fx, request)
        assert not decision.authorized
        assert "RUNTIME_ACTIVATION_DISABLED" in decision.blockers

    def test_shadow_local_authorizes(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        decision = _authorize(fx, request)
        assert decision.authorized, decision.blockers

    def test_timeout_over_policy_limit_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        policy = RuntimePolicy(mode=RuntimeMode.SHADOW_LOCAL, max_timeout_seconds=1.0)
        request = make_request(fx, timeout_seconds=5.0)
        decision = _authorize(fx, request, policy=policy)
        assert "RUNTIME_ACTIVATION_DISABLED" in decision.blockers

    def test_output_over_policy_limit_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        policy = RuntimePolicy(mode=RuntimeMode.SHADOW_LOCAL, max_output_bytes=1024)
        request = make_request(fx, max_stdout_bytes=65536)
        decision = _authorize(fx, request, policy=policy)
        assert "RUNTIME_ACTIVATION_DISABLED" in decision.blockers

    def test_mode_host_mismatch_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        policy = RuntimePolicy(mode=RuntimeMode.SHADOW_WSL)
        request = make_request(fx)
        decision = _authorize(fx, request, policy=policy)
        assert "EXECUTION_MODE_INVALID" in decision.blockers


class TestIntentBinding:
    def test_missing_intent_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        decision = _authorize(fx, request, intent=None)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers

    def test_task_mismatch_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, task_id="task-other")
        decision = _authorize(fx, request)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers

    def test_base_mismatch_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, base_sha="b" * 40)
        decision = _authorize(fx, request)
        assert "CANDIDATE_BASE_SHA_MISMATCH" in decision.blockers

    def test_wrong_authority_digest_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, authority_digest="a" * 64)
        decision = _authorize(fx, request)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers


class TestAuthorityGate:
    def test_production_authority_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        from dataclasses import replace

        bad = replace(fx.authority, production_authorized=True)
        decision = _authorize(fx, request, authority=bad)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers

    def test_secret_authority_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        from dataclasses import replace

        bad = replace(fx.authority, secret_access_authorized=True)
        decision = _authorize(fx, request, authority=bad)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers

    def test_data_authority_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        from dataclasses import replace

        bad = replace(fx.authority, data_access_authorized=True)
        decision = _authorize(fx, request, authority=bad)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers

    def test_git_push_effect_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        from dataclasses import replace

        bad = replace(
            fx.authority,
            allowed_effect_classes=(EffectClass.GIT_PUSH,),
        )
        decision = _authorize(fx, request, authority=bad)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers

    def test_merge_stop_boundary_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        from dataclasses import replace

        bad = replace(fx.authority, stop_boundary=StopBoundary.MERGE)
        decision = _authorize(fx, request, authority=bad)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers

    def test_missing_authority_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        decision = _authorize(fx, request, authority=None)
        assert "CONTROL_PLANE_AUTHORIZATION_MISMATCH" in decision.blockers


class TestWorkspaceLeaseGate:
    def test_missing_workspace_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        decision = _authorize(fx, request, workspace=None)
        assert "WORKSPACE_NOT_FOUND" in decision.blockers

    def test_wrong_workspace_id_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, workspace_id="ws-other")
        decision = _authorize(fx, request)
        assert "RUN_WORKSPACE_MISMATCH" in decision.blockers

    def test_released_lease_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        fx.workspace_manager.release_lease(WORKSPACE_ID, RUN_ID)
        request = make_request(fx)
        decision = _authorize(fx, request)
        assert not decision.authorized
        assert "WORKTREE_IDENTITY_MISMATCH" in decision.blockers

    def test_expired_lease_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        stale = replace(fx.lease, expires_at=past)
        fx.workspace_manager._leases[WORKSPACE_ID] = stale
        request = make_request(fx)
        decision = _authorize(fx, request)
        assert "LEASE_EXPIRED" in decision.blockers

    def test_wrong_lease_owner_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, run_id="run-other")
        decision = _authorize(fx, request)
        assert "RUN_LEASE_OWNERSHIP_MISMATCH" in decision.blockers


class TestRunIdentityGate:
    def test_missing_run_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        decision = _authorize(fx, request, run_record=None)
        assert "STALE_RUN_EVENT" in decision.blockers

    def test_stale_epoch_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, execution_epoch=2)
        decision = _authorize(fx, request)
        assert "STALE_RUN_MUTATION" in decision.blockers

    def test_wrong_host_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, execution_host_id="host-other")
        decision = _authorize(fx, request)
        assert "EXECUTION_HOST_MISMATCH" in decision.blockers

    def test_wrong_candidate_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, candidate_id="cand-other")
        decision = _authorize(fx, request)
        assert not decision.authorized


class TestWorkingDirectoryGate:
    def test_parent_directory_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, working_directory="..")
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value

    def test_foreign_relative_path_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, working_directory="../../outside")
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value

    def test_unc_path_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, working_directory="\\\\server\\share")
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value

    def test_workspace_root_allowed(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, working_directory=".")
        decision = _authorize(fx, request)
        assert "RUNTIME_WORKSPACE_ESCAPE" not in decision.blockers


class TestCommandGate:
    def test_forbidden_command_blocks(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, argv=("git", "push", "origin", "main"))
        decision = _authorize(fx, request)
        assert "RUNTIME_COMMAND_NOT_AUTHORIZED" in decision.blockers

    def test_authorized_command_passes(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, argv=(sys.executable, "-c", "print('ok')"))
        decision = _authorize(fx, request)
        assert decision.authorized
