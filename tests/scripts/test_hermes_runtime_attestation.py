from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import hermes_execution_authority as authority
from scripts import hermes_runtime_attestation as runtime_attestation


CURRENT_IMAGE_ID = "sha256:" + "1" * 64
TARGET_IMAGE_ID = "sha256:" + "2" * 64
CURRENT_REVISION = "a" * 40
TARGET_REVISION = "b" * 40
PROJECT_NAME = "hermes-prod"
SERVICE_NAME = "hermes-bot"
DB_SOURCE = "/srv/hermes/production-db/healbite.db"
DB_TARGET = "/var/lib/hermes/healbite.db"


class _Artifact:
    def __init__(self, path: Path, payload: dict[str, object]) -> None:
        self.path = path
        self.payload = payload

    def path_matches(self) -> bool:
        return True

    def close(self) -> None:
        return None


class _Bundle:
    def __init__(self, plan_path: Path) -> None:
        self.runtime_image_id = CURRENT_IMAGE_ID
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        self.final_authority = SimpleNamespace(sha256="c" * 64)
        self.invocation_descriptor = SimpleNamespace(
            payload={
                "APPLICATION_SERVICE": SERVICE_NAME,
                "COMPOSE_PROJECT_NAME": PROJECT_NAME,
                "CANONICAL_DB_SOURCE": DB_SOURCE,
                "CANONICAL_DB_TARGET": DB_TARGET,
            }
        )
        self.plan_path = plan_path


def _runtime(*, running: bool = True) -> dict[str, object]:
    return {
        "Id": "d" * 64,
        "Name": "/" + SERVICE_NAME,
        "Image": CURRENT_IMAGE_ID,
        "State": {
            "Running": running,
            "Status": "running" if running else "exited",
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "OOMKilled": False,
            "Error": "",
        },
        "Config": {
            "Labels": {
                "com.docker.compose.project": PROJECT_NAME,
                "com.docker.compose.service": SERVICE_NAME,
                "com.docker.compose.project.working_dir": "/srv/hermes",
                "com.docker.compose.project.config_files": (
                    "/srv/hermes/compose.yml,/srv/hermes/compose.override.yml"
                ),
            },
            "Env": [
                "HEALBITE_FEATURE=false",
                "TELEGRAM_BOT_TOKEN=synthetic-secret",
            ],
        },
        "HostConfig": {"NetworkMode": "hermes_default"},
        "Mounts": [
            {
                "Type": "bind",
                "Source": DB_SOURCE,
                "Destination": DB_TARGET,
                "RW": True,
                "Mode": "rw",
                "Propagation": "rprivate",
            }
        ],
    }


def _plan(plan_path: Path) -> dict[str, object]:
    return {
        "EXPIRES_AT": (
            datetime.now(timezone.utc) + timedelta(minutes=30)
        ).isoformat().replace("+00:00", "Z"),
        "OPERATION_ID": "e" * 32,
        "MIGRATION_IMAGE_REVISION": TARGET_REVISION,
        "MIGRATION_IMAGE_ID": TARGET_IMAGE_ID,
        "PREVIOUS_IMAGE_ID": CURRENT_IMAGE_ID,
        "DB_CANONICAL_PATH": DB_SOURCE,
        "SOURCE_DEVICE": 8,
        "SOURCE_INODE": 9,
        "SOURCE_SIZE": 10,
        "SOURCE_SHA256": "f" * 64,
        "OPERATIONS_ROOT_APPROVAL_SHA256": "1" * 64,
        "CLEAN_START_POLICY_SHA256": "2" * 64,
        "PLAN_PATH": str(plan_path),
    }


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    bundle = _Bundle(plan_path)
    current = _runtime()
    image_revision = CURRENT_REVISION
    monkeypatch.setattr(
        authority,
        "_inspect_runtime",
        lambda _service: copy.deepcopy(current),
    )
    monkeypatch.setattr(
        authority,
        "_inspect_image",
        lambda _image, _expected: image_revision,
    )
    monkeypatch.setattr(
        runtime_attestation,
        "_runtime_file_record",
        lambda path: {
            "PATH": path,
            "DEVICE": 1,
            "INODE": 2,
            "SIZE": 3,
            "UID": 0,
            "GID": 0,
            "MODE": 0o600,
            "SHA256": hashlib.sha256(path.encode()).hexdigest(),
        },
    )
    return SimpleNamespace(
        bundle=bundle,
        plan_path=plan_path,
        plan=_plan(plan_path),
        current=current,
        set_runtime=lambda value: current.update(value),
        set_image_revision=lambda value: (
            monkeypatch.setattr(
                authority,
                "_inspect_image",
                lambda _image, _expected: value,
            )
        ),
    )


