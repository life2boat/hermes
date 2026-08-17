"""Canonical Compose command builder for hermes-bot recreation."""

from __future__ import annotations
import os
import re
import subprocess
from ops.secret_remediation_r1.constants import (
    COMPOSE_FILES,
    COMPOSE_PROJECT,
    COMPOSE_WORKDIR,
    LEGACY_IMAGE_REF,
    PROTECTED_NAMES,
)


class ComposeCommandError(Exception):
    pass


def build_recreate_argv() -> list[str]:
    """Return the exact argv list for hermes-bot recreation."""
    argv = ["docker", "compose"]
    for f in COMPOSE_FILES:
        argv.extend(["-f", f])
    argv.extend(["-p", COMPOSE_PROJECT])
    argv.extend([
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--no-build",
        "--pull",
        "never",
        "hermes-bot",
    ])
    return argv


def _resolve_compose_git_sha(workdir: str) -> str:
    """Resolve the real checkout SHA used only for Compose interpolation."""
    try:
        result = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ComposeCommandError("Compose Git SHA resolution failed") from exc
    sha = result.stdout.strip() if result.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ComposeCommandError("Compose Git SHA resolution returned invalid data")
    return sha


def build_clean_env(workdir: str = COMPOSE_WORKDIR) -> dict[str, str]:
    """Return a child-only, fail-closed Compose interpolation environment."""
    env = os.environ.copy()
    for name in PROTECTED_NAMES:
        env.pop(name, None)
    # Verify removal
    for name in PROTECTED_NAMES:
        if name in env:
            raise ComposeCommandError(f"Failed to remove {name} from environment")
    env["HERMES_IMAGE"] = LEGACY_IMAGE_REF
    env["HERMES_GIT_SHA"] = _resolve_compose_git_sha(workdir)
    return env


def run_recreate(workdir: str = COMPOSE_WORKDIR) -> None:
    """Execute the exact Compose recreate command with clean ambient environment."""
    argv = build_recreate_argv()
    env = build_clean_env(workdir)
    result = subprocess.run(argv, cwd=workdir, env=env)
    if result.returncode != 0:
        raise ComposeCommandError(
            f"Compose recreate failed with exit code {result.returncode}"
        )
