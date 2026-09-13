"""Task 7.8 CLI Integration — Clean Import Tests.

Verifies that all four import orderings between ai_engineering.supervisor.ci_provider
and ai_engineering.supervisor.autonomous_run succeed in fresh Python subprocesses.
The circular import was broken in Task 7.8 CLI Integration.
"""
from __future__ import annotations

import subprocess
import sys
import pytest


def _run_import(import_statements: str) -> tuple[int, str]:
    """Run import statement(s) in a fresh Python process and return (returncode, output)."""
    result = subprocess.run(
        [sys.executable, "-c", import_statements],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout + result.stderr


class TestCleanImports:
    """All four import orderings must succeed in fresh Python processes."""

    def test_import_ci_provider_first(self):
        """Import ci_provider before autonomous_run."""
        code, out = _run_import(
            "import ai_engineering.supervisor.ci_provider; "
            "import ai_engineering.supervisor.autonomous_run; "
            "print('OK')"
        )
        assert code == 0, f"Import failed:\n{out}"
        assert "OK" in out

    def test_import_autonomous_run_first(self):
        """Import autonomous_run before ci_provider."""
        code, out = _run_import(
            "import ai_engineering.supervisor.autonomous_run; "
            "import ai_engineering.supervisor.ci_provider; "
            "print('OK')"
        )
        assert code == 0, f"Import failed:\n{out}"
        assert "OK" in out

    def test_import_ci_provider_standalone(self):
        """Import ci_provider in isolation."""
        code, out = _run_import(
            "import ai_engineering.supervisor.ci_provider; "
            "print('OK')"
        )
        assert code == 0, f"Import failed:\n{out}"
        assert "OK" in out

    def test_import_autonomous_run_standalone(self):
        """Import autonomous_run in isolation."""
        code, out = _run_import(
            "import ai_engineering.supervisor.autonomous_run; "
            "print('OK')"
        )
        assert code == 0, f"Import failed:\n{out}"
        assert "OK" in out

    def test_ci_provider_defines_local_cistatus_provider(self):
        """ci_provider defines its own CIStatusProvider (no longer imports from autonomous_run)."""
        code, out = _run_import(
            "from ai_engineering.supervisor.ci_provider import CIStatusProvider; "
            "print(CIStatusProvider.__module__)"
        )
        assert code == 0, f"Import failed:\n{out}"
        # Must be defined in ci_provider module
        assert "ci_provider" in out

    def test_autonomous_run_defines_cistatus_provider(self):
        """autonomous_run still exports CIStatusProvider for backward compatibility."""
        code, out = _run_import(
            "from ai_engineering.supervisor.autonomous_run import CIStatusProvider; "
            "print('CIStatusProvider OK')"
        )
        assert code == 0, f"Import failed:\n{out}"
        assert "CIStatusProvider OK" in out
