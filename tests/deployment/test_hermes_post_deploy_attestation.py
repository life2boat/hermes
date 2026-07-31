from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_production_deploy as deploy  # noqa: E402
runtime = deploy.attestation


IMAGE_OLD = "sha256:" + "a" * 64
IMAGE_NEW = "sha256:" + "b" * 64
IMAGE_OTHER = "sha256:" + "c" * 64
REVISION_OLD = "d" * 40
REVISION_NEW = "e" * 40
TOKEN = "synthetic-secret-value"
FEATURES = {
    "HEALBITE_HOUSEHOLDS_ENABLED": "false",
    "HEALBITE_HOUSEHOLDS_ALLOWLIST": "",
    "HEALBITE_SHOPPING_LIST_ENABLED": "false",
    "HEALBITE_SHOPPING_LIST_ALLOWLIST": "",
}


def _completed(
    argv,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE parent(id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE child(
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id)
            );
            CREATE INDEX child_parent_idx ON child(parent_id);
            CREATE TRIGGER parent_value_guard
            BEFORE UPDATE OF value ON parent
            BEGIN
              SELECT CASE WHEN NEW.value = '' THEN RAISE(ABORT, 'empty') END;
            END;
            INSERT INTO parent(id, value) VALUES (1, 'stable');
            INSERT INTO child(id, parent_id) VALUES (1, 1);
            """
        )
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "healbite.db"
    _create_database(path)
    return path


@pytest.fixture
def policy() -> runtime.RuntimeAttestationPolicy:
    return deploy.load_contract().attestation_policy


def _hermes_record(
    database_path: Path,
    *,
    container_id: str,
    image_id: str,
    revision: str,
    state: str = "running",
    restart_count: int = 0,
    started_at: str = "2026-07-31T10:00:00Z",
    env_changes: dict[str, str | None] | None = None,
    mount_source: Path | None = None,
) -> dict[str, object]:
    environment: dict[str, str] = {**FEATURES, "TELEGRAM_BOT_TOKEN": TOKEN}
    for name, value in (env_changes or {}).items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return {
        "Id": container_id,
        "Image": image_id,
        "Created": "2026-07-31T09:59:59Z",
        "RestartCount": restart_count,
        "State": {"Status": state, "StartedAt": started_at},
        "Config": {
            "Labels": {"org.opencontainers.image.revision": revision},
            "Env": [f"{name}={value}" for name, value in environment.items()],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(mount_source or database_path),
                "Destination": "/home/hermes/healbite.db",
                "RW": True,
            }
        ],
    }


def _qdrant_record(
    *,
    container_id: str = "qdrant-container",
    image_id: str = IMAGE_OTHER,
    restart_count: int = 0,
    started_at: str = "2026-07-30T00:00:00Z",
    mount_source: str = "/var/lib/hermes/qdrant",
) -> dict[str, object]:
    return {
        "Id": container_id,
        "Image": image_id,
        "Created": "2026-07-30T00:00:00Z",
        "RestartCount": restart_count,
        "State": {"Status": "running", "StartedAt": started_at},
        "Config": {"Labels": {}, "Env": []},
        "Mounts": [
            {
                "Type": "bind",
                "Source": mount_source,
                "Destination": "/qdrant/storage",
                "RW": True,
            }
        ],
    }


class SyntheticRunner:
    def __init__(
        self,
        policy: runtime.RuntimeAttestationPolicy,
        *,
        hermes_records: list[dict[str, object]],
        qdrant_records: list[dict[str, object]] | None = None,
    ) -> None:
        self.policy = policy
        self.hermes_records = hermes_records
        self.qdrant_records = qdrant_records or [_qdrant_record()]
        self.logs = ""
        self.telegram_pass = True
        self.gateway_output = "GATEWAY_NO_SEND_SMOKE=PASS\nPROVIDER_REQUESTS=0\n"
        self.calls: list[tuple[str, ...]] = []

    @staticmethod
    def _next(records: list[dict[str, object]]) -> dict[str, object]:
        return records.pop(0) if len(records) > 1 else records[0]

    def __call__(self, argv, **_kwargs):
        command = tuple(str(item) for item in argv)
        self.calls.append(command)
        if command == ("docker", "inspect", "hermes-bot"):
            return _completed(argv, stdout=json.dumps([self._next(self.hermes_records)]))
        if command == ("docker", "inspect", "qdrant"):
            return _completed(argv, stdout=json.dumps([self._next(self.qdrant_records)]))
        if command[:2] == ("docker", "logs"):
            return _completed(argv, stdout=self.logs)
        if command == self.policy.telegram_health_command:
            return _completed(
                argv,
                returncode=0 if self.telegram_pass else 1,
                stdout="TELEGRAM_HEALTH=PASS\n" if self.telegram_pass else "",
                stderr="identity-shaped-response-that-must-not-escape",
            )
        if command == self.policy.gateway_no_send_command:
            return _completed(argv, stdout=self.gateway_output)
        raise AssertionError(f"unexpected synthetic command: {command!r}")


def _capture(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
    runner: SyntheticRunner,
) -> runtime.RuntimeBaseline:
    return runtime.capture_pre_mutation_baseline(
        policy,
        hermes_service="hermes-bot",
        qdrant_service="qdrant",
        database_path=database_path,
        database_target=Path("/home/hermes/healbite.db"),
        revision_label="org.opencontainers.image.revision",
        expected_feature_gates=FEATURES,
        protected_secret_names=("TELEGRAM_BOT_TOKEN",),
        run=runner,
    )


def _post(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
    runner: SyntheticRunner,
    baseline: runtime.RuntimeBaseline,
) -> runtime.PostDeployAttestation:
    return runtime.post_deploy_attestation(
        policy,
        baseline,
        hermes_service="hermes-bot",
        qdrant_service="qdrant",
        database_path=database_path,
        revision_label="org.opencontainers.image.revision",
        target_image_id=IMAGE_NEW,
        target_revision=REVISION_NEW,
        expected_feature_gates=FEATURES,
        protected_secret_names=("TELEGRAM_BOT_TOKEN",),
        run=runner,
        sleep=lambda _seconds: None,
    )


def _success_runner(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
) -> SyntheticRunner:
    old = _hermes_record(
        database_path,
        container_id="old-container",
        image_id=IMAGE_OLD,
        revision=REVISION_OLD,
    )
    new = _hermes_record(
        database_path,
        container_id="new-container",
        image_id=IMAGE_NEW,
        revision=REVISION_NEW,
        started_at="2026-07-31T10:01:00Z",
    )
    return SyntheticRunner(
        policy,
        hermes_records=[old, new, new, new],
        qdrant_records=[_qdrant_record(), _qdrant_record()],
    )


def test_complete_runtime_attestation_success_is_bounded_and_secret_safe(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
) -> None:
    runner = _success_runner(policy, database_path)
    baseline = _capture(policy, database_path, runner)
    result = _post(policy, database_path, runner, baseline)

    assert result.stability_samples == 3
    assert result.provider_request_count == 0
    assert result.database_delta_result == "UNCHANGED"
    assert TOKEN not in repr(baseline)
    log_calls = [call for call in runner.calls if call[:2] == ("docker", "logs")]
    assert len(log_calls) == 1
    assert "--since" in log_calls[0]
    assert log_calls[0][-2:] == (str(policy.startup_log_tail_lines), "hermes-bot")


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ({"image_id": IMAGE_OTHER}, "HERMES_IMAGE_MISMATCH"),
        ({"revision": REVISION_OLD}, "HERMES_REVISION_MISMATCH"),
        ({"state": "exited"}, "HERMES_NOT_RUNNING"),
        ({"restart_count": 1}, "HERMES_RESTART_COUNT_CHANGED"),
        ({"mount_source": Path("/tmp/other.db")}, "HERMES_MOUNT_SET_CHANGED"),
        (
            {"env_changes": {"HEALBITE_HOUSEHOLDS_ENABLED": "true"}},
            "FEATURE_GATE_DELTA",
        ),
        (
            {"env_changes": {"HEALBITE_HOUSEHOLDS_ALLOWLIST": "member"}},
            "ALLOWLIST_DELTA",
        ),
        (
            {"env_changes": {"TELEGRAM_BOT_TOKEN": "changed-secret"}},
            "SECRET_FINGERPRINT_DELTA",
        ),
        (
            {"env_changes": {"HEALBITE_UNKNOWN_ENABLED": "true"}},
            "UNKNOWN_FEATURE_VARIABLE",
        ),
    ),
)
def test_runtime_delta_boundaries_fail_closed(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
    change: dict[str, object],
    code: str,
) -> None:
    runner = _success_runner(policy, database_path)
    baseline = _capture(policy, database_path, runner)
    parameters: dict[str, object] = {
        "container_id": "new-container",
        "image_id": IMAGE_NEW,
        "revision": REVISION_NEW,
        "started_at": "2026-07-31T10:01:00Z",
        **change,
    }
    bad = _hermes_record(
        database_path,
        **parameters,
    )
    runner.hermes_records = [bad, bad, bad]
    with pytest.raises(runtime.RuntimeAttestationError, match=code):
        _post(policy, database_path, runner, baseline)


def test_late_crash_is_detected_across_multiple_samples(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
) -> None:
    runner = _success_runner(policy, database_path)
    baseline = _capture(policy, database_path, runner)
    first = _hermes_record(
        database_path,
        container_id="new-container",
        image_id=IMAGE_NEW,
        revision=REVISION_NEW,
        started_at="2026-07-31T10:01:00Z",
    )
    crashed = replace_record(first, Id="replacement-container")
    runner.hermes_records = [first, crashed, crashed]
    with pytest.raises(runtime.RuntimeAttestationError, match="HERMES_LATE_CRASH"):
        _post(policy, database_path, runner, baseline)


def replace_record(record: dict[str, object], **changes: object) -> dict[str, object]:
    return {**record, **changes}


@pytest.mark.parametrize(
    ("classification", "line"),
    (
        ("PYTHON_TRACEBACK", "Traceback (most recent call last):"),
        ("UNHANDLED_EXCEPTION", "unhandled exception while starting"),
        ("FATAL_STARTUP_FAILURE", "fatal startup initialization"),
        ("DATABASE_FAILURE", "sqlite integrity failed"),
        ("AUTHENTICATION_FAILURE", "authentication failed"),
        ("SECRET_INVALID", "token is invalid"),
        ("PROVIDER_FALLBACK", "provider fallback selected"),
        ("GATEWAY_INITIALIZATION_FAILURE", "gateway initialization failed"),
        ("TELEGRAM_INITIALIZATION_FAILURE", "telegram polling initialization error"),
        ("CRASH_LOOP", "crash-loop observed"),
    ),
)
def test_forbidden_startup_log_classifications_are_sanitized(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
    classification: str,
    line: str,
) -> None:
    runner = _success_runner(policy, database_path)
    baseline = _capture(policy, database_path, runner)
    runner.logs = line
    with pytest.raises(runtime.RuntimeAttestationError, match=classification) as error:
        _post(policy, database_path, runner, baseline)
    assert line not in str(error.value)
    assert TOKEN not in str(error.value)


def test_startup_log_byte_bound_is_enforced(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
) -> None:
    runner = _success_runner(policy, database_path)
    baseline = _capture(policy, database_path, runner)
    runner.logs = "x" * (policy.startup_log_max_bytes + 1)
    with pytest.raises(runtime.RuntimeAttestationError, match="STARTUP_LOG_BOUND_EXCEEDED"):
        _post(policy, database_path, runner, baseline)


@pytest.mark.parametrize(
    ("telegram_pass", "gateway_output", "code"),
    (
        (False, "GATEWAY_NO_SEND_SMOKE=PASS\nPROVIDER_REQUESTS=0\n", "TELEGRAM_CONNECTIVITY_FAILED"),
        (True, "", "GATEWAY_SMOKE_FAILED"),
        (
            True,
            "GATEWAY_NO_SEND_SMOKE=PASS\nPROVIDER_REQUESTS=1\n",
            "GATEWAY_SMOKE_FAILED",
        ),
    ),
)
def test_telegram_gateway_and_provider_attempt_fail_closed(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
    telegram_pass: bool,
    gateway_output: str,
    code: str,
) -> None:
    runner = _success_runner(policy, database_path)
    baseline = _capture(policy, database_path, runner)
    runner.telegram_pass = telegram_pass
    runner.gateway_output = gateway_output
    with pytest.raises(runtime.RuntimeAttestationError, match=code):
        _post(policy, database_path, runner, baseline)


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"schema_fingerprint": "0" * 64}, "DATABASE_SCHEMA_CHANGED"),
        ({"integrity_ok": False}, "DATABASE_INTEGRITY_FAILED"),
        ({"foreign_key_violations": 1}, "DATABASE_FOREIGN_KEY_VIOLATION"),
        ({"main_fingerprint": "1" * 64}, "DATABASE_DATA_DELTA"),
        ({"user_version": 1}, "DATABASE_USER_VERSION_CHANGED"),
    ),
)
def test_database_contract_failures(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    code: str,
) -> None:
    runner = _success_runner(policy, database_path)
    baseline = _capture(policy, database_path, runner)
    after = replace(baseline.database, **changes)
    monkeypatch.setattr(runtime, "capture_database", lambda _path: after)
    with pytest.raises(runtime.RuntimeAttestationError, match=code):
        _post(policy, database_path, runner, baseline)


@pytest.mark.parametrize(
    ("qdrant_change", "code"),
    (
        ({"container_id": "changed"}, "QDRANT_CONTAINER_CHANGED"),
        ({"image_id": IMAGE_NEW}, "QDRANT_IMAGE_CHANGED"),
        ({"restart_count": 1}, "QDRANT_RESTART_COUNT_CHANGED"),
        ({"started_at": "2026-07-31T00:00:00Z"}, "QDRANT_CREATED_TIME_CHANGED"),
        ({"mount_source": "/other/qdrant"}, "QDRANT_MOUNT_SET_CHANGED"),
    ),
)
def test_qdrant_non_interference_fields(
    policy: runtime.RuntimeAttestationPolicy,
    database_path: Path,
    qdrant_change: dict[str, object],
    code: str,
) -> None:
    runner = _success_runner(policy, database_path)
    baseline = _capture(policy, database_path, runner)
    runner.qdrant_records = [_qdrant_record(**qdrant_change)]
    with pytest.raises(runtime.RuntimeAttestationError, match=code):
        _post(policy, database_path, runner, baseline)


def _post_result(policy: runtime.RuntimeAttestationPolicy) -> runtime.PostDeployAttestation:
    return runtime.PostDeployAttestation(
        observed_at="2026-07-31T10:02:00+00:00",
        stability_samples=policy.stability_sample_count,
        startup_log_classifications=(),
        database_structural_result="PASS",
        database_delta_result="UNCHANGED",
        feature_gate_delta="UNCHANGED",
        allowlist_delta="UNCHANGED",
        secret_delta="UNCHANGED",
        qdrant_result="UNCHANGED",
        telegram_health="PASS",
        gateway_health="PASS",
        provider_request_count=0,
    )


def test_evidence_is_bounded_mode_0600_and_contains_no_raw_identity(
    policy: runtime.RuntimeAttestationPolicy,
    tmp_path: Path,
) -> None:
    runtime_directory = tmp_path / "run"
    runtime_directory.mkdir(mode=0o700)
    path = runtime.write_evidence(
        policy,
        runtime_directory=runtime_directory,
        target_revision=REVISION_NEW,
        target_image_id=IMAGE_NEW,
        previous_image_id=IMAGE_OLD,
        operation_status="PASS",
        post_result=_post_result(policy),
        original_error=None,
        rollback_attempted=False,
        rollback_result="NOT_ATTEMPTED",
        rollback_error=None,
    )
    payload = path.read_bytes()
    assert len(payload) <= policy.evidence_max_bytes
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert TOKEN.encode() not in payload
    assert b"chat_id" not in payload
    assert b"qdrant-container" not in payload


def test_evidence_rejects_unsafe_classification(
    policy: runtime.RuntimeAttestationPolicy,
    tmp_path: Path,
) -> None:
    runtime_directory = tmp_path / "run"
    runtime_directory.mkdir(mode=0o700)
    with pytest.raises(runtime.RuntimeAttestationError, match="EVIDENCE_SANITIZATION_FAILED"):
        runtime.write_evidence(
            policy,
            runtime_directory=runtime_directory,
            target_revision=REVISION_NEW,
            target_image_id=IMAGE_NEW,
            previous_image_id=IMAGE_OLD,
            operation_status="FAIL",
            post_result=None,
            original_error="unsafe identity value",
            rollback_attempted=True,
            rollback_result="FAIL",
            rollback_error="SAFE_CODE",
        )


def test_policy_rejects_unknown_missing_and_duplicate_fields() -> None:
    manifest_path = REPO_ROOT / "deploy" / "hermes-production.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["attestation"]["unknown"] = True
    with pytest.raises(deploy.DeploymentContractError, match="attestation-policy-fields"):
        deploy.load_contract(manifest_bytes=json.dumps(raw).encode())

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    del raw["attestation"]["evidence_max_bytes"]
    with pytest.raises(deploy.DeploymentContractError, match="attestation-policy-fields"):
        deploy.load_contract(manifest_bytes=json.dumps(raw).encode())

    text = manifest_path.read_text(encoding="utf-8")
    duplicate = text.replace('"version": 2,', '"version": 2, "version": 2,', 1)
    with pytest.raises(deploy.DeploymentContractError, match="manifest-duplicate-field"):
        deploy.load_contract(manifest_bytes=duplicate.encode())


def _orchestration_contract(tmp_path: Path) -> deploy.DeploymentContract:
    runtime_directory = tmp_path / "runtime"
    return replace(
        deploy.load_contract(),
        runtime_directory=runtime_directory,
        secret_override=runtime_directory / "secrets.yml",
        lease_path=runtime_directory / "lease.json",
        lease_owner_uids=frozenset({0}),
    )


def _stub_orchestration_gates(
    monkeypatch: pytest.MonkeyPatch,
    contract: deploy.DeploymentContract,
    events: list[str],
    *,
    post_error: str | None = None,
    rollback_error: str | None = None,
    baseline_error: str | None = None,
    readiness_error: str | None = None,
    evidence_error_once: bool = False,
    release_error: bool = False,
) -> SimpleNamespace:
    target = deploy.InspectedImage(IMAGE_NEW, REVISION_NEW)
    baseline = SimpleNamespace(
        hermes=SimpleNamespace(image_id=IMAGE_OLD, revision=REVISION_OLD)
    )
    post = _post_result(contract.attestation_policy)
    monkeypatch.setattr(deploy.preflight, "validate_deployment_lease_owner", lambda **_kwargs: None)
    monkeypatch.setattr(
        deploy,
        "_validate_operation_identity",
        lambda *_args, **_kwargs: (target, REVISION_NEW),
    )
    monkeypatch.setattr(deploy, "_validate_runtime_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        deploy.preflight,
        "acquire_deployment_lease",
        lambda **_kwargs: SimpleNamespace(),
    )

    def release(*_args, **_kwargs):
        events.append("lease_release")
        if release_error:
            raise deploy.preflight.DeployPreflightError("lease-release-failed")

    monkeypatch.setattr(deploy.preflight, "release_deployment_lease", release)
    monkeypatch.setattr(
        deploy,
        "_ordinary_deploy_pre_mutation_barrier",
        lambda *_args, **_kwargs: (events.append("p0") or target, {"TELEGRAM_BOT_TOKEN": TOKEN}, REVISION_NEW),
    )

    def capture(_contract):
        events.append("baseline")
        if baseline_error:
            raise deploy.DeploymentContractError(baseline_error)
        return baseline

    monkeypatch.setattr(deploy, "_capture_pre_mutation_baseline", capture)

    def readiness(*_args):
        events.append("rollback_readiness")
        if readiness_error:
            raise deploy.DeploymentContractError(readiness_error)

    monkeypatch.setattr(deploy, "_validate_automatic_rollback_readiness", readiness)
    monkeypatch.setattr(
        deploy,
        "_begin_secret_override_transaction",
        lambda *_args: events.append("first_mutation") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        deploy,
        "_compose_recreate_hermes",
        lambda *_args, **_kwargs: events.append("compose"),
    )

    def post_check(*_args, **_kwargs):
        events.append("post")
        if post_error:
            raise runtime.RuntimeAttestationError(post_error)
        return post

    monkeypatch.setattr(deploy, "_post_deploy_attestation", post_check)
    monkeypatch.setattr(
        deploy,
        "_finish_secret_override_transaction",
        lambda *_args, **_kwargs: events.append("config_restore"),
    )

    def automatic(*_args):
        events.append("automatic_rollback")
        if rollback_error:
            raise runtime.RuntimeAttestationError(rollback_error)
        return post

    monkeypatch.setattr(deploy, "_automatic_rollback", automatic)
    evidence_calls = 0

    def evidence(*_args, **_kwargs):
        nonlocal evidence_calls
        evidence_calls += 1
        events.append(f"evidence_{_kwargs['operation_status']}")
        if evidence_error_once and evidence_calls == 1:
            raise runtime.RuntimeAttestationError("EVIDENCE_WRITE_FAILED")
        return contract.runtime_directory / "evidence.json"

    monkeypatch.setattr(deploy, "_write_operation_evidence", evidence)
    return baseline


def test_orchestrator_captures_baseline_before_first_mutation_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _orchestration_contract(tmp_path)
    events: list[str] = []
    _stub_orchestration_gates(monkeypatch, contract, events)
    deploy.execute_operation(
        contract,
        source=Path("/synthetic/source"),
        image=IMAGE_NEW,
        revision=REVISION_NEW,
        confirmation=deploy.DEPLOY_CONFIRMATION,
        rollback=False,
    )
    assert events.index("baseline") < events.index("first_mutation")
    assert events.index("rollback_readiness") < events.index("first_mutation")
    assert events.count("automatic_rollback") == 0
    assert events[-1] == "lease_release"


@pytest.mark.parametrize(
    "post_error",
    (
        "HERMES_IMAGE_MISMATCH",
        "HERMES_REVISION_MISMATCH",
        "HERMES_NOT_RUNNING",
        "HERMES_RESTART_COUNT_CHANGED",
        "HERMES_LATE_CRASH",
        "PYTHON_TRACEBACK",
        "TELEGRAM_CONNECTIVITY_FAILED",
        "GATEWAY_SMOKE_FAILED",
        "DATABASE_SCHEMA_CHANGED",
        "DATABASE_INTEGRITY_FAILED",
        "DATABASE_FOREIGN_KEY_VIOLATION",
        "DATABASE_DATA_DELTA",
        "FEATURE_GATE_DELTA",
        "ALLOWLIST_DELTA",
        "SECRET_FINGERPRINT_DELTA",
        "QDRANT_CONTAINER_CHANGED",
        "QDRANT_IMAGE_CHANGED",
        "QDRANT_RESTART_COUNT_CHANGED",
        "QDRANT_MOUNT_SET_CHANGED",
    ),
)
def test_every_post_mutation_failure_calls_rollback_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_error: str,
) -> None:
    contract = _orchestration_contract(tmp_path)
    events: list[str] = []
    _stub_orchestration_gates(
        monkeypatch,
        contract,
        events,
        post_error=post_error,
    )
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        deploy.execute_operation(
            contract,
            source=Path("/synthetic/source"),
            image=IMAGE_NEW,
            revision=REVISION_NEW,
            confirmation=deploy.DEPLOY_CONFIRMATION,
            rollback=False,
        )
    assert error.value.status == "ROLLED_BACK"
    assert error.value.original_error_code == post_error
    assert error.value.rollback_error_code is None
    assert events.count("automatic_rollback") == 1
    assert events.count("config_restore") == 1
    assert events[-1] == "lease_release"


def test_rollback_health_failure_is_reported_separately_and_release_does_not_mask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _orchestration_contract(tmp_path)
    events: list[str] = []
    _stub_orchestration_gates(
        monkeypatch,
        contract,
        events,
        post_error="TELEGRAM_CONNECTIVITY_FAILED",
        rollback_error="ROLLBACK_HEALTH_FAILED",
        release_error=True,
    )
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        deploy.execute_operation(
            contract,
            source=Path("/synthetic/source"),
            image=IMAGE_NEW,
            revision=REVISION_NEW,
            confirmation=deploy.DEPLOY_CONFIRMATION,
            rollback=False,
        )
    assert error.value.status == "FAIL"
    assert error.value.original_error_code == "TELEGRAM_CONNECTIVITY_FAILED"
    assert error.value.rollback_error_code == "ROLLBACK_HEALTH_FAILED"
    assert events.count("automatic_rollback") == 1
    assert events[-1] == "lease_release"


@pytest.mark.parametrize(
    ("baseline_error", "readiness_error"),
    (("baseline-failed", None), (None, "rollback-readiness-failed")),
)
def test_pre_mutation_failure_never_mutates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    baseline_error: str | None,
    readiness_error: str | None,
) -> None:
    contract = _orchestration_contract(tmp_path)
    events: list[str] = []
    _stub_orchestration_gates(
        monkeypatch,
        contract,
        events,
        baseline_error=baseline_error,
        readiness_error=readiness_error,
    )
    with pytest.raises(deploy.DeploymentContractError):
        deploy.execute_operation(
            contract,
            source=Path("/synthetic/source"),
            image=IMAGE_NEW,
            revision=REVISION_NEW,
            confirmation=deploy.DEPLOY_CONFIRMATION,
            rollback=False,
        )
    assert "first_mutation" not in events
    assert "compose" not in events
    assert "automatic_rollback" not in events
    assert events[-1] == "lease_release"


def test_evidence_write_failure_after_mutation_triggers_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _orchestration_contract(tmp_path)
    events: list[str] = []
    _stub_orchestration_gates(
        monkeypatch,
        contract,
        events,
        evidence_error_once=True,
    )
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        deploy.execute_operation(
            contract,
            source=Path("/synthetic/source"),
            image=IMAGE_NEW,
            revision=REVISION_NEW,
            confirmation=deploy.DEPLOY_CONFIRMATION,
            rollback=False,
        )
    assert error.value.status == "ROLLED_BACK"
    assert error.value.original_error_code == "EVIDENCE_WRITE_FAILED"
    assert events.count("automatic_rollback") == 1
    assert events.count("evidence_ROLLED_BACK") == 1


def test_automatic_rollback_restores_previous_image_and_uses_same_health_contract(
    policy: runtime.RuntimeAttestationPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = deploy.load_contract()
    baseline = SimpleNamespace(
        hermes=SimpleNamespace(image_id=IMAGE_OLD, revision=REVISION_OLD)
    )
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        runtime,
        "rollback_log_baseline",
        lambda value: calls.append(("cursor", "", "")) or value,
    )
    monkeypatch.setattr(
        deploy,
        "_begin_secret_override_transaction",
        lambda _contract, secrets: (
            calls.append(("config_publish", str(len(secrets)), "")) or object()
        ),
    )
    monkeypatch.setattr(
        deploy,
        "_finish_secret_override_transaction",
        lambda *_args, **_kwargs: calls.append(("config_restore", "", "")),
    )
    monkeypatch.setattr(
        deploy,
        "_compose_recreate_hermes",
        lambda _contract, *, image_id, revision: calls.append(("compose", image_id, revision)),
    )
    expected = _post_result(policy)
    monkeypatch.setattr(
        deploy,
        "_post_deploy_attestation",
        lambda _contract, _baseline, *, target_image_id, target_revision: (
            calls.append(("health", target_image_id, target_revision)) or expected
        ),
    )
    assert deploy._automatic_rollback(
        contract, baseline, {"TELEGRAM_BOT_TOKEN": TOKEN}
    ) is expected
    assert calls == [
        ("cursor", "", ""),
        ("config_publish", "1", ""),
        ("compose", IMAGE_OLD, REVISION_OLD),
        ("health", IMAGE_OLD, REVISION_OLD),
        ("config_restore", "", ""),
    ]
