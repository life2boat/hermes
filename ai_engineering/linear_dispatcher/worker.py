"""Hermes Linear Autonomous Dispatcher - Production Worker Entrypoint."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from ai_engineering.linear_dispatcher.contracts import DispatcherConfig, TaskState
from ai_engineering.linear_dispatcher.dispatcher import AutonomousDispatcher
from ai_engineering.linear_dispatcher.execution_ledger import (
    CorruptedLedgerError,
    ExecutionLedger,
    ExecutionState,
    SingleWorkerLock,
)
from ai_engineering.linear_dispatcher.github_service_production import (
    GitHubProductionService,
)
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.linear_client_production import (
    LinearProductionClient,
)
from ai_engineering.linear_dispatcher.task_executor import (
    CodexTaskExecutor,
    DshModelGateway,
    ITaskExecutor,
    LinuxCodexTaskExecutor,
    SandboxedBrokerTaskExecutor,
    SimulatedTaskExecutor,
)
from ai_engineering.linear_dispatcher.worktree_service import WorktreeService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("hermes.dispatcher.worker")


class DispatcherWorker:
    def __init__(self, task_executor: ITaskExecutor | None = None) -> None:
        self.shutdown_requested = False
        self._task_executor = task_executor
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        logger.info("Graceful shutdown requested...")
        self.shutdown_requested = True

    def validate_canonical_root(self, canonical_root: Path) -> tuple[bool, str | None]:
        """Validate that canonical root is a valid git repository pointing to life2boat/hermes."""
        c_root = Path(canonical_root).resolve()
        if not c_root.exists() or not c_root.is_dir():
            return False, f"CANONICAL_ROOT_NOT_FOUND: {c_root}"

        try:
            # Check git repository
            res_git = subprocess.run(
                ["git", "-C", str(c_root), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
            )
            if res_git.returncode != 0:
                return False, f"NOT_A_GIT_REPOSITORY: {c_root}"

            # Check github remote
            res_remote = subprocess.run(
                ["git", "-C", str(c_root), "remote", "get-url", "github"],
                capture_output=True,
                text=True,
            )
            if res_remote.returncode != 0:
                return False, f"INVALID_CANONICAL_REMOTE: {res_remote.stderr.strip() or 'failed to get url'}"
            remote_url = res_remote.stdout.strip()
            norm_url = remote_url.rstrip("/")
            if norm_url.endswith(".git"):
                norm_url = norm_url[:-4]
            valid_canonical_urls = {
                "https://github.com/life2boat/hermes",
                "git@github.com:life2boat/hermes",
                "ssh://git@github.com/life2boat/hermes",
            }
            if norm_url not in valid_canonical_urls:
                return False, f"INVALID_CANONICAL_REMOTE: {remote_url}"

            # Check github/main ref exists
            res_ref = subprocess.run(
                [
                    "git",
                    "-C",
                    str(c_root),
                    "rev-parse",
                    "--verify",
                    "refs/remotes/github/main",
                ],
                capture_output=True,
                text=True,
            )
            if res_ref.returncode != 0:
                return False, "MISSING_GITHUB_MAIN_REF"

            return True, None
        except Exception as e:
            return False, f"CANONICAL_ROOT_VALIDATION_ERROR: {e}"

    def validate_dependencies(
        self,
        config: DispatcherConfig,
        wt_base: Path,
        lease_path: Path,
        canonical_root: Path | None = None,
    ) -> bool:
        if not os.environ.get("GITHUB_TOKEN"):
            logger.error("Missing GITHUB_TOKEN")
            return False
        if not os.environ.get("LINEAR_API_KEY"):
            logger.error("Missing LINEAR_API_KEY")
            return False

        try:
            wt_base.mkdir(parents=True, exist_ok=True)
            test_file = wt_base / ".write_test"
            test_file.touch()
            test_file.unlink()
        except Exception as e:
            logger.error(f"Cannot write to worktree base directory {wt_base}: {e}")
            return False

        try:
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            test_file = lease_path.parent / ".write_test"
            test_file.touch()
            test_file.unlink()
        except Exception as e:
            logger.error(f"Cannot write to lease directory {lease_path.parent}: {e}")
            return False

        if canonical_root:
            ok, root_err = self.validate_canonical_root(canonical_root)
            if not ok:
                logger.error(f"Canonical root validation failed: {root_err}")
                return False

        # Validate executor backend and isolation invariants
        executor = self._get_executor()
        if not executor or not executor.health():
            logger.error(
                "Configured task execution backend is not available or failed isolation health check. NOT_READY."
            )
            return False

        if hasattr(executor, "validate_isolation"):
            iso_ok, iso_err = executor.validate_isolation()
            if not iso_ok:
                logger.error(
                    f"Configured task execution backend failed isolation validation: {iso_err}. NOT_READY."
                )
                return False

        if hasattr(executor, "execution_ready") and not executor.execution_ready():
            logger.error(
                "Configured task execution backend is not execution ready. "
                "Refusing to declare Dispatcher Worker READY. NOT_READY."
            )
            return False

        return True

    def _get_executor(self) -> ITaskExecutor | None:
        if self._task_executor:
            return self._task_executor

        backend = os.environ.get("DISPATCHER_EXECUTOR_BACKEND", "codex").lower()
        if backend in ("codex", "linux_codex"):
            return LinuxCodexTaskExecutor()
        elif backend in ("sandboxed_broker", "broker", "safe_broker"):
            dsh_bin = os.environ.get("DSH_BIN")
            use_dsh = os.environ.get("HERMES_USE_DSH", "").lower() in ("1", "true")
            patch_path = os.environ.get("DSH_PATCH_PATH")
            gateway = (
                DshModelGateway(dsh_bin=dsh_bin, patch_path=patch_path)
                if (dsh_bin or use_dsh)
                else None
            )
            return SandboxedBrokerTaskExecutor(model_gateway=gateway)
        elif backend in ("simulated", "offline"):
            return SimulatedTaskExecutor()
        return None

    def _resolve_canonical_main_sha(self) -> str:
        try:
            result = subprocess.run(
                [
                    "git",
                    "ls-remote",
                    "https://github.com/life2boat/hermes.git",
                    "refs/heads/main",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            if not result.stdout.strip():
                raise RuntimeError("Empty response from ls-remote")
            sha = result.stdout.split()[0]
            if len(sha) != 40:
                raise RuntimeError(f"Invalid SHA format: {sha}")
            return sha
        except Exception as e:
            logger.error(f"Failed to resolve canonical main SHA: {e}")
            return ""

    def run(self) -> None:
        logger.info("Starting Dispatcher Worker...")
        enabled = (
            os.environ.get("HEALBITE_LINEAR_DISPATCHER_ENABLED", "false").lower()
            == "true"
        )
        mode = os.environ.get("HEALBITE_LINEAR_DISPATCHER_MODE", "shadow")

        if not enabled:
            logger.info(
                "Dispatcher is disabled (HEALBITE_LINEAR_DISPATCHER_ENABLED=false). Exiting gracefully."
            )
            sys.exit(0)

        if mode != "shadow":
            logger.error(
                f"LIVE mode unsupported or fail-closed for v1. Found mode: {mode}"
            )
            sys.exit(1)

        config = DispatcherConfig(enabled=enabled, mode=mode)

        # Production Paths
        base_run_dir = Path(
            os.environ.get(
                "DISPATCHER_RUNTIME_DIR", "/var/lib/hermes/linear-dispatcher"
            )
        ).resolve()
        wt_base = (base_run_dir / "worktrees").resolve()
        lease_path = (base_run_dir / "leases.json").resolve()
        ledger_path = (base_run_dir / "execution_state.json").resolve()
        lock_path = (base_run_dir / "worker.lock").resolve()
        canonical_root = Path(
            os.environ.get("HERMES_CANONICAL_ROOT", "/home/runner/work/hermes/hermes")
        ).resolve()

        if not self.validate_dependencies(config, wt_base, lease_path, canonical_root):
            logger.error("Dependency validation failed. NOT_READY.")
            sys.exit(1)

        lock = SingleWorkerLock(lock_path)
        if not lock.acquire():
            logger.error(
                "Failed to acquire single-worker lock. Another instance is running. BLOCKED."
            )
            sys.exit(1)

        try:
            linear_client = LinearProductionClient()
            lease_manager = LeaseManager(persistence_path=lease_path)
            github_service = GitHubProductionService()
            ledger = ExecutionLedger(ledger_path)
            executor = self._get_executor()

            canonical_sha = self._resolve_canonical_main_sha()
            if not canonical_sha:
                logger.error(
                    "Could not resolve canonical main SHA at startup. BLOCKED."
                )
                sys.exit(1)

            # Check recovery state
            try:
                state = ledger.read_state()
            except CorruptedLedgerError as e:
                logger.error(
                    f"CRITICAL: Recovery ledger corrupted: {e}. BLOCKED."
                )
                sys.exit(1)
            if state:
                logger.info(
                    f"Found recovery state for task {state.task_id} at state: {state.state}"
                )
                if lease_manager.is_held_by_foreign_worker(
                    state.task_id, config.worker_id
                ):
                    logger.error(
                        "CRITICAL: Recovery blocked: lease held by foreign worker. BLOCKED."
                    )
                    sys.exit(1)

                # Initialize dispatcher for recovery
                worktree_service = WorktreeService(
                    canonical_root=canonical_root,
                    base_sha=state.base_sha or canonical_sha,
                )
                dispatcher = AutonomousDispatcher(
                    config=config,
                    linear_client=linear_client,
                    lease_manager=lease_manager,
                    worktree_service=worktree_service,
                    github_service=github_service,
                    worktree_base_dir=wt_base,
                    canonical_main_sha=state.base_sha or canonical_sha,
                    execution_ledger=ledger,
                    task_executor=executor,
                )
                rec_result = dispatcher.recover_task(state)
                if rec_result is None or rec_result.final_state != TaskState.DONE:
                    reason = (rec_result.block_reason if rec_result else "UNRESOLVED_RECOVERY_STATE") or "UNRESOLVED_RECOVERY_STATE"
                    logger.error(
                        f"Task recovery failed: {reason}. BLOCKED."
                    )
                    sys.exit(1)

            logger.info("Dispatcher Worker READY. Mode: shadow")

            while not self.shutdown_requested:
                try:
                    tasks = linear_client.list_issues()
                    eligible = [
                        t
                        for t in tasks
                        if "agent:auto" in t.labels and "agent:shadow" in t.labels
                    ]

                    if not eligible:
                        time.sleep(15)
                        continue

                    # Fresh canonical SHA and fresh WorktreeService per dispatch cycle
                    canonical_sha = self._resolve_canonical_main_sha()
                    if not canonical_sha:
                        logger.error(
                            "Could not resolve canonical main SHA. Skipping poll."
                        )
                        time.sleep(15)
                        continue

                    worktree_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)
                    dispatcher = AutonomousDispatcher(
                        config=config,
                        linear_client=linear_client,
                        lease_manager=lease_manager,
                        worktree_service=worktree_service,
                        github_service=github_service,
                        worktree_base_dir=wt_base,
                        canonical_main_sha=canonical_sha,
                        execution_ledger=ledger,
                        task_executor=executor,
                    )

                    result = dispatcher.dispatch_one_task(eligible)
                    if result:
                        logger.info(
                            f"Task {result.task_id} processed. Final state: {result.final_state}. Reason: {result.block_reason}"
                        )

                    time.sleep(15)
                except Exception as e:
                    logger.error(f"Unexpected error in poll loop: {e}", exc_info=True)
                    try:
                        surviving_state = ledger.read_state()
                        if surviving_state and "dispatcher" in locals():
                            rec_res = dispatcher.recover_task(surviving_state)
                            if rec_res is not None and rec_res.final_state != TaskState.DONE:
                                reason = rec_res.block_reason or "UNRESOLVED_RECOVERY_STATE"
                                logger.error(
                                    f"CRITICAL: Post-exception recovery failed: {reason}. BLOCKED."
                                )
                                sys.exit(1)
                    except Exception as rec_err:
                        logger.error(
                            f"CRITICAL: Reconciliation error after unexpected exception: {rec_err}. BLOCKED."
                        )
                        sys.exit(1)
                    time.sleep(15)

            logger.info("Dispatcher Worker STOPPING...")
        finally:
            lock.release()


if __name__ == "__main__":
    worker = DispatcherWorker()
    worker.run()
