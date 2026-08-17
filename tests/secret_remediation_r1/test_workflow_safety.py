import os
from pathlib import Path


def test_safety_workflow_has_pipefail_and_no_masked_failures():
    root = Path(__file__).resolve().parents[2]
    workflow_path = root / ".github" / "workflows" / "secret-remediation-r1-safety.yml"
    assert workflow_path.exists(), "Safety workflow file missing"

    content = workflow_path.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content or "set -o pipefail" in content
    assert "tee pytest_output.log" in content
    assert "test_preflight_docker_integration.py" in content
