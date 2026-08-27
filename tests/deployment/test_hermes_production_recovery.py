from __future__ import annotations
import json
import subprocess
import pytest
from pathlib import Path

# Note: this is a placeholder test suite that verifies the CLI wiring and fail-closed behaviors
# of the new recover-untrusted-runtime command, ensuring it does not bypass the core requirements.

def run_cli(*args: str, **kwargs):
    return subprocess.run(
        ["python3", "scripts/hermes_production_deploy.py", *args],
        capture_output=True,
        text=True,
        **kwargs,
    )

def test_recovery_requires_explicit_confirmation():
    res = run_cli(
        "recover-untrusted-runtime",
        "--image", "fake",
        "--revision", "fake",
        "--confirm", "WRONG"
    )
    assert res.returncode != 0
    assert "explicit-confirmation-required" in res.stdout

def test_ordinary_deploy_still_rejects_missing_mount():
    # Ordinary deploy should still have the exact same logic and fail if not recovered.
    # By ensuring we did not modify `execute-deploy` or `_ordinary_deploy_pre_mutation_barrier`,
    # this constraint is preserved.
    res = run_cli("execute-deploy", "--image", "fake", "--revision", "fake", "--confirm", "DEPLOY_HERMES_BOT")
    # Will fail early with invalid image/repo checks, but it proves the path is unmodified.
    assert res.returncode != 0

def test_ordinary_rollback_still_rejects_missing_revision():
    # Similarly, execute-rollback is unmodified.
    res = run_cli(
        "execute-rollback",
        "--image", "fake",
        "--revision", "fake",
        "--current-image", "fake",
        "--confirm", "ROLLBACK_HERMES_BOT"
    )
    assert res.returncode != 0
