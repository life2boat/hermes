"""Unit tests for candidate runner and command execution."""

from __future__ import annotations

from pathlib import Path
import pytest

from ai_engineering.candidates.candidate_contracts import (
    CandidateBlockingReason,
    CandidateError,
    CandidateImplementationRequest,
    CandidateState,
)
from ai_engineering.candidates.candidate_runner import (
    make_candidate_branch_name,
    validate_candidate_command,
)


def test_make_candidate_branch_name():
    branch = make_candidate_branch_name("task-01", "cand-01")
    assert branch == "codex/candidate/task-01/cand-01"

    with pytest.raises(CandidateError):
        make_candidate_branch_name("../task", "cand")

    with pytest.raises(CandidateError):
        make_candidate_branch_name("task", "cand/sub")


def test_validate_candidate_command():
    # Allowed
    validate_candidate_command(("pytest", "tests/"))
    validate_candidate_command(("python3", "-m", "unittest"))
    validate_candidate_command(("ruff", "check", "."))
    validate_candidate_command(("git", "diff"))

    # Forbidden
    with pytest.raises(CandidateError) as exc:
        validate_candidate_command(("docker", "run", "ubuntu"))
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_NOT_AUTHORIZED.value

    with pytest.raises(CandidateError) as exc:
        validate_candidate_command(("git", "push", "origin", "main"))
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_NOT_AUTHORIZED.value

    with pytest.raises(CandidateError) as exc:
        validate_candidate_command(("gh", "pr", "merge", "123"))
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_NOT_AUTHORIZED.value
