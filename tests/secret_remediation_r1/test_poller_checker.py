import pytest
from ops.secret_remediation_r1.poller_checker import check_exactly_one_poller, PollerCheckerError
from ops.secret_remediation_r1.process_identity import ProcessIdentityError

def test_check_exactly_one_poller_success(monkeypatch):
    import ops.secret_remediation_r1.poller_checker
    monkeypatch.setattr(ops.secret_remediation_r1.poller_checker, "resolve_poller_pid", lambda docker: (123, None))
    assert check_exactly_one_poller() == 123

def test_check_exactly_one_poller_fails(monkeypatch):
    import ops.secret_remediation_r1.poller_checker
    def mock_resolve(docker):
        raise ProcessIdentityError("Multiple pollers found")
    monkeypatch.setattr(ops.secret_remediation_r1.poller_checker, "resolve_poller_pid", mock_resolve)
    with pytest.raises(PollerCheckerError, match="Multiple pollers found"):
        check_exactly_one_poller()
