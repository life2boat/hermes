from __future__ import annotations

import os
import subprocess

import pytest

from tests.scripts import staged_migration_image_harness as harness


TARGET_IMAGE = os.environ.get("HEALBITE_STAGED_MIGRATION_IMAGE_ID")
IMMUTABLE_IMAGE = "sha256:" + "a" * 64
requires_exact_image = pytest.mark.skipif(
    not TARGET_IMAGE,
    reason="exact staged-migration image contract not supplied",
)


def _flag_value(command: tuple[str, ...], flag: str) -> str:
    return command[command.index(flag) + 1]


@pytest.mark.parametrize("scenario", sorted(harness.SCENARIOS))
def test_hardened_command_contract(scenario: str) -> None:
    command = harness.build_docker_command(IMMUTABLE_IMAGE, scenario)

    assert command[:3] == ("docker", "run", "--rm")
    assert _flag_value(command, "--network") == "none"
    assert "--read-only" in command
    assert _flag_value(command, "--cap-drop") == "ALL"
    assert _flag_value(command, "--security-opt") == "no-new-privileges"
    assert _flag_value(command, "--tmpfs") == harness.TMPFS_SPEC
    assert "--name" not in command
    assert harness.executable_forbidden_patterns(command) == ()
    assert command[-3:] == ("-B", "-", scenario)


@pytest.mark.parametrize(
    "image_id",
    (
        "healbite-hermes:latest",
        "sha256:short",
        "sha256:" + "g" * 64,
        "sha256:" + "a" * 64 + ";touch /tmp/injected",
    ),
)
def test_image_reference_must_be_an_exact_immutable_id(image_id: str) -> None:
    with pytest.raises(ValueError, match="immutable image ID"):
        harness.build_docker_command(image_id, "import")


def test_scenario_is_allowlisted_without_shell_interpolation() -> None:
    with pytest.raises(ValueError, match="unknown harness scenario"):
        harness.build_docker_command(IMMUTABLE_IMAGE, "import; touch /tmp/injected")


def test_fixture_is_delivered_over_standard_input() -> None:
    observed: dict[str, object] = {}

    def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"scenario":"import","status":"pass"}',
            stderr="",
        )

    assert harness.run_scenario(IMMUTABLE_IMAGE, "import", runner=runner)["status"] == "pass"
    assert observed["input"] == harness.HARNESS_PROGRAM
    assert observed["text"] is True
    assert observed["capture_output"] is True
    assert observed["check"] is False
    assert "shell" not in observed
    assert all(".env" not in token for token in observed["command"])
    assert all("/home/hermes" not in token for token in observed["command"])


def test_controlled_failure_does_not_forward_container_output() -> None:
    def runner(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 17, stdout="sensitive stdout", stderr="sensitive stderr")

    with pytest.raises(harness.HarnessExecutionError) as raised:
        harness.run_scenario(IMMUTABLE_IMAGE, "failure-rollback", runner=runner)

    message = str(raised.value)
    assert message == "scenario_failed:failure-rollback:exit_17"
    assert "sensitive" not in message


@requires_exact_image
def test_exact_image_python_import_smoke() -> None:
    payload = harness.run_scenario(str(TARGET_IMAGE), "import")

    assert payload["migration_module_imported"] is True
    assert payload["repository_root"] is True


@requires_exact_image
def test_exact_image_forward_migration_and_staged_copy_idempotency() -> None:
    payload = harness.run_scenario(str(TARGET_IMAGE), "forward-idempotency")

    assert payload["forward_exit_code"] == 0
    assert payload["forward_committed"] is True
    assert payload["forward_schema_changed"] is True
    assert payload["idempotent_exit_code"] == 0
    assert payload["idempotent_committed"] is True
    assert payload["idempotent_schema_changed"] is False
    assert payload["path_mode"] == "STAGED_COPY"
    assert payload["sqlite_valid"] is True
    assert payload["private_directory"] is True
    assert payload["private_database"] is True


@requires_exact_image
def test_exact_image_injected_failure_rolls_back_private_fixture() -> None:
    payload = harness.run_scenario(str(TARGET_IMAGE), "failure-rollback")

    assert payload["failure_exit_classification"] == "MIGRATION_FAILED"
    assert payload["migration_commit_state"] == "ROLLED_BACK"
    assert payload["schema_may_have_changed"] is False
    assert payload["safe_to_rerun"] is True
    assert payload["schema_unchanged"] is True
    assert payload["sqlite_valid"] is True
