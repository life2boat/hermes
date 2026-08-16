"""Canonical Compose command builder for hermes-bot recreation."""

from __future__ import annotations
import subprocess
from ops.secret_remediation_r1.constants import (
    COMPOSE_FILES,
    COMPOSE_PROJECT,
    COMPOSE_WORKDIR,
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


def build_clean_env() -> dict[str, str]:
    """Return subprocess environment with all protected names removed."""
    import os

    env = os.environ.copy()
    for name in PROTECTED_NAMES:
        env.pop(name, None)
    # Verify removal
    for name in PROTECTED_NAMES:
        if name in env:
            raise ComposeCommandError(f"Failed to remove {name} from environment")
    return env


def run_recreate(workdir: str = COMPOSE_WORKDIR) -> None:
    """Execute the exact Compose recreate command with clean ambient environment."""
    argv = build_recreate_argv()
    env = build_clean_env()
    result = subprocess.run(argv, cwd=workdir, env=env)
    if result.returncode != 0:
        raise ComposeCommandError(
            f"Compose recreate failed with exit code {result.returncode}"
        )
