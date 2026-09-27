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
from ai_engineering.linear_dispatcher.writeback import ILinearClient

class LinearClient(ILinearClient):
    pass
class GitHubService:
    pass

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

    def validate_dependencies(self, config: DispatcherConfig, wt_base: Path) -> bool:
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
        wt_base = Path(os.environ.get("DISPATCHER_WORKTREE_BASE", "/tmp/hermes-dispatcher-wt")).resolve()
        lease_path = Path(os.environ.get("DISPATCHER_LEASE_PATH", "/tmp/hermes-dispatcher-lease.json")).resolve()

        if not self.validate_dependencies(config, wt_base):
            logger.error("Dependency validation failed. NOT_READY.")
            sys.exit(1)

        # In a real app we'd inject proper dependencies here
        linear_client = LinearClient()
        lease_manager = LeaseManager(persistence_path=lease_path)
        worktree_service = WorktreeService()
        github_service = GitHubService()

        logger.info("Dispatcher Worker READY. Mode: shadow")
        
        while not self.shutdown_requested:
            try:
                tasks = [] # mock fetch tasks for shadow mode loop
                if not tasks:
                    time.sleep(15)
                    continue

                canonical_sha = self._resolve_canonical_main_sha()
                if not canonical_sha:
                    logger.error("Could not resolve canonical main SHA. Skipping poll.")
                    time.sleep(15)
                    continue

                dispatcher = AutonomousDispatcher(
                    config=config,
                    linear_client=linear_client,
                    lease_manager=lease_manager,
                    worktree_service=worktree_service,
                    github_service=github_service,
                    worktree_base_dir=wt_base,
                    canonical_main_sha=canonical_sha,
                )

                result = dispatcher.dispatch_one_task(tasks)
                if result:
                    logger.info(f"Task {result.task_id} processed. Final state: {result.final_state}. Block reason: {result.block_reason}")
                
                time.sleep(15)
            except Exception as e:
                logger.error(f"Unexpected error in poll loop: {e}", exc_info=True)
                time.sleep(15)

        logger.info("Dispatcher Worker STOPPING...")

if __name__ == "__main__":
    worker = DispatcherWorker()
    worker.run()
