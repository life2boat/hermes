import os
from pathlib import Path

def test_nix_workflow_truthfulness_semantics():
    root = Path(__file__).resolve().parents[2]
    workflow_path = root / ".github" / "workflows" / "nix.yml"
    assert workflow_path.exists(), "Nix workflow file missing"

    content = workflow_path.read_text(encoding="utf-8")

    # 1. Post sticky PR comment (stale hashes) must have continue-on-error: true
    # We find the index of the step name and ensure continue-on-error is within the step block
    assert "name: Post sticky PR comment (stale hashes)" in content

    # 2. Clear sticky PR comment (resolved) must have continue-on-error: true
    assert "name: Clear sticky PR comment (resolved)" in content

    # In YAML, if continue-on-error is added, we should find it twice near these steps
    import yaml
    try:
        with open(workflow_path, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)

        jobs = parsed.get("jobs", {})
        nix_job = jobs.get("nix", {})
        steps = nix_job.get("steps", [])

        post_comment_step = next((s for s in steps if s.get("name") == "Post sticky PR comment (stale hashes)"), None)
        clear_comment_step = next((s for s in steps if s.get("name") == "Clear sticky PR comment (resolved)"), None)
        final_fail_step = next((s for s in steps if s.get("name") == "Final fail if flake check failed"), None)

        assert post_comment_step is not None, "Missing Post comment step"
        assert post_comment_step.get("continue-on-error") is True, "Post comment step must have continue-on-error: true"

        assert clear_comment_step is not None, "Missing Clear comment step"
        assert clear_comment_step.get("continue-on-error") is True, "Clear comment step must have continue-on-error: true"

        assert final_fail_step is not None, "Missing Final fail step"
        assert final_fail_step.get("continue-on-error", False) is False, "Final fail step must NOT continue on error"

    except ImportError:
        # Fallback to string matching if pyyaml is missing
        assert "continue-on-error: true" in content
