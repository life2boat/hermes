from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def test_secret_check_exact_range_scans_candidate_tree_and_blocks_bad_input(
    tmp_path: Path,
) -> None:
    try:
        python3_probe = subprocess.run(
            ["bash", "-lc", "python3 --version"],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        pytest.skip("bash is unavailable on this host")
    if python3_probe.returncode != 0:
        pytest.skip("bash python3 runtime is unavailable")

    source_root = Path(__file__).resolve().parents[2]
    repository = tmp_path / "repository"
    scripts_dir = repository / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in (
        "secret_check.sh",
        "secret_scanner.py",
        "git_object_secret_policy.py",
    ):
        shutil.copy2(source_root / "scripts" / name, scripts_dir / name)

    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Synthetic Test")
    _git(repository, "config", "user.email", "synthetic@example.invalid")
    _git(repository, "add", "scripts")
    _git(repository, "commit", "--quiet", "-m", "scanner baseline")
    base_sha = _git(repository, "rev-parse", "HEAD")

    secret_value = "".join(
        (
            "sk-",
            "proj-",
            "Q7m2V9x4",
            "L6p8R3n5",
            "K1s0D4c9",
            "B2h7W6z8",
        )
    )
    assignment_name = "".join(("API", "_KEY"))
    (repository / "candidate.txt").write_text(
        f'{assignment_name} = "{secret_value}"\n',
        encoding="utf-8",
    )
    _git(repository, "add", "candidate.txt")
    _git(repository, "commit", "--quiet", "-m", "synthetic candidate")
    source_sha = _git(repository, "rev-parse", "HEAD")

    environment = dict(os.environ)
    environment["HERMES_SECRET_CHECK_BASE_SHA"] = base_sha
    environment["HERMES_SECRET_CHECK_SOURCE_SHA"] = source_sha
    completed = subprocess.run(
        ["bash", "scripts/secret_check.sh"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "result=SECRET_FOUND" in output
    assert "exact candidate Git object denied" in output
    assert secret_value not in output

    environment["HERMES_SECRET_CHECK_SOURCE_SHA"] = ""
    malformed = subprocess.run(
        ["bash", "scripts/secret_check.sh"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    malformed_output = malformed.stdout + malformed.stderr
    assert malformed.returncode == 2
    assert "GIT_OBJECT_SCAN_RANGE_INVALID" in malformed_output
    assert secret_value not in malformed_output
