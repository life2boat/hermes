"""Unit tests for repository investigation runner and command validation."""

from __future__ import annotations

from pathlib import Path
import pytest

from ai_engineering.execution.run_registry import ActiveRunRegistry
from ai_engineering.investigation.investigation_contracts import (
    InvestigationBlockingReason,
    InvestigationError,
    RepositoryInvestigationBatch,
    RepositoryInvestigationRequest,
    RepositoryInvestigationResult,
)
from ai_engineering.investigation.investigation_runner import (
    ParallelRepositoryInvestigator,
    execute_single_investigation,
    validate_investigation_command,
)
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)


def test_command_validation_allowlist():
    # Allowed
    validate_investigation_command(("git", "grep", "foo"))
    validate_investigation_command(("git", "rev-parse", "HEAD"))
    validate_investigation_command(("rg", "pattern", "ai_engineering"))
    validate_investigation_command(("grep", "-rn", "pattern"))

    # Forbidden
    with pytest.raises(InvestigationError) as exc:
        validate_investigation_command(("git", "commit", "-m", "foo"))
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_WRITE_FORBIDDEN.value

    with pytest.raises(InvestigationError) as exc:
        validate_investigation_command(("rm", "-rf", "foo"))
    assert exc.value.code in (
        InvestigationBlockingReason.INVESTIGATION_WRITE_FORBIDDEN.value,
        InvestigationBlockingReason.INVESTIGATION_COMMAND_FORBIDDEN.value,
    )


def test_single_investigation_execution(tmp_path: Path):
    # Setup test file
    test_file = tmp_path / "hello.py"
    test_file.write_text("def hello_world():\n    return 'hermes'\n", encoding="utf-8")

    # Mock git head
    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True, check=True).stdout.strip()

    req = RepositoryInvestigationRequest(
        investigation_id="inv-1",
        task_id="task-1",
        node_id="node-1",
        base_sha=head_sha,
        repository_root=str(tmp_path),
        query="hello_world",
        scope_paths=("hello.py",),
    )

    res = execute_single_investigation(req, "run-1")
    assert res.success is True
    assert len(res.matches) == 1
    assert res.matches[0].path == "hello.py"
    assert res.matches[0].line_start == 1
    assert "def hello_world" in res.matches[0].snippet
