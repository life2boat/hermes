"""Shared fixture helpers for the PR-13 controlled runtime test suite.

Builds real isolated canonical repositories, candidate worktrees,
leases, run identities, canonical TaskIntents, and authority
boundaries. All git operations run against temp directories.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ai_engineering.candidates.candidate_contracts import CandidateIdentity
from ai_engineering.contracts import AuthorityBoundary, EffectClass, StopBoundary, TaskClass
from ai_engineering.execution.local_host import LocalExecutionHost
from ai_engineering.execution.run_registry import ActiveRunRegistry
from ai_engineering.execution.wsl_host import WslExecutionHost
from ai_engineering.runtime.agent_runtime import ControlledAgentRuntime
from ai_engineering.runtime.runtime_contracts import AgentExecutionRequest, RuntimeMode
from ai_engineering.runtime.runtime_policy import RuntimePolicy
from ai_engineering.task_intent import AcceptanceCriterion, IntentStatus, TaskIntent, intent_digest
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity, WorktreeLease
from ai_engineering.workspaces.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
NODE_ID = "node-1"
CANDIDATE_ID = "cand-1"
RUN_ID = "run-1"
WORKSPACE_ID = "ws-task-1"
LOCAL_HOST_ID = "host-local"
WSL_HOST_ID = "host-wsl"


def init_canonical_repo(repo_path: Path) -> str:
    """Initialize a real git repo with one commit; return the commit SHA."""
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Test User"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    (repo_path / "README.md").write_text("Canonical", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "Initial commit"], check=True, capture_output=True)
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def make_intent(base_sha: str, *, task_id: str = TASK_ID) -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id=task_id,
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Bounded implementation inside an isolated candidate workspace.",
        source_repository="life2boat/hermes",
        source_main_ref="main",
        source_base_sha=base_sha,
        constraints=(),
        allowed_mutations=("src/",),
        forbidden_mutations=(),
        stop_boundary=StopBoundary.LOCAL_DIFF,
        acceptance_criteria=(AcceptanceCriterion(criterion_id="c1", statement="Works"),),
        unknowns=(),
        applicable_invariants=(),
        required_gates=(),
    )


def make_authority() -> AuthorityBoundary:
    return AuthorityBoundary(
        allowed_effect_classes=(EffectClass.REPOSITORY_WRITE, EffectClass.READ_ONLY),
        forbidden_effect_classes=(
            EffectClass.GIT_PUSH,
            EffectClass.PR_MERGE,
            EffectClass.DEPLOY,
            EffectClass.SECRET_MUTATION,
        ),
        stop_boundary=StopBoundary.LOCAL_DIFF,
        production_authorized=False,
        secret_access_authorized=False,
        data_access_authorized=False,
    )


@dataclass
class RuntimeFixture:
    """A fully assembled controlled runtime scenario."""

    runtime: ControlledAgentRuntime
    workspace_manager: WorkspaceManager
    run_registry: ActiveRunRegistry
    workspace: WorkspaceIdentity
    lease: WorktreeLease
    run_identity: object
    candidate: CandidateIdentity
    intent: TaskIntent
    authority: AuthorityBoundary
    base_sha: str
    canonical_root: Path

    @property
    def authority_digest(self) -> str:
        return intent_digest(self.intent)


def make_local_fixture(
    tmp_path: Path,
    *,
    mode: RuntimeMode = RuntimeMode.SHADOW_LOCAL,
    max_concurrent_processes: int = 3,
    auto_spawn_run: bool = True,
    budget_parallel: int | None = None,
    parent_env_override: dict[str, str] | None = None,
) -> RuntimeFixture:
    """Assemble a SHADOW_LOCAL runtime over a real isolated repo/worktree."""
    repo_dir = tmp_path / "canonical_repo"
    base_sha = init_canonical_repo(repo_dir)

    workspace_manager = WorkspaceManager(repo_dir)
    run_registry = ActiveRunRegistry(workspace_manager=workspace_manager)

    workspace, lease = workspace_manager.create_isolated_workspace(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        candidate_id=CANDIDATE_ID,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=base_sha,
        branch="codex/candidate/task-1/cand-1",
        worktree_path=tmp_path / "workspaces" / WORKSPACE_ID,
        execution_host_id=LOCAL_HOST_ID,
        execution_mode="LOCAL",
        owner_run_id=RUN_ID,
        auto_acquire_lease=True,
    )

    from ai_engineering.execution.run_contracts import AgentRunIdentity
    from ai_engineering.execution.run_state import RunState

    run_identity = AgentRunIdentity(
        run_id=RUN_ID,
        task_id=TASK_ID,
        node_id=NODE_ID,
        workspace_id=WORKSPACE_ID,
        candidate_id=CANDIDATE_ID,
        model="test-model",
        agent_capability="CANDIDATE_IMPLEMENTATION",
        execution_host_id=LOCAL_HOST_ID,
        execution_epoch=1,
        start_time=datetime.now(timezone.utc),
    )
    if auto_spawn_run:
        run_registry.spawn_agent(run_identity)

    candidate = CandidateIdentity(
        candidate_id=CANDIDATE_ID,
        task_id=TASK_ID,
        node_id=NODE_ID,
        base_sha=base_sha,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
    )

    intent = make_intent(base_sha)
    authority = make_authority()
    policy = RuntimePolicy(
        mode=mode,
        max_concurrent_processes=budget_parallel or max_concurrent_processes,
    )
    runtime = ControlledAgentRuntime(
        policy=policy,
        workspace_manager=workspace_manager,
        run_registry=run_registry,
        local_host=LocalExecutionHost(execution_host_id=LOCAL_HOST_ID),
        parent_env=parent_env_override
        or {"PATH": "C:\\fake-path", "SYSTEMROOT": "C:\\fake-system"},
    )

    return RuntimeFixture(
        runtime=runtime,
        workspace_manager=workspace_manager,
        run_registry=run_registry,
        workspace=workspace,
        lease=lease,
        run_identity=run_identity,
        candidate=candidate,
        intent=intent,
        authority=authority,
        base_sha=base_sha,
        canonical_root=repo_dir,
    )


def make_request(
    fixture: RuntimeFixture,
    *,
    execution_id: str = "exec-1",
    argv: tuple[str, ...] | None = None,
    working_directory: str = ".",
    timeout_seconds: float = 20.0,
    base_sha: str | None = None,
    run_id: str = RUN_ID,
    workspace_id: str = WORKSPACE_ID,
    candidate_id: str = CANDIDATE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    execution_host_id: str = LOCAL_HOST_ID,
    execution_epoch: int = 1,
    repository_id: str = "life2boat/hermes",
    authority_digest: str | None = None,
    max_stdout_bytes: int = 65536,
    max_stderr_bytes: int = 65536,
) -> AgentExecutionRequest:
    import sys

    if argv is None:
        argv = (sys.executable, "-c", "print('hello from runtime')")
    return AgentExecutionRequest(
        execution_id=execution_id,
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        cycle_id="cycle-1",
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        repository_id=repository_id,
        base_sha=(base_sha or fixture.base_sha).lower(),
        execution_epoch=execution_epoch,
        execution_host_id=execution_host_id,
        agent_capability="CANDIDATE_IMPLEMENTATION",
        command_argv=argv,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        authority_digest=authority_digest or fixture.authority_digest,
    )


def execute_default(fixture: RuntimeFixture, **kwargs):
    """Convenience: build a default request and execute it."""
    request = make_request(fixture, **kwargs)
    evidence = fixture.runtime.execute_agent_process(
        request,
        intent=fixture.intent,
        authority=fixture.authority,
        run_identity=fixture.run_identity,
        candidate=fixture.candidate,
    )
    return request, evidence
