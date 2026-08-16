"""Post-remediation source invariant checker.

Verifies that the remediation outcome matches the approved design:
  - The legacy mixed .env bytes are unchanged (not mutated during remediation).
  - The runtime env file exists and contains zero protected NAME assignments.
  - The secret file exists, is a regular file owned by root with mode 0600.
  - The effective protected NAME set after remediation equals the pre-remediation
    protected NAME set (no secret names added or removed).
  - DASHSCOPE_API_KEY presence is preserved exactly.

NOTE: We do NOT assert ``legacy_env_keys == runtime_keys | secret_keys``
because the legacy mixed .env is not necessarily the only historical source
of protected names in the production environment.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field

from ops.secret_remediation_r1.constants import PROTECTED_NAMES
from ops.secret_remediation_r1.safe_fs import safe_open_source


class SourceInvariantError(Exception):
    pass


def _parse_env_keys(content: bytes) -> frozenset[str]:
    """Extract variable names from env-file bytes.

    Ignores blank lines and comment lines (starting with ``#`` after stripping).
    Returns a frozenset of variable name strings.
    """
    keys: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        if b"=" in stripped:
            key_bytes = stripped.split(b"=", 1)[0].strip()
            keys.add(key_bytes.decode("utf-8", errors="ignore"))
    return frozenset(keys)


def _read_env_keys(path: str) -> frozenset[str]:
    """Read variable names from a plain env file using safe_open_source."""
    try:
        fd, _ = safe_open_source(path)
        with os.fdopen(fd, "rb") as f:
            content = f.read()
    except Exception as exc:
        raise SourceInvariantError(f"Failed to read {path}: {exc}")
    return _parse_env_keys(content)


@dataclass
class SourceState:
    """Pre-remediation state captured before any mutation.

    Attributes:
        legacy_env_bytes: Exact bytes of the legacy mixed .env file.
        dashscope_present_before: Whether DASHSCOPE_API_KEY was present in
            the legacy .env before remediation.
        legacy_env_name_set: Frozenset of all variable names in the legacy .env.
            Derived automatically if not supplied.
        pre_remediation_effective_protected_name_set: The set of protected NAME
            assignments that were present before remediation. Derived from
            ``legacy_env_bytes`` if not supplied.
    """

    legacy_env_bytes: bytes
    dashscope_present_before: bool
    legacy_env_name_set: frozenset[str] = field(default_factory=frozenset)
    pre_remediation_effective_protected_name_set: frozenset[str] = field(
        default_factory=frozenset
    )

    def __post_init__(self) -> None:
        parsed = _parse_env_keys(self.legacy_env_bytes)
        if not self.legacy_env_name_set:
            object.__setattr__(self, "legacy_env_name_set", parsed)
        if not self.pre_remediation_effective_protected_name_set:
            object.__setattr__(
                self,
                "pre_remediation_effective_protected_name_set",
                parsed & PROTECTED_NAMES,
            )


def _verify_effective_compose(compose_files: list[str], workdir: str) -> None:
    import subprocess
    import json
    import os
    from ops.secret_remediation_r1.constants import PROTECTED_NAMES

    cmd = ["docker", "compose"]
    for f in compose_files:
        cmd.extend(["-f", f])
    cmd.extend(["config", "--format", "json"])

    env = os.environ.copy()
    for name in PROTECTED_NAMES:
        env.pop(name, None)

    r = subprocess.run(
        cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        raise SourceInvariantError(f"Failed to compile effective compose: {r.stderr}")
    try:
        data = json.loads(r.stdout)
    except Exception as exc:
        raise SourceInvariantError(f"Failed to parse effective compose JSON: {exc}")

    services = data.get("services", {})
    bot = services.get("hermes-bot")
    if not bot:
        raise SourceInvariantError("hermes-bot service missing in effective compose")

    env_files = bot.get("env_file", [])
    env_file_paths = []
    for ef in env_files:
        if isinstance(ef, str):
            env_file_paths.append(ef)
        elif isinstance(ef, dict) and "path" in ef:
            env_file_paths.append(ef["path"])

    if "/home/hermes/.hermes/.env" in env_file_paths:
        raise SourceInvariantError("Legacy .env is still active in env_file")
    if "/etc/hermes/hermes-runtime.env" not in env_file_paths:
        raise SourceInvariantError("runtime env missing in env_file")
    if "/etc/hermes/hermes-production.env" not in env_file_paths:
        raise SourceInvariantError("production secret env missing in env_file")

    env_vars = bot.get("environment", {})
    if isinstance(env_vars, list):
        keys = set()
        for v in env_vars:
            if "=" in v:
                keys.add(v.split("=", 1)[0])
            else:
                keys.add(v)
    elif isinstance(env_vars, dict):
        keys = set(env_vars.keys())
    else:
        keys = set()

    protected_inline = keys & PROTECTED_NAMES
    if protected_inline:
        raise SourceInvariantError(
            f"Protected inline environment bindings present: {protected_inline}"
        )


def verify_source_invariant(
    prestate: SourceState,
    legacy_env_path: str,
    runtime_env_path: str,
    secret_file_path: str,
    compose_files: list[str] | None = None,
    compose_workdir: str | None = None,
) -> None:
    """Verify all post-remediation source invariants.

    Raises SourceInvariantError on any violation.
    """
    if compose_files and compose_workdir:
        _verify_effective_compose(compose_files, compose_workdir)

    # A. Legacy .env bytes must be unchanged.
    try:
        fd, _ = safe_open_source(legacy_env_path)
        with os.fdopen(fd, "rb") as f:
            current_legacy = f.read()
    except Exception as exc:
        raise SourceInvariantError(f"Legacy env read failed: {exc}")

    if current_legacy != prestate.legacy_env_bytes:
        raise SourceInvariantError("Legacy .env was mutated")

    # B. Runtime env file: must exist, be a regular file, and contain no protected names.
    try:
        rt_st = os.lstat(runtime_env_path)
    except OSError as exc:
        raise SourceInvariantError(f"Runtime env missing: {exc}")
    if stat.S_ISLNK(rt_st.st_mode):
        raise SourceInvariantError("Runtime env is a symlink")
    if not stat.S_ISREG(rt_st.st_mode):
        raise SourceInvariantError("Runtime env is not a regular file")

    runtime_keys = _read_env_keys(runtime_env_path)
    protected_in_runtime = runtime_keys & PROTECTED_NAMES
    if protected_in_runtime:
        raise SourceInvariantError(
            f"Protected names in runtime env: {protected_in_runtime}"
        )

    # C. Secret file: must exist, be regular, uid=0, mode=0600 (on POSIX).
    try:
        secret_st = os.lstat(secret_file_path)
    except OSError as exc:
        raise SourceInvariantError(f"Secret file missing: {exc}")
    if stat.S_ISLNK(secret_st.st_mode):
        raise SourceInvariantError("Secret file is a symlink")
    if not stat.S_ISREG(secret_st.st_mode):
        raise SourceInvariantError("Secret file is not a regular file")
    if os.name != "nt":
        if secret_st.st_uid != 0:
            raise SourceInvariantError(
                f"Secret file uid={secret_st.st_uid}, expected 0"
            )
        if (secret_st.st_mode & 0o777) != 0o600:
            raise SourceInvariantError(
                f"Secret file mode={oct(secret_st.st_mode & 0o777)}, expected 0600"
            )

    # D. The protected NAME set after remediation must equal the pre-remediation set.
    #    We compare by name only — values are never read or compared here.
    secret_keys = _read_env_keys(secret_file_path)
    post_protected_names = secret_keys & PROTECTED_NAMES

    if post_protected_names != prestate.pre_remediation_effective_protected_name_set:
        raise SourceInvariantError(
            f"Protected name set changed after remediation: "
            f"before={prestate.pre_remediation_effective_protected_name_set!r}, "
            f"after={post_protected_names!r}"
        )

    # E. DASHSCOPE_API_KEY presence must be preserved exactly.
    dashscope_after = "DASHSCOPE_API_KEY" in secret_keys
    if prestate.dashscope_present_before != dashscope_after:
        raise SourceInvariantError(
            f"DASHSCOPE_API_KEY presence changed: "
            f"before={prestate.dashscope_present_before}, after={dashscope_after}"
        )
