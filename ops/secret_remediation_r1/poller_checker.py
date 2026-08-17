"""Verify exactly one hermes gateway poller is active and container-bound."""

from __future__ import annotations
import time
from ops.secret_remediation_r1.process_identity import (
    resolve_poller_pid,
    DockerBackend,
    ProcessIdentityError,
)


class PollerCheckerError(Exception):
    pass


def check_exactly_one_poller(
    docker: DockerBackend | None = None,
    *,
    timeout_seconds: float = 10.0,
    interval_seconds: float = 1.0,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> int:
    """
    Returns host PID of the single container-bound poller.
    Raises PollerCheckerError on zero or multiple pollers, or identity mismatch.
    """
    if timeout_seconds < 0 or interval_seconds <= 0:
        raise PollerCheckerError("Invalid poller convergence bounds")
    deadline = monotonic() + timeout_seconds
    while True:
        try:
            pid, _identity = resolve_poller_pid(docker=docker)
            return pid
        except ProcessIdentityError as exc:
            if str(exc) != "No hermes gateway poller found":
                raise PollerCheckerError(str(exc)) from exc
            now = monotonic()
            if now >= deadline:
                raise PollerCheckerError(
                    "Timed out waiting for exactly one container-bound poller"
                ) from exc
            sleep(min(interval_seconds, deadline - now))
