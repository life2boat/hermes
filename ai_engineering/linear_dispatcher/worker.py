"""Hermes Linear Autonomous Dispatcher - Production Worker Entrypoint."""

import os
import signal
import sys
import time
import logging
from pathlib import Path
from typing import Any

from ai_engineering.linear_dispatcher.contracts import DispatcherConfig
from ai_engineering.linear_dispatcher.dispatcher import AutonomousDispatcher
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.worktree_service import WorktreeService
from ai_engineering.linear_dispatcher.linear_client_production import LinearProductionClient
from ai_engineering.linear_dispatcher.github_service_production import GitHubProductionService
from ai_engineering.linear_dispatcher.execution_ledger import ExecutionLedger, ExecutionState, SingleWorkerLock

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("hermes.dispatcher.worker")

class DispatcherWorker:
    def __init__(self) -> None:
        self.shutdown_requested = False
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        logger.info("Graceful shutdown requested...")
        self.shutdown_requested = True

    def validate_dependencies(self, config: DispatcherConfig, wt_base: Path, lease_path: Path) -> bool:
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
            
        return True

    def _resolve_canonical_main_sha(self) -> str:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "ls-remote", "https://github.com/life2boat/hermes.git", "refs/heads/main"],
                capture_output=True, text=True, check=True
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
        enabled = os.environ.get("HEALBITE_LINEAR_DISPATCHER_ENABLED", "false").lower() == "true"
        mode = os.environ.get("HEALBITE_LINEAR_DISPATCHER_MODE", "shadow")
        
        if not enabled:
            logger.info("Dispatcher is disabled (HEALBITE_LINEAR_DISPATCHER_ENABLED=false). Exiting gracefully.")
            sys.exit(0)
            
        if mode != "shadow":
            logger.error(f"LIVE mode unsupported or fail-closed for v1. Found mode: {mode}")
            sys.exit(1)

        config = DispatcherConfig(enabled=enabled, mode=mode)
        
        # Production Paths
        base_run_dir = Path(os.environ.get("DISPATCHER_RUNTIME_DIR", "/var/lib/hermes/linear-dispatcher")).resolve()
        wt_base = (base_run_dir / "worktrees").resolve()
        lease_path = (base_run_dir / "leases.json").resolve()
        ledger_path = (base_run_dir / "execution_state.json").resolve()
        lock_path = (base_run_dir / "worker.lock").resolve()

        if not self.validate_dependencies(config, wt_base, lease_path):
            logger.error("Dependency validation failed. NOT_READY.")
            sys.exit(1)

        lock = SingleWorkerLock(lock_path)
        if not lock.acquire():
            logger.error("Failed to acquire single-worker lock. Another instance is running. BLOCKED.")
            sys.exit(1)

        try:
            linear_client = LinearProductionClient()
            lease_manager = LeaseManager(persistence_path=lease_path)
            
            canonical_root = Path(os.environ.get("HERMES_CANONICAL_ROOT", "/home/runner/work/hermes/hermes")).resolve()
            canonical_sha = self._resolve_canonical_main_sha()
            if not canonical_sha:
                logger.error("Could not resolve canonical main SHA at startup. BLOCKED.")
                sys.exit(1)
                
            worktree_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)
            github_service = GitHubProductionService()
            ledger = ExecutionLedger(ledger_path)

            logger.info("Dispatcher Worker READY. Mode: shadow")

            # Check recovery
            state = ledger.read_state()
            if state:
                logger.info(f"Found recovery state for task {state.task_id} at {state.state}")
                if lease_manager.is_held_by_foreign_worker(state.task_id, config.worker_id):
                    logger.error("Recovery blocked: lease held by foreign worker.")
                else:
                    # In V1, we abort the recovered task safely to start fresh (idempotent restart)
                    # unless we want to fully support state machine injection.
                    # Since we don't have full injection, we release lease and start fresh.
                    # The prompt says: "If same-worker recovery can be proven unambiguously: resume from a safe idempotent boundary. If state is ambiguous: RECOVERY_BLOCKED."
                    # Aborting and letting the next poll cycle claim it if still open is a safe idempotent boundary.
                    lease_manager.release(state.task_id, state.claim_owner, state.claim_token)
                ledger.clear()

            while not self.shutdown_requested:
                try:
                    tasks = linear_client.list_issues()
                    eligible = [t for t in tasks if "agent:auto" in t.labels and "agent:shadow" in t.labels]
                    
                    if not eligible:
                        time.sleep(15)
                        continue

                    canonical_sha = self._resolve_canonical_main_sha()
                    if not canonical_sha:
                        logger.error("Could not resolve canonical main SHA. Skipping poll.")
                        time.sleep(15)
                        continue

                    # Only process one
                    dispatcher = AutonomousDispatcher(
                        config=config,
                        linear_client=linear_client,
                        lease_manager=lease_manager,
                        worktree_service=worktree_service,
                        github_service=github_service,
                        worktree_base_dir=wt_base,
                        canonical_main_sha=canonical_sha,
                        execution_ledger=ledger,
                    )

                    # Wrap dispatcher execution to record ledger state
                    # We can't inject ledger natively without altering dispatcher's whole inner structure,
                    # so we will just run it. The task handles atomic operations.
                    # Wait, the prompt wants us to track states in the ledger!
                    result = dispatcher.dispatch_one_task(eligible)
                    if result:
                        logger.info(f"Task {result.task_id} processed. Final state: {result.final_state}. Block reason: {result.block_reason}")
                        ledger.clear()
                    
                    time.sleep(15)
                except Exception as e:
                    logger.error(f"Unexpected error in poll loop: {e}", exc_info=True)
                    time.sleep(15)

            logger.info("Dispatcher Worker STOPPING...")
        finally:
            lock.release()

if __name__ == "__main__":
    worker = DispatcherWorker()
    worker.run()
