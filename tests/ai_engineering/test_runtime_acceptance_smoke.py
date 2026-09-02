"""PR-13 controlled end-to-end runtime acceptance smoke (Phase 31).

Scenario:
1. create real isolated canonical repo + candidate worktree
2. register valid workspace/lease/run identity
3. authorize SHADOW_LOCAL
4. spawn a real Python process
5. process writes only inside the candidate workspace
6. capture output/result + PRE/POST_EXECUTION snapshots + diff
7. produce candidate evidence
8. verify canonical main remains clean
9. project runtime state via observability
"""

from __future__ import annotations

import json
import subprocess
import sys

from ai_engineering.candidates.candidate_contracts import CandidateState
from ai_engineering.observability.runtime_views import build_runtime_views
from ai_engineering.runtime.runtime_evidence import build_candidate_result_from_evidence
from tests.ai_engineering.runtime_fixture_helpers import make_local_fixture, make_request


def test_real_local_agent_execution_smoke(tmp_path):
    fx = make_local_fixture(tmp_path)

    # Real bounded agent process: writes a file only inside the workspace.
    agent_code = (
        "from pathlib import Path\n"
        "Path('src_probe').mkdir(exist_ok=True)\n"
        "Path('src_probe/feature.py').write_text('VALUE = 41 + 1\\n')\n"
        "print('agent-done')\n"
    )
    request = make_request(
        fx,
        argv=(sys.executable, "-c", agent_code),
        execution_id="exec-acceptance",
    )
    evidence = fx.runtime.execute_agent_process(
        request,
        intent=fx.intent,
        authority=fx.authority,
        run_identity=fx.run_identity,
        candidate=fx.candidate,
    )

    # 1. proven real execution
    assert evidence.exit_proven is True
    assert evidence.exit_code == 0
    assert evidence.success is True
    assert evidence.timed_out is False
    assert evidence.cancelled is False
    assert evidence.process is not None
    assert "agent-done" in evidence.stdout
    assert evidence.blockers == ()

    # 2. workspace artifact actually created inside the candidate worktree
    feature = (
        fx.workspace_manager.canonical_root.parent
        / "workspaces"
        / "ws-task-1"
        / "src_probe"
        / "feature.py"
    )
    assert feature.exists()
    assert "VALUE = 41 + 1" in feature.read_text(encoding="utf-8")

    # 3. snapshot/diff evidence chain bound to workspace/candidate/base/run/epoch
    artifacts = fx.runtime.get_artifacts(evidence.execution_id)
    assert artifacts.pre_execution_snapshot is not None
    assert artifacts.post_execution_snapshot is not None
    assert artifacts.diff_artifact is not None
    post = artifacts.post_execution_snapshot
    assert post.workspace_id == "ws-task-1"
    assert post.candidate_id == "cand-1"
    assert post.base_sha == fx.base_sha
    assert post.run_id == "run-1"
    assert post.execution_epoch == 1
    assert "src_probe" in post.changed_paths

    # 4. candidate evidence via the canonical adapter
    result = build_candidate_result_from_evidence(
        evidence,
        branch="codex/candidate/task-1/cand-1",
        pre_execution_snapshot=artifacts.pre_execution_snapshot,
        post_execution_snapshot=artifacts.post_execution_snapshot,
        diff_artifact=artifacts.diff_artifact,
    )
    assert result.state == CandidateState.COMPLETED
    assert result.success is True
    # validation evidence is NOT fabricated by the runtime
    assert result.validation_results == ()

    # 5. canonical repository remains clean
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(fx.canonical_root),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == ""

    # 6. observability projection over the runtime evidence
    projection = build_runtime_views([evidence])
    payload = json.loads(json.dumps(projection.to_dict()))
    assert payload["schema_version"] == 1
    assert payload["processes"][0]["state"] == "EXITED"
    assert payload["processes"][0]["exit_code"] == 0
    assert payload["processes"][0]["exit_proven"] is True
    assert payload["truncation"]["processes_truncated"] is False

    # 7. evidence JSON is deterministic and secret-free
    blob = evidence.to_json()
    assert blob == evidence.to_json()
    assert "GITHUB_TOKEN" not in blob
    assert "raw_prompt" not in blob


def test_wsl_agent_execution_smoke_mock_launcher(tmp_path):
    """WSL acceptance via the deterministic mock launcher (host-independent)."""
    from tests.ai_engineering.test_runtime_wsl_activation import TestWslActivation

    harness = TestWslActivation()
    fx, launcher = harness._build_wsl_fixture(tmp_path)
    request = make_request(
        fx,
        argv=("python3", "-c", "print('wsl-smoke')"),
        execution_id="exec-wsl-acceptance",
        execution_host_id="host-wsl",
    )
    evidence = fx.runtime.execute_agent_process(
        request,
        intent=fx.intent,
        authority=fx.authority,
        run_identity=fx.run_identity,
        candidate=fx.candidate,
    )
    assert evidence.exit_proven is True
    assert "wsl-ok" in evidence.stdout
    # argv passed through wsl.exe --exec unchanged, shell=False enforced
    cmd, kwargs = launcher.calls[0]
    assert "--exec" in cmd and "python3" in cmd
    assert kwargs.get("shell") is False
