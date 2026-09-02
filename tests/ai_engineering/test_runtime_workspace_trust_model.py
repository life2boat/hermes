"""PR-13.1 corrective tests: honest workspace trust model (C9-C13).

Establishes and verifies the ACTUAL security model of the controlled
runtime: the child process is a TRUSTED_CHILD_POLICY process, not an
OS sandbox. Concretely:

- a child that creates a symlink into the canonical repository and
  writes through it DOES mutate the canonical repo during execution;
  the runtime DETECTS this post-execution (CANONICAL_CHECKOUT_PROTECTED,
  error message says DETECTED, never PREVENTED) and the candidate fails;
- writes into foreign worktrees or external temp paths are NOT
  prevented and (outside the canonical repo) NOT detected — proving the
  absence of a filesystem sandbox honestly;
- a child may spawn descendant processes (no process-spawn sandbox).

No test in this module claims "escape prevention" for a mutation that
actually happened.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ai_engineering.candidates.candidate_contracts import CandidateState
from ai_engineering.runtime.runtime_evidence import build_candidate_result_from_evidence
from tests.ai_engineering.runtime_fixture_helpers import (
    WORKSPACE_ID,
    execute_default,
    make_local_fixture,
)


def _symlink_supported(tmp_path) -> bool:
    probe = tmp_path / "symlink_probe"
    try:
        os.symlink(tmp_path, probe, target_is_directory=True)
        return True
    except OSError:
        return False


class TestSymlinkSecurityModel:
    def test_symlink_write_into_canonical_is_detected_not_prevented(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        if not _symlink_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        workspace_root = fx.workspace_manager.canonical_root.parent / "workspaces" / WORKSPACE_ID
        canonical_readme = fx.canonical_root / "README.md"
        child_code = (
            "import os\n"
            f"os.symlink({str(fx.canonical_root)!r}, {str(workspace_root / 'escape_link')!r}, target_is_directory=True)\n"
            f"open({str(workspace_root / 'escape_link' / 'README.md')!r}, 'a').write('TAMPER')\n"
        )
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", child_code),
            execution_id="exec-tm1",
        )
        # The mutation really happened: prevention is NOT claimed.
        assert "TAMPER" in canonical_readme.read_text(encoding="utf-8")
        # The runtime DETECTED the violation post-execution.
        assert "CANONICAL_CHECKOUT_PROTECTED" in evidence.blockers
        assert "DETECTED" in (evidence.error_message or "").upper()
        assert "PREVENTED" not in (evidence.error_message or "").upper()

    def test_symlink_write_into_foreign_worktree_is_not_prevented(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        if not _symlink_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        workspace_root = fx.workspace_manager.canonical_root.parent / "workspaces" / WORKSPACE_ID
        foreign = tmp_path / "foreign_tree"
        foreign.mkdir()
        foreign_target = foreign / "secret.txt"
        child_code = (
            "import os\n"
            f"os.symlink({str(foreign)!r}, {str(workspace_root / 'foreign_link')!r}, target_is_directory=True)\n"
            f"open({str(foreign_target)!r}, 'w').write('LEAK')\n"
        )
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", child_code),
            execution_id="exec-tm2",
        )
        # Trusted-child model: the external write really happened and the
        # runtime did not and can not prevent it.
        assert foreign_target.exists()
        assert foreign_target.read_text(encoding="utf-8") == "LEAK"
        # It is outside the canonical repo, so post-execution canonical
        # detection does not fire; the honest expectation is no detection.
        assert "CANONICAL_CHECKOUT_PROTECTED" not in evidence.blockers

    def test_symlink_write_into_external_tmp_is_not_prevented(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        if not _symlink_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        workspace_root = fx.workspace_manager.canonical_root.parent / "workspaces" / WORKSPACE_ID
        external = tmp_path / "external_outside_workspace.txt"
        child_code = (
            "import os\n"
            f"os.symlink({str(tmp_path)!r}, {str(workspace_root / 'tmp_link')!r}, target_is_directory=True)\n"
            f"open({str(external)!r}, 'w').write('OUT')\n"
        )
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", child_code),
            execution_id="exec-tm3",
        )
        assert external.exists()
        assert external.read_text(encoding="utf-8") == "OUT"

    def test_child_can_read_outside_workspace_no_filesystem_sandbox(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        external = tmp_path / "outside.txt"
        external.write_text("READABLE", encoding="utf-8")
        request, evidence = execute_default(
            fx,
            argv=(
                sys.executable,
                "-c",
                f"print(open(r'{external}', encoding='utf-8').read())",
            ),
            execution_id="exec-tm4",
        )
        assert "READABLE" in evidence.stdout


class TestChildSpawnCapability:
    def test_child_may_spawn_descendants_no_spawn_sandbox(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        marker = tmp_path / "workspaces" / WORKSPACE_ID / "descendant.txt"
        child_code = (
            "import subprocess, sys\n"
            f"subprocess.run([sys.executable, '-c', r\"open(r'{marker}', 'w').write('1')\"], check=True)\n"
        )
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", child_code),
            execution_id="exec-tm5",
        )
        assert evidence.exit_code == 0
        assert marker.exists()


class TestCanonicalViolationBlocksCandidate:
    def test_security_violation_never_completes_candidate(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        if not _symlink_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host")
        workspace_root = fx.workspace_manager.canonical_root.parent / "workspaces" / WORKSPACE_ID
        child_code = (
            "import os\n"
            f"os.symlink({str(fx.canonical_root)!r}, {str(workspace_root / 'escape_link')!r}, target_is_directory=True)\n"
            f"open({str(workspace_root / 'escape_link' / 'README.md')!r}, 'a').write('TAMPER')\n"
        )
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", child_code),
            execution_id="exec-tm6",
        )
        assert "CANONICAL_CHECKOUT_PROTECTED" in evidence.blockers
        artifacts = fx.runtime.get_artifacts(evidence.execution_id)
        result = build_candidate_result_from_evidence(
            evidence,
            branch="codex/candidate/task-1/cand-1",
            post_execution_snapshot=artifacts.post_execution_snapshot,
            diff_artifact=artifacts.diff_artifact,
        )
        assert result.state == CandidateState.FAILED
        assert result.success is False
        assert "CANONICAL_CHECKOUT_PROTECTED" in result.blockers


class TestDocumentationClaimsMatchImplementation:
    def test_enforcement_matrix_present_in_invariants(self):
        """RUNTIME-11 must exist and classify every audited capability."""
        repo_root = Path(__file__).resolve().parents[2]
        docs = (repo_root / "docs" / "HERMES_INVARIANTS.md").read_text(encoding="utf-8")
        assert "RUNTIME-11" in docs
        for capability in (
            "INITIAL_EXECUTABLE_POLICY",
            "CWD_CONFINEMENT",
            "ENVIRONMENT_SANITIZATION",
            "FILESYSTEM_SANDBOX",
            "NETWORK_SANDBOX",
            "PROCESS_SPAWN_SANDBOX",
            "SECRET_INHERITANCE",
            "DIRECT_CHILD_REAPING",
            "PROCESS_TREE_REAPING",
        ):
            assert capability in docs
        assert "TRUSTED_CHILD_POLICY" in docs
        assert "PROCESS_TREE_CONTAINMENT=PLATFORM_DEPENDENT" in docs

    def test_command_policy_docstring_does_not_claim_arbitrary_code_sandbox(self):
        import inspect

        from ai_engineering.runtime import runtime_policy

        doc = inspect.getdoc(runtime_policy) or ""
        assert "NOT an arbitrary-code sandbox" in doc