def _attest(harness) -> tuple[dict[str, object], Path]:
    payload = runtime_attestation.build_runtime_attestation_payload(
        bundle=harness.bundle,
        plan_path=harness.plan_path,
        plan_sha256="3" * 64,
        plan=harness.plan,
    )
    return payload, harness.plan_path.parent / "runtime-pin.json"


def test_running_attestation_allows_expected_stop(
    harness,
) -> None:
    payload, pin_path = _attest(harness)
    harness.current["State"] = {
        "Running": False,
        "Status": "exited",
        "Paused": False,
        "Restarting": False,
        "Dead": False,
        "OOMKilled": False,
        "Error": "",
    }
    runtime_attestation.validate_stopped_runtime_attestation(
        artifact=_Artifact(pin_path, payload),
        bundle=harness.bundle,
        plan_path=harness.plan_path,
        plan_sha256="3" * 64,
        plan=harness.plan,
    )
    assert payload["RUNTIME_STATE_ATTESTED"] == "running"
    assert payload["EXPECTED_RUNTIME_TRANSITION"] == "running_to_stopped"
    assert payload["CONTAINS_SECRETS"] is False
    assert "synthetic-secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.parametrize(
    "drift",
    ("container", "image", "revision", "compose", "mount", "config", "credential"),
)
def test_identity_drift_after_stop_fails_closed(harness, drift: str) -> None:
    payload, pin_path = _attest(harness)
    harness.current["State"] = {
        "Running": False,
        "Status": "exited",
        "Paused": False,
        "Restarting": False,
        "Dead": False,
        "OOMKilled": False,
        "Error": "",
    }
    if drift == "container":
        harness.current["Id"] = "e" * 64
    elif drift == "image":
        harness.current["Image"] = "sha256:" + "9" * 64
    elif drift == "revision":
        harness.set_image_revision("d" * 40)
    elif drift == "compose":
        harness.current["Config"]["Labels"][
            "com.docker.compose.project.working_dir"
        ] = "/srv/other"
    elif drift == "mount":
        harness.current["Mounts"][0]["Source"] = "/srv/other/healbite.db"
    elif drift == "config":
        harness.current["Config"]["Env"][0] = "HEALBITE_FEATURE=true"
    elif drift == "credential":
        harness.current["Config"]["Env"][1] = (
            "TELEGRAM_BOT_TOKEN=different-synthetic-secret"
        )
    with pytest.raises(authority.ExecutionAuthorityError):
        runtime_attestation.validate_stopped_runtime_attestation(
            artifact=_Artifact(pin_path, payload),
            bundle=harness.bundle,
            plan_path=harness.plan_path,
            plan_sha256="3" * 64,
            plan=harness.plan,
        )


