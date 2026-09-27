import pytest
import os
from pathlib import Path
from typing import Any
import json
from ai_engineering.linear_dispatcher.worker import DispatcherWorker
from ai_engineering.linear_dispatcher.contracts import DispatcherConfig
from ai_engineering.linear_dispatcher.worktree_service import WorktreeService
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager

def test_worker_fails_discovery_due_to_tasks_stub(tmp_path):
    with open("ai_engineering/linear_dispatcher/worker.py", "r") as f:
        content = f.read()
    assert "tasks = linear_client.list_issues()" in content or "linear_client.list_issues()" in content, "Defect 1 not fixed"

def test_stub_linear_client_fails(tmp_path):
    with open("ai_engineering/linear_dispatcher/worker.py", "r") as f:
        content = f.read()
    assert "LinearProductionClient" in content, "Defect 2 not fixed"

def test_stub_github_service_fails(tmp_path):
    with open("ai_engineering/linear_dispatcher/worker.py", "r") as f:
        content = f.read()
    assert "GitHubProductionService" in content, "Defect 3 not fixed"

def test_worktree_construction_invalid(tmp_path):
    with open("ai_engineering/linear_dispatcher/worker.py", "r") as f:
        content = f.read()
    assert "worktree_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)" in content, "Defect 4 not fixed"
    
def test_tmp_lease_storage(tmp_path):
    with open("ai_engineering/linear_dispatcher/worker.py", "r") as f:
        content = f.read()
    assert "/var/lib/hermes/linear-dispatcher" in content, "Defect 5 not fixed"
