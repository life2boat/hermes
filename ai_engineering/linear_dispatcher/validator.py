"""Local validation runner for task diffs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from ai_engineering.linear_dispatcher.worktree_service import (
    SAFE_GIT_OPTS,
    get_clean_git_env,
)


class LocalValidator:
    """Runs applicable canonical checks against the working directory."""

    @staticmethod
    def run_diff_check(cwd: Path | str) -> tuple[bool, str]:
        """Run git diff --check on unstaged and staged changes safely."""
        env = get_clean_git_env()

        res_unstaged = subprocess.run(
            ["git"] + SAFE_GIT_OPTS + ["-C", str(cwd), "diff", "--check"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if res_unstaged.returncode != 0:
            return False, res_unstaged.stdout or res_unstaged.stderr

        res_staged = subprocess.run(
            ["git"] + SAFE_GIT_OPTS + ["-C", str(cwd), "diff", "--check", "--cached"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if res_staged.returncode != 0:
            return False, res_staged.stdout or res_staged.stderr

        return True, "PASS"

    @staticmethod
    def run_validation(
        cwd: Path | str,
        run_tests: bool = False,
        trusted_root: Path | str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Run standard validation suite strictly using trusted validator scripts from canonical root."""
        results: dict[str, Any] = {}
        diff_ok, diff_msg = LocalValidator.run_diff_check(cwd)
        results["git_diff_check"] = "PASS" if diff_ok else f"FAIL: {diff_msg}"
        if not diff_ok:
            return False, results

        trusted_dir = (
            Path(trusted_root).resolve()
            if trusted_root
            else Path(__file__).resolve().parents[2]
        )

        # Sanitized environment: strip sensitive host secrets/tokens
        clean_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "PYTHONPATH": str(trusted_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "NoDefaultCurrentDirectoryInExePath": "1",
        }

        candidate_data_path = str(Path(cwd).resolve())

        # Footgun check executed strictly from trusted root with candidate as data only
        footguns_script = trusted_dir / "scripts" / "check-windows-footguns.py"
        if footguns_script.exists():
            fg_res = subprocess.run(
                [sys.executable, "-I", str(footguns_script), candidate_data_path],
                cwd=str(trusted_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=clean_env,
            )
            results["windows_footguns"] = (
                "PASS"
                if fg_res.returncode == 0
                else f"FAIL: {fg_res.stderr or fg_res.stdout}"
            )
            if fg_res.returncode != 0:
                return False, results

        # Secret scanner executed strictly from trusted root with candidate as data only
        secret_script = trusted_dir / "scripts" / "secret_scanner.py"
        if secret_script.exists():
            sec_res = subprocess.run(
                [sys.executable, "-I", str(secret_script), "--fail-on-skipped", candidate_data_path],
                cwd=str(trusted_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=clean_env,
            )
            results["secret_scanner"] = (
                "PASS"
                if sec_res.returncode == 0
                else f"FAIL: {sec_res.stderr or sec_res.stdout}"
            )
            if sec_res.returncode != 0:
                return False, results

        if run_tests:
            # Candidate code cannot be executed with host privileges outside sandbox
            results["pytest"] = "FAIL: host pytest execution in untrusted candidate worktree is forbidden"
            return False, results

        return True, results
