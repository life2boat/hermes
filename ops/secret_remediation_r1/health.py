"""Bounded health check for HealBite hermes-bot post-remediation."""

from __future__ import annotations
import subprocess
from ops.secret_remediation_r1.poller_checker import (
    check_exactly_one_poller,
    PollerCheckerError,
)
from ops.secret_remediation_r1.process_identity import DockerBackend


class HealthCheckError(Exception):
    pass


# HEALTH_CHECK_LIMITATION: hermes gateway status proves process/service presence
# and gateway runtime state (connected platforms, exit reasons), but does NOT
# prove end-to-end Telegram message delivery. This remediation's health check
# is bounded to:
#   1. Exactly one container-bound poller process (TRUE_TELEGRAM_POLLER_COUNT=1)
#   2. hermes gateway status reports a non-fatal gateway state
#
# This is sufficient to verify secret source remediation did not break the
# runtime; full application health requires separate smoke testing.
HEALTH_CHECK_LIMITATION = "bounded_to_process_presence_and_gateway_runtime_state"
HEALTH_CHECK_METHOD = "hermes_gateway_status + poller_count"


def check_health(docker: DockerBackend | None = None) -> None:
    """
    Verify bounded runtime health:
    1. Exactly one container-bound poller.
    2. hermes gateway status exits 0 (non-fatal gateway state).
    Raises HealthCheckError on failure.
    """
    # 1. Poller count
    try:
        check_exactly_one_poller(docker=docker)
    except PollerCheckerError as exc:
        raise HealthCheckError(f"Poller check failed: {exc}") from exc

    # 2. Gateway status
    try:
        r = subprocess.run(
            ["hermes", "gateway", "status"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise HealthCheckError("hermes command not found in PATH")
    except subprocess.TimeoutExpired:
        raise HealthCheckError("hermes gateway status timed out")

    if r.returncode != 0:
        raise HealthCheckError(
            f"hermes gateway status exited {r.returncode}: {r.stderr[:200]}"
        )