def test_pin_payload_tamper_fails_closed(harness) -> None:
    payload, pin_path = _attest(harness)
    payload["IMMUTABLE_RUNTIME_IDENTITY"]["CONTAINER_ID"] = "0" * 64
    harness.current["State"] = {
        "Running": False,
        "Status": "exited",
        "Paused": False,
        "Restarting": False,
        "Dead": False,
        "OOMKilled": False,
        "Error": "",
    }
    with pytest.raises(authority.ExecutionAuthorityError) as error:
        runtime_attestation.validate_stopped_runtime_attestation(
            artifact=_Artifact(pin_path, payload),
            bundle=harness.bundle,
            plan_path=harness.plan_path,
            plan_sha256="3" * 64,
            plan=harness.plan,
        )
    assert error.value.code == "RUNTIME_IDENTITY_DRIFT_AFTER_STOP"


@pytest.mark.parametrize(
    ("binding", "value"),
    (
        ("plan_sha256", "4" * 64),
        ("source_db_inode", 42),
        ("release_revision", "c" * 40),
    ),
)
def test_wrong_plan_db_or_release_binding_fails_closed(
    harness,
    binding: str,
    value: object,
) -> None:
    payload, pin_path = _attest(harness)
    harness.current["State"] = {
        "Running": False,
        "Status": "exited",
        "Paused": False,
        "Restarting": False,
        "Dead": False,
        "OOMKilled": False,
        "Error": "",
    }
    plan_sha256 = "3" * 64
    if binding == "plan_sha256":
        plan_sha256 = str(value)
    elif binding == "source_db_inode":
        harness.plan["SOURCE_INODE"] = value
    elif binding == "release_revision":
        harness.plan["MIGRATION_IMAGE_REVISION"] = value
    with pytest.raises(authority.ExecutionAuthorityError) as error:
        runtime_attestation.validate_stopped_runtime_attestation(
            artifact=_Artifact(pin_path, payload),
            bundle=harness.bundle,
            plan_path=harness.plan_path,
            plan_sha256=plan_sha256,
            plan=harness.plan,
        )
    assert error.value.code == "RUNTIME_ATTESTATION_PLAN_BINDING_MISMATCH"


@pytest.mark.parametrize("expired_component", ("pin", "authority"))
def test_expired_pin_or_authority_fails_closed(
    harness,
    expired_component: str,
) -> None:
    payload, pin_path = _attest(harness)
    harness.current["State"] = {
        "Running": False,
        "Status": "exited",
        "Paused": False,
        "Restarting": False,
        "Dead": False,
        "OOMKilled": False,
        "Error": "",
    }
    if expired_component == "pin":
        payload["EXPIRES_AT"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
    else:
        harness.bundle.expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
    with pytest.raises(authority.ExecutionAuthorityError) as error:
        runtime_attestation.validate_stopped_runtime_attestation(
            artifact=_Artifact(pin_path, payload),
            bundle=harness.bundle,
            plan_path=harness.plan_path,
            plan_sha256="3" * 64,
            plan=harness.plan,
        )
    assert error.value.code == "RUNTIME_ATTESTATION_EXPIRED"


def test_open_pin_rejects_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(authority, "TRUSTED_FILESYSTEM_ANCHOR", tmp_path)
    pin_path = tmp_path / "runtime-pin.json"
    pin_path.write_text(
        json.dumps(
            {
                name: (
                    False
                    if name == "CONTAINS_SECRETS"
                    else (
                        1
                        if name in {
                            "RUNTIME_ATTESTATION_VERSION",
                        }
                        else (
                            "running"
                            if name == "RUNTIME_STATE_ATTESTED"
                            else (
                                "running_to_stopped"
                                if name == "EXPECTED_RUNTIME_TRANSITION"
                                else ("a" * 64)
                            )
                        )
                    )
                )
                for name in runtime_attestation.RUNTIME_ATTESTATION_FIELDS
            },
            sort_keys=True,
        ),
        encoding="ascii",
    )
    pin_path.chmod(0o600)
    with pytest.raises(authority.ExecutionAuthorityError) as error:
        runtime_attestation.open_runtime_attestation(
            str(pin_path),
            "b" * 64,
        )
    assert error.value.code == "RUNTIME_ATTESTATION_SHA256_MISMATCH"
