"""PR-13 workspace confinement and canonical write-boundary tests."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from ai_engineering.runtime.runtime_contracts import RuntimeBlockingReason
from tests.ai_engineering.runtime_fixture_helpers import (
    WORKSPACE_ID,
    execute_default,
    make_local_fixture,
    make_request,
)


def _canonical_porcelain(canonical_root) -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(canonical_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


class TestWorkspaceConfinement:
    def test_process_writes_confined_to_workspace(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", "open('out.txt', 'w').write('x')"),
            execution_id="exec-w1",
        )
        assert evidence.exit_proven and evidence.exit_code == 0
        workspace_root = fx.workspace_manager.canonical_root.parent / "workspaces" / WORKSPACE_ID
        assert (workspace_root / "out.txt").exists()

    def test_write_outside_workspace_not_counted_as_change(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        outside = tmp_path / "outside_dump.txt"
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", f"open(r'{outside}', 'w').write('x')"),
            execution_id="exec-w2",
        )
        assert evidence.exit_proven and evidence.exit_code == 0
        artifacts = fx.runtime.get_artifacts(evidence.execution_id)
        assert artifacts is not None
        assert artifacts.post_execution_snapshot is not None
        assert artifacts.post_execution_snapshot.changed_paths == ()

    def test_canonical_write_flags_protection(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        canonical_readme = fx.canonical_root / "README.md"
        request, evidence = execute_default(
            fx,
            argv=(
                sys.executable,
                "-c",
                f"open(r'{canonical_readme}', 'a').write('TAMPER')",
            ),
            execution_id="exec-w3",
        )
        assert "CANONICAL_CHECKOUT_PROTECTED" in evidence.blockers
        assert evidence.success is False
        # Restore is not attempted: canonical mutation is a hard failure signal.
        assert "TAMPER" in canonical_readme.read_text(encoding="utf-8")

    def test_canonical_delete_flags_protection(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        canonical_readme = fx.canonical_root / "README.md"
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", f"import os; os.remove(r'{canonical_readme}')"),
            execution_id="exec-w4",
        )
        assert "CANONICAL_CHECKOUT_PROTECTED" in evidence.blockers
        assert not canonical_readme.exists()

    def test_canonical_clean_after_normal_run(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(fx, execution_id="exec-w5")
        assert "CANONICAL_CHECKOUT_PROTECTED" not in evidence.blockers
        assert _canonical_porcelain(fx.canonical_root) == ""

    def test_dirty_workspace_blocks_spawn(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        workspace_root = fx.workspace_manager.canonical_root.parent / "workspaces" / WORKSPACE_ID
        (workspace_root / "uncommitted.txt").write_text("dirty", encoding="utf-8")
        request = make_request(fx, execution_id="exec-w6")
        from ai_engineering.runtime.runtime_contracts import AgentRuntimeError

        with pytest.raises(AgentRuntimeError) as exc:
            fx.runtime.execute_agent_process(
                request,
                intent=fx.intent,
                authority=fx.authority,
                run_identity=fx.run_identity,
                candidate=fx.candidate,
            )
        assert "WORKTREE_DIRTY_REUSE" in exc.value.blockers

    def test_symlink_escape_into_canonical_flagged(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        workspace_root = fx.workspace_manager.canonical_root.parent / "workspaces" / WORKSPACE_ID
        probe_link = tmp_path / "symlink_probe"
        try:
            os.symlink(tmp_path, probe_link, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not permitted on this host")
        child_code = (
            "import os\n"
            f"os.symlink({str(fx.canonical_root)!r}, {str(workspace_root / 'escape_link')!r}, target_is_directory=True)\n"
            f"open({str(workspace_root / 'escape_link' / 'README.md')!r}, 'a').write('TAMPER')\n"
        )
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", child_code),
            execution_id="exec-w7",
        )
        assert "CANONICAL_CHECKOUT_PROTECTED" in evidence.blockers

    def test_foreign_workspace_cwd_rejected_at_request_level(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        other = tmp_path / "foreign_tree"
        other.mkdir()
        from ai_engineering.runtime.runtime_contracts import AgentRuntimeError, RuntimeBlockingReason

        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, working_directory=str(other))
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value
