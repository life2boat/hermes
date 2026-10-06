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
            timeout=30,
        )
        if res_unstaged.returncode != 0:
            return False, res_unstaged.stdout or res_unstaged.stderr

        res_staged = subprocess.run(
            ["git", "diff", "--check", "--cached"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
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

        # Invariant: Trusted validator code must be resolved from immutable canonical root
        import os
        import sys

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
        }

        # Footgun check executed strictly from trusted root
        footguns_script = trusted_dir / "scripts" / "check-windows-footguns.py"
        if footguns_script.exists():
            fg_res = subprocess.run(
                [sys.executable, str(footguns_script), str(cwd)],
                cwd=str(cwd),
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

        # Secret scanner executed strictly from trusted root
        secret_script = trusted_dir / "scripts" / "secret_scanner.py"
        if secret_script.exists():
            sec_res = subprocess.run(
                [sys.executable, str(secret_script), str(cwd)],
                cwd=str(cwd),
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
            test_res = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=120,
                env=clean_env,
            )
            results["pytest"] = (
                "PASS" if test_res.returncode == 0 else f"FAIL: {test_res.stdout}"
            )
            if test_res.returncode != 0:
                return False, results

        return True, results
