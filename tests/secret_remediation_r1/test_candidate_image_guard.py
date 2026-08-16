import pytest
from ops.secret_remediation_r1.candidate_image_guard import (
    verify_legacy_image,
    CandidateImageGuardError,
)
from ops.secret_remediation_r1.constants import LEGACY_IMAGE_REF, LEGACY_IMAGE_ID
from ops.secret_remediation_r1.compose_command import build_recreate_argv


class MockBackend:
    def __init__(self, expected_id):
        self.expected_id = expected_id

    def inspect_image(self, ref):
        return {"Id": self.expected_id}


def test_verify_legacy_image_success():
    verify_legacy_image(LEGACY_IMAGE_REF, MockBackend(LEGACY_IMAGE_ID))


def test_verify_legacy_image_wrong_ref():
    with pytest.raises(
        CandidateImageGuardError, match="Effective image .* != expected"
    ):
        verify_legacy_image("wrong-image:latest", MockBackend(LEGACY_IMAGE_ID))


def test_verify_legacy_image_wrong_id():
    with pytest.raises(CandidateImageGuardError, match="Image ID .* != expected"):
        verify_legacy_image(LEGACY_IMAGE_REF, MockBackend("sha256:wrong"))


def test_compose_command_no_build_and_pull_never():
    argv = build_recreate_argv()
    assert "--no-build" in argv
    assert "--pull" in argv
    assert argv[argv.index("--pull") + 1] == "never"
