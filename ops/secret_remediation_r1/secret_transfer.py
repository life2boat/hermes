"""Secure secret extraction from live container environment to /etc/hermes/hermes-production.env."""

from __future__ import annotations
from typing import Protocol
from ops.secret_remediation_r1.constants import PROTECTED_NAMES, REQUIRED_SECRET_NAMES
from ops.secret_remediation_r1.safe_fs import publish_file, SafeFsError
from ops.secret_remediation_r1.process_identity import (
    DockerBackend,
    resolve_poller_pid,
    read_poller_environ,
    ProcessIdentityError,
)


class SecretTransferError(Exception):
    pass


_UNSAFE_BYTES = (b"\n", b"\r", b"\x00")


def parse_protected_env(env_bytes: bytes) -> list[bytes]:
    """
    Parse NUL-delimited environ bytes, extracting canonical protected entries.
    Returns list of b"KEY=VALUE\n" lines.
    Raises SecretTransferError on policy violations.
    """
    entries = env_bytes.split(b"\x00")
    seen_protected: dict[str, bool] = {}
    result: list[bytes] = []

    for entry in entries:
        if not entry or b"=" not in entry:
            continue
        key_bytes, value_bytes = entry.split(b"=", 1)
        try:
            key = key_bytes.decode("ascii")
        except UnicodeDecodeError:
            continue

        if key not in PROTECTED_NAMES:
            continue

        # Check for duplicates
        if key in seen_protected:
            raise SecretTransferError(f"Duplicate protected name: {key}")
        seen_protected[key] = True

        # Reject empty values
        if not value_bytes:
            raise SecretTransferError(f"Empty value for protected secret: {key}")

        # Reject unsafe byte sequences
        for unsafe in _UNSAFE_BYTES:
            if unsafe in value_bytes:
                raise SecretTransferError(f"Unsafe bytes in secret {key}")

        result.append(entry + b"\n")

    # Require mandatory names
    for required in REQUIRED_SECRET_NAMES:
        if required not in seen_protected:
            raise SecretTransferError(f"{required} not found in container environment")

    return result


def transfer_secrets(
    destination: str,
    docker: DockerBackend | None = None,
) -> None:
    """
    Extract protected secrets from the live container and publish atomically.
    NEVER prints, logs, or returns secret values.
    """
    pid, identity = resolve_poller_pid(docker=docker)
    env_bytes = read_poller_environ(pid, identity, docker=docker)

    secret_lines = parse_protected_env(env_bytes)
    content = b"".join(secret_lines)

    try:
        publish_file(
            destination,
            content,
            mode=0o600,
            uid=0,
            require_uid=0,
            require_mode=0o600,
        )
    except SafeFsError as exc:
        raise SecretTransferError(f"Publication failed: {exc}") from exc
