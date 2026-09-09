"""run_autonomous_supervisor.py - CLI entry point for the autonomous supervisor.

Usage:
    python scripts/run_autonomous_supervisor.py \\
        --intent <path-to-intent.json> \\
        [--state-dir <path-to-store-dir>] \\
        [--profile <path-to-profile.json>] \\
        [--worker-result <path-to-worker-result.json>] \\
        [--pr-number <int>] \\
        [--pr-head-sha <sha>] \\
        [--ci-state <PASS|FAIL|BLOCKED|PENDING>] \\
        [--output <output-path>]

Exit codes:
    0  DONE / PASS
    1  FAILED
    2  BLOCKED or error
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Ensure the repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_engineering.contracts import Status
from ai_engineering.effective_policy import (
    EffectivePolicyReport,
    EffectivePolicyStatus,
    TaskPolicyAttribution,
)
from ai_engineering.supervisor.dispatch.contracts import compute_dispatch_id
from ai_engineering.supervisor.dispatch.coordinator import DispatchCoordinator
from ai_engineering.supervisor.dispatch.github import (
    CIDetailedState,
    CIState,
    FakeGitHubProvider,
    PRState,
)
from ai_engineering.supervisor.dispatch.worker import FakeWorkerDispatcher
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.policy.contracts import WorkProfile
from ai_engineering.supervisor.policy.work_profile import validate_work_profile
from ai_engineering.supervisor.state import (
    SupervisorError,
    SupervisorPhase,
    canonical_serialize_state,
)
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.task_intent import (
    LineageNode,
    NodeKind,
    TaskIntent,
    TaskLineage,
    deserialize_intent,
    intent_digest,
)


def _load_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _exit_code(phase: SupervisorPhase) -> int:
    if phase == SupervisorPhase.DONE:
        return 0
    if phase == SupervisorPhase.FAILED:
        return 1
    return 2  # BLOCKED, CANCELLED, PENDING, etc.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hermes Autonomous Supervisor CLI (Task No.4)",
    )
    parser.add_argument(
        "--intent", required=True, metavar="PATH",
        help="Path to TaskIntent JSON file",
    )
    parser.add_argument(
        "--state-dir", default=None, metavar="DIR",
        help="Path to supervisor persistent state directory",
    )
    parser.add_argument(
        "--profile", default=None, metavar="PATH",
        help="Optional path to WorkProfile JSON file",
    )
    parser.add_argument(
        "--root-goal", default=None, metavar="TEXT",
        help="Optional root goal string",
    )
    parser.add_argument(
        "--root-goal-id", default=None, metavar="ID",
        help="Optional root goal ID",
    )
    parser.add_argument(
        "--run-id", default=None, metavar="ID",
        help="Optional run ID",
    )
    parser.add_argument(
        "--max-steps", type=int, default=50, metavar="N",
        help="Maximum coordination steps (default 50)",
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help="Optional path to write final SupervisorState JSON",
    )
    parser.add_argument(
        "--worker-result", default=None, metavar="PATH",
        help="Optional path to worker result bundle or verified result JSON to stage",
    )
    parser.add_argument(
        "--pr-number", type=int, default=None, metavar="INT",
        help="Optional GitHub PR number to simulate",
    )
    parser.add_argument(
        "--pr-head-sha", default=None, metavar="SHA",
        help="Optional PR head commit SHA",
    )
    parser.add_argument(
        "--ci-state", default=None, choices=["PASS", "FAIL", "BLOCKED", "PENDING"],
        help="Simulated GitHub CI state for the PR",
    )
    args = parser.parse_args(argv)

    # 1. Load Intent
    try:
        raw_intent = _load_text(args.intent)
        intent = deserialize_intent(raw_intent)
    except Exception as exc:
        print(f"ERROR: Cannot load intent: {exc}", file=sys.stderr)
        return 2

    # 2. Setup state storage directory
    state_dir = Path(args.state_dir) if args.state_dir else Path(tempfile.mkdtemp(prefix="hermes_supervisor_"))
    store = FileSupervisorStateStore(state_dir)
    loop = SupervisorLoop(store)

    root_goal_id = args.root_goal_id or intent.task_id
    run_id = args.run_id or root_goal_id
    root_goal = args.root_goal or intent.desired_outcome
    now_utc = datetime.now(timezone.utc).isoformat()

    # 3. Check existing run or initialize
    try:
        state = store.load_state(run_id)
    except Exception:
        lineage = TaskLineage(
            schema_version=1,
            nodes=(LineageNode(node_id=intent.task_id, kind=NodeKind.TASK),),
            edges=(),
        )
        state = loop.initialize_run(
            intent=intent,
            lineage=lineage,
            root_goal=root_goal,
            root_goal_id=root_goal_id,
            created_at_utc=now_utc,
        )

    # 4. Bind profile if provided
    work_profile = None
    if args.profile:
        try:
            profile_data = json.loads(_load_text(args.profile))
            work_profile = validate_work_profile(profile_data)
            tpa = TaskPolicyAttribution(
                task_id=intent.task_id,
                intent_revision=intent.intent_revision,
                intent_digest=intent_digest(intent),
                source_base_sha=intent.source_base_sha,
                constraints=intent.constraints,
                allowed_mutations=intent.allowed_mutations,
                forbidden_mutations=intent.forbidden_mutations,
                stop_boundary=intent.stop_boundary.value,
                source_id="s" * 64,
            )
            eff_policy = EffectivePolicyReport(
                schema_version=1,
                effective_policy_id="e" * 64,
                task_id=intent.task_id,
                intent_digest=intent_digest(intent),
                intent_revision=intent.intent_revision,
                source_base_sha=intent.source_base_sha,
                subject_sha=intent.source_base_sha,
                status=EffectivePolicyStatus.COMPLETE,
                policy_sources=(),
                task_policy=tpa,
                invariant_resolutions=(),
                required_gate_resolutions=(),
                unresolved_references=(),
                precedence_source_id="p" * 64,
            )
            if state.autonomy_state is None:
                state = loop.bind_profile(
                    run_id=run_id,
                    profile=work_profile,
                    effective_policy=eff_policy,
                    created_at_utc=now_utc,
                )
        except Exception as exc:
            print(f"ERROR: Cannot bind profile: {exc}", file=sys.stderr)
            return 2

    # 5. Setup Fake Worker & GitHub Providers
    dispatcher = FakeWorkerDispatcher()
    github = FakeGitHubProvider()

    pr_num = args.pr_number
    head_sha = args.pr_head_sha or intent.source_base_sha
    if pr_num is not None:
        github.add_pr(
            repository=intent.source_repository,
            pr_number=pr_num,
            pr=PRState(
                pr_number=pr_num,
                head_branch="feature",
                base_branch="main",
                head_sha=head_sha,
                mergeable=True,
                merge_state_status="clean",
            ),
        )

    if args.ci_state and pr_num is not None:
        ci_st = CIState(args.ci_state)
        github.add_ci(
            repository=intent.source_repository,
            pr_number=pr_num,
            head_sha=head_sha,
            ci=CIDetailedState(
                head_sha=head_sha,
                overall_state=ci_st,
                check_conclusions={"tests": "success" if ci_st == CIState.PASS else "failure"},
            ),
        )

    coordinator = DispatchCoordinator(
        loop=loop,
        store=store,
        run_id=run_id,
        worker_dispatcher=dispatcher,
        github_provider=github,
        work_profile=work_profile,
    )

    if pr_num is not None:
        coordinator.bind_pr(pr_num, head_sha)

    # 6. If offline worker result is supplied, stage it
    if args.worker_result:
        try:
            res_content = json.loads(_load_text(args.worker_result))
            coordinator.submit_worker_result(res_content)
        except Exception as exc:
            print(f"ERROR: Failed to load worker result: {exc}", file=sys.stderr)
            return 2

    # 7. Run coordinator loop
    final_state = coordinator.run_until_quiescent(max_steps=args.max_steps)

    # 8. Output results
    serialized = canonical_serialize_state(final_state)
    if args.output:
        try:
            Path(args.output).write_text(serialized, encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: Cannot write output: {exc}", file=sys.stderr)
            return 2

    print(f"Supervisor finished in phase: {final_state.phase.value} (revision: {final_state.state_revision})")
    return _exit_code(final_state.phase)


if __name__ == "__main__":
    sys.exit(main())
