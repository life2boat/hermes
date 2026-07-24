from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.name == "nt",
    reason="real POSIX ownership integration is Linux-only",
)
def test_public_evidence_negative_matrix_uses_real_root_security() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    harness = (
        repository_root / "tests/scripts/hermes_production_evidence_negative_harness.py"
    )
    base_command = [
        sys.executable,
        str(harness),
        "--repository-root",
        str(repository_root),
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository_root)
    if os.geteuid() == 0 and os.getegid() == 0:  # windows-footgun: ok
        command = base_command
    else:
        sudo = shutil.which("sudo")
        assert sudo is not None, "real-root evidence matrix requires sudo"
        command = [
            sudo,
            "-n",
            "env",
            f"PYTHONPATH={repository_root}",
            *base_command,
        ]
    result = subprocess.run(
        command,
        cwd=repository_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "automatic_retry_allowed": False,
        "cases_with_missing_delta_assertions": 0,
        "database_delta_asserted_per_case": True,
        "filesystem_delta_asserted_per_case": True,
        "fstat_monkeypatched": False,
        "gid_check_from_pinned_fd": True,
        "migration_container_started": False,
        "no_bytecode_files_created": True,
        "negative_evidence_cases": 41,
        "production_database_used": False,
        "public_execute_main_used": True,
        "public_plan_main_used": True,
        "root_identity_monkeypatched": False,
        "secure_loader_real": True,
        "status": "PASS",
        "synthetic_database_only": True,
        "target_may_have_changed": False,
    }
