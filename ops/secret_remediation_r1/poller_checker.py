"""Verify exactly one hermes gateway poller is active and container-bound."""
from __future__ import annotations
from ops.secret_remediation_r1.process_identity import (
    resolve_poller_pid, DockerBackend, ProcessIdentityError
)


class PollerCheckerError(Exception):
    pass


def check_exactly_one_poller(docker: DockerBackend | None = None) -> int:
    """
    Returns host PID of the single container-bound poller.
    Raises PollerCheckerError on zero or multiple pollers, or identity mismatch.
    """
    try:
        pid, identity = resolve_poller_pid(docker=docker)
    except ProcessIdentityError as exc:
        raise PollerCheckerError(str(exc)) from exc
    return pid
