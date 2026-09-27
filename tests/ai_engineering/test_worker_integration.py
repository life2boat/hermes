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
    assert "tasks = [] # mock fetch tasks" in content, "Defect 1 not present"

def test_stub_linear_client_fails(tmp_path):
    from ai_engineering.linear_dispatcher.worker import LinearClient
    client = LinearClient()
    with pytest.raises(AttributeError):
        client.list_issues()

def test_stub_github_service_fails(tmp_path):
    from ai_engineering.linear_dispatcher.worker import GitHubService
    service = GitHubService()
    with pytest.raises(AttributeError):
        service.get_ci_status("repo", "sha", 1)

def test_worktree_construction_invalid(tmp_path):
    with open("ai_engineering/linear_dispatcher/worker.py", "r") as f:
        content = f.read()
    assert "worktree_service = WorktreeService()" in content, "Defect 4 not present"
    
def test_tmp_lease_storage(tmp_path):
    with open("ai_engineering/linear_dispatcher/worker.py", "r") as f:
        content = f.read()
    assert "/tmp/hermes-dispatcher-lease.json" in content, "Defect 5 not present"
