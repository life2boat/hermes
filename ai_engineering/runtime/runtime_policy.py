"""Runtime activation policy, command policy, and child environment sanitization (PR-13).

Three fail-closed policy layers:

1. :class:`RuntimePolicy` gates whether real processes may run at all
   (SHADOW-only activation; default DISABLED).
2. :func:`validate_runtime_command` rejects shell invocation and
   production/network/mutation command categories (defense in depth;
   authority and workspace isolation remain the primary boundaries).
3. :func:`build_child_environment` constructs a deny-by-default
   allowlisted child environment. Provider API keys, Telegram tokens,
   GitHub tokens, SSH credentials, database URLs, secret-store
   references, and any credential-shaped name can never leak into a
   child process.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath

from ai_engineering.runtime.runtime_contracts import (
    AgentRuntimeError,
    RuntimeBlockingReason,
    RuntimeMode,
)

_MAX_TIMEOUT_SECONDS = 3600.0
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024

_WINDOWS_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "WINDIR",
)

_POSIX_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
)

# Credential-shaped names are denied even if someone allowlists them.
_SECRET_LIKE_RE = re.compile(
    r"(?i)(?:^|_)(?:secret|token|password|passwd|credential|api[_-]?key|"
    r"private[_-]?key|authorization|auth|cookie|bearer|session)(?:$|_)"
)

_FORBIDDEN_BASENAMES = frozenset(
    {
        "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
        "cmd", "cmd.exe", "command", "command.com",
        "powershell", "powershell.exe", "pwsh", "pwsh.exe",
        "wsl", "wsl.exe", "bash.exe",
        "docker", "docker.exe", "docker-compose", "docker-compose.exe",
        "kubectl", "kubectl.exe",
        "ssh", "ssh.exe", "scp", "scp.exe", "sftp", "sftp.exe",
        "rsync", "rsync.exe",
        "curl", "curl.exe", "wget", "wget.exe", "nc", "nc.exe", "netcat",
        "gh", "gh.exe",
        "systemctl", "service", "services.msc",
        "sqlite3", "sqlite3.exe", "psql", "psql.exe", "mysql", "mysql.exe",
        "sudo", "su", "doas",
    }
)

# git is permitted only for read-only / worktree-local subcommands.
_GIT_DENY_SUBCOMMANDS = frozenset(
    {
        "push", "pull", "fetch", "merge", "rebase", "reset", "revert",
        "cherry-pick", "remote", "submodule", "filter-branch", "gc",
        "prune", "bisect", "worktree", "replace", "notes",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """Explicit runtime activation policy.

    The default mode is DISABLED: no real process can be spawned unless
    an explicitly activated SHADOW policy is supplied. Real process
    execution requires SHADOW_LOCAL or SHADOW_WSL; there is no
    production or remote mode in PR-13.
    """

    mode: RuntimeMode = RuntimeMode.DISABLED
    max_timeout_seconds: float = _MAX_TIMEOUT_SECONDS
    max_output_bytes: int = _MAX_OUTPUT_BYTES
    max_concurrent_processes: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode):
            try:
                object.__setattr__(self, "mode", RuntimeMode(str(self.mode)))
            except ValueError as exc:
                raise AgentRuntimeError(
                    RuntimeBlockingReason.RUNTIME_ACTIVATION_DISABLED.value,
                    f"Unknown runtime mode: {self.mode!r}",
                ) from exc
        if self.mode == RuntimeMode.DISABLED:
            return
        for label in ("max_timeout_seconds", "max_output_bytes", "max_concurrent_processes"):
            value = getattr(self, label)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 1:
                raise AgentRuntimeError(
                    RuntimeBlockingReason.RUNTIME_ACTIVATION_DISABLED.value,
                    f"RuntimePolicy.{label} must be a positive number, got {value!r}",
                )

    def requires_mode(self) -> str:
        """Return the ExecutionMode value required by the active policy."""
        if self.mode == RuntimeMode.SHADOW_LOCAL:
            return "LOCAL"
        if self.mode == RuntimeMode.SHADOW_WSL:
            return "WSL"
        raise AgentRuntimeError(
            RuntimeBlockingReason.RUNTIME_ACTIVATION_DISABLED.value,
            "Runtime policy is DISABLED; no execution mode is available",
        )


def validate_runtime_command(argv: tuple[str, ...]) -> None:
    """Validate that an argv tuple is authorized for real process execution.

    argv-based only: shell wrappers are rejected outright, which makes
    shell command-string injection unreachable. Known production,
    remote, network, and secret-manager command categories are denied
    as an additional defense layer.
    """
    if not argv or not all(isinstance(arg, str) and arg for arg in argv):
        raise AgentRuntimeError(
            RuntimeBlockingReason.RUNTIME_COMMAND_NOT_AUTHORIZED.value,
            "Runtime command must be a non-empty argv tuple of non-empty strings",
        )
    raw_head = argv[0]
    head = PureWindowsPath(raw_head.replace("/", "\\")).name.lower()
    head = head.removesuffix(".exe")
    if head in _FORBIDDEN_BASENAMES:
        raise AgentRuntimeError(
            RuntimeBlockingReason.RUNTIME_COMMAND_NOT_AUTHORIZED.value,
            f"Runtime command head {raw_head!r} is not authorized",
        )
    if head == "git" and len(argv) > 1:
        sub = argv[1].strip().lower()
        if sub in _GIT_DENY_SUBCOMMANDS:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_COMMAND_NOT_AUTHORIZED.value,
                f"git subcommand {sub!r} is not authorized in the runtime",
            )


def name_is_secret_like(name: str) -> bool:
    """Return True when an environment variable name is credential-shaped."""
    return bool(_SECRET_LIKE_RE.search(str(name).strip()))


def build_child_environment(
    parent_env: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
    *,
    target_platform: str | None = None,
) -> dict[str, str]:
    """Build an explicit deny-by-default child process environment.

    Only allowlisted variable names present in ``parent_env`` are
    copied. ``extra`` entries are applied only when their names are not
    credential-shaped. Provider keys, Telegram/GitHub tokens, SSH
    credentials, database URLs, and secret-store references are absent
    from the result by construction.
    """
    resolved_parent = dict(os.environ) if parent_env is None else dict(parent_env)
    platform = (target_platform or os.name).lower()
    allowlist = _WINDOWS_ENV_ALLOWLIST if platform.startswith("win") else _POSIX_ENV_ALLOWLIST

    child: dict[str, str] = {}
    for name in allowlist:
        value = resolved_parent.get(name)
        if value is not None and not name_is_secret_like(name):
            child[name] = value

    for name, value in (extra or {}).items():
        if name_is_secret_like(name):
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_ENVIRONMENT_NOT_AUTHORIZED.value,
                f"Refusing to inject credential-shaped environment variable: {name!r}",
            )
        child[str(name)] = str(value)
    return child
