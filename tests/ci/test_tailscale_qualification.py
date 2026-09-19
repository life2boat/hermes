import yaml
import pytest
from pathlib import Path

def test_tailscale_workflow_security():
    workflow_path = Path(".github/workflows/release-qualification.yml")
    assert workflow_path.exists()
    
    with open(workflow_path, "r") as f:
        content = f.read()
        
    doc = yaml.safe_load(content)
    qualify_job = doc["jobs"]["qualify"]
    
    # id-token: write exists
    assert qualify_job.get("permissions", {}).get("id-token") == "write", "id-token: write is required for WIF"
    
    steps = qualify_job["steps"]
    
    ts_step_index = -1
    collect_step_index = -1
    
    for i, step in enumerate(steps):
        if step.get("name") == "Tailscale Connect":
            ts_step_index = i
            # official Tailscale action is immutable-SHA pinned
            assert step.get("uses") == "tailscale/github-action@306e68a486fd2350f2bfc3b19fcd143891a4a2d8"
            
            # WIF uses TS_OAUTH_CLIENT_ID + TS_AUDIENCE
            with_args = step.get("with", {})
            assert with_args.get("oauth-client-id") == "${{ secrets.TS_OAUTH_CLIENT_ID }}"
            assert with_args.get("audience") == "${{ secrets.TS_AUDIENCE }}"
            
            # tag is exactly tag:hermes-ci
            assert with_args.get("tags") == "tag:hermes-ci"
            
        elif step.get("name") == "Collect Canonical Production Evidence":
            collect_step_index = i
            
            run_script = step.get("run", "")
            # PROD_SSH_PORT validation remains fail-closed
            assert "if [ -z \"${PROD_SSH_PORT}\" ]; then" in run_script
            
            # StrictHostKeyChecking=yes remains
            assert "StrictHostKeyChecking=yes" in run_script
            
            # no StrictHostKeyChecking=no
            assert "StrictHostKeyChecking=no" not in run_script
            
            # no ssh-keyscan trust bootstrap
            assert "ssh-keyscan" not in run_script
            
            # no public WAN host hardcoding
            assert "79.137.196.14" not in run_script
            
            # HERMES_PROVENANCE_KEY absent from remote collection step
            assert "HERMES_PROVENANCE_KEY" not in step.get("env", {})
            assert "HERMES_PROVENANCE_KEY" not in run_script
            
    # Tailscale step occurs before SSH evidence collection
    assert ts_step_index != -1
    assert collect_step_index != -1
    assert ts_step_index < collect_step_index
    

    preflight_step_index = -1
    
    for i, step in enumerate(steps):
        if step.get("name") == "Verify Tailscale Production Path":
            preflight_step_index = i
            run_script = step.get("run", "")
            # Path verification uses PROD_SSH_HOST
            assert "tailscale ping" in run_script
            assert "${PROD_SSH_HOST}" in run_script
            assert "100.97.138.4" not in run_script
            assert "tailscale ssh" not in run_script.lower()

    # Path verification executes after Tailscale Connect
    assert preflight_step_index > ts_step_index
    # Path verification executes before production SSH collection
    assert preflight_step_index < collect_step_index

    # canonical signer/verifier flow unchanged
    signer_found = any(step.get("name") == "Sign Canonical Production Evidence" for step in steps)
    verifier_found = any(step.get("name") == "Run Qualifier" for step in steps)
    assert signer_found
    assert verifier_found
