import pytest
from ops.secret_remediation_r1.poller_checker import (
    check_exactly_one_poller,
    PollerCheckerError,
)
from ops.secret_remediation_r1.process_identity import ProcessIdentityError


def test_check_exactly_one_poller_success(monkeypatch):
    import ops.secret_remediation_r1.poller_checker

    monkeypatch.setattr(
        ops.secret_remediation_r1.poller_checker,
        "resolve_poller_pid",
        lambda docker: (123, None),
    )
    assert check_exactly_one_poller() == 123


def test_check_exactly_one_poller_fails(monkeypatch):
    import ops.secret_remediation_r1.poller_checker

    def mock_resolve(docker):
        raise ProcessIdentityError("Multiple pollers found")

    monkeypatch.setattr(
        ops.secret_remediation_r1.poller_checker, "resolve_poller_pid", mock_resolve
    )
    with pytest.raises(PollerCheckerError, match="Multiple pollers found"):
        check_exactly_one_poller(sleep=lambda _seconds: None)


def test_check_exactly_one_poller_retries_only_startup_absence(monkeypatch):
    import ops.secret_remediation_r1.poller_checker

    outcomes = [
        ProcessIdentityError("No hermes gateway poller found"),
        ProcessIdentityError("No hermes gateway poller found"),
        (321, None),
    ]
    sleeps = []

    def mock_resolve(docker):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    ticks = iter([0.0, 0.0, 0.5, 0.5, 1.0])
    monkeypatch.setattr(
        ops.secret_remediation_r1.poller_checker, "resolve_poller_pid", mock_resolve
    )
    assert (
        check_exactly_one_poller(
            timeout_seconds=2,
            interval_seconds=0.5,
            sleep=sleeps.append,
            monotonic=lambda: next(ticks),
        )
        == 321
    )
    assert sleeps == [0.5, 0.5]


def test_check_exactly_one_poller_times_out(monkeypatch):
    import ops.secret_remediation_r1.poller_checker

    monkeypatch.setattr(
        ops.secret_remediation_r1.poller_checker,
        "resolve_poller_pid",
        lambda docker: (_ for _ in ()).throw(
            ProcessIdentityError("No hermes gateway poller found")
        ),
    )
    ticks = iter([0.0, 0.0, 1.0])
    with pytest.raises(PollerCheckerError, match="Timed out"):
        check_exactly_one_poller(
            timeout_seconds=1,
            interval_seconds=1,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(ticks),
        )


def test_identity_mismatch_is_not_retried(monkeypatch):
    import ops.secret_remediation_r1.poller_checker

    monkeypatch.setattr(
        ops.secret_remediation_r1.poller_checker,
        "resolve_poller_pid",
        lambda docker: (_ for _ in ()).throw(
            ProcessIdentityError("PID namespace mismatch")
        ),
    )
    with pytest.raises(PollerCheckerError, match="PID namespace mismatch"):
        check_exactly_one_poller(sleep=lambda _seconds: pytest.fail("must not retry"))
