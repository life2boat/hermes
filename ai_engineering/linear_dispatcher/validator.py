"""Local validation runner for task diffs."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any


class LocalValidator:
    """Runs applicable canonical checks against the working directory."""

    @staticmethod
    def run_diff_check(cwd: Path | str) -> tuple[bool, str]:
        """Run git diff --check on unstaged and staged changes."""
        res_unstaged = subprocess.run(
            ["git", "diff", "--check"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if res_unstaged.returncode != 0:
            return False, res_unstaged.stdout or res_unstaged.stderr

        res_staged = subprocess.run(
            ["git", "diff", "--check", "--cached"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if res_staged.returncode != 0:
            return False, res_staged.stdout or res_staged.stderr

        return True, "PASS"

    @staticmethod
    def run_validation(cwd: Path | str, run_tests: bool = False) -> tuple[bool, dict[str, Any]]:
        """Run standard validation suite appropriate for the diff."""
        results: dict[str, Any] = {}
        diff_ok, diff_msg = LocalValidator.run_diff_check(cwd)
        results["git_diff_check"] = "PASS" if diff_ok else f"FAIL: {diff_msg}"
        if not diff_ok:
            return False, results

        # Footgun check if script exists
        footguns_script = Path(cwd) / "scripts" / "check-windows-footguns.py"
        if footguns_script.exists():
            fg_res = subprocess.run(
                ["python", str(footguns_script)],
                cwd=str(cwd),
                capture_output=True,
                text=True,
            )
            results["windows_footguns"] = "PASS" if fg_res.returncode == 0 else f"FAIL: {fg_res.stderr or fg_res.stdout}"
            if fg_res.returncode != 0:
                return False, results

        # Secret scanner if script exists
        secret_script = Path(cwd) / "scripts" / "secret_scanner.py"
        if secret_script.exists():
            sec_res = subprocess.run(
                ["python", str(secret_script)],
                cwd=str(cwd),
                capture_output=True,
                text=True,
            )
            results["secret_scanner"] = "PASS" if sec_res.returncode == 0 else f"FAIL: {sec_res.stderr or sec_res.stdout}"
            if sec_res.returncode != 0:
                return False, results

        if run_tests:
            test_res = subprocess.run(
                ["pytest", "-q"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
            )
            results["pytest"] = "PASS" if test_res.returncode == 0 else f"FAIL: {test_res.stdout}"
            if test_res.returncode != 0:
                return False, results

        return True, results
