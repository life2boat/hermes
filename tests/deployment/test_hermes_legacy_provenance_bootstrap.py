from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import hermes_legacy_provenance_bootstrap as bootstrap
from scripts import hermes_production_deploy as deploy


LEGACY_IMAGE = "sha256:" + "1" * 64
CANDIDATE_IMAGE = "sha256:" + "2" * 64
CANDIDATE_REVISION = "a" * 40


def _completed(argv: tuple[str, ...], *, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


def _image_inspect(image_id: str, revision: str | None) -> str:
    labels = {} if revision is None else {"org.opencontainers.image.revision": revision}
    return json.dumps([{"Id": image_id, "Config": {"Labels": labels}}])


def _contract() -> SimpleNamespace:
    return SimpleNamespace(
        image_revision_label="org.opencontainers.image.revision",
        lease_owner_uids=frozenset({0}),
        lease_path=Path("/run/hermes/deployment.lock"),
        lease_timeout_seconds=60,
        canonical_repository="https://github.com/life2boat/hermes.git",
        target_service="hermes-bot",
        database_source=Path("/var/lib/hermes/production-db/healbite.db"),
        protected_secret_names=("TELEGRAM_BOT_TOKEN",),
        attestation_policy=object(),
    )


def _baseline() -> SimpleNamespace:
    hermes = SimpleNamespace(image_id=LEGACY_IMAGE, revision="")
    return SimpleNamespace(hermes=hermes)


def test_missing_revision_is_classified_as_legacy_without_inventing_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **_kwargs: _completed(
            argv,
            stdout=_image_inspect(LEGACY_IMAGE, None),
        ),
    )

    assert bootstrap._inspect_legacy_image(contract, LEGACY_IMAGE) == LEGACY_IMAGE
    assert bootstrap.LEGACY_CLASSIFICATION == "LEGACY_BASELINE"
    assert bootstrap.UNKNOWN_REVISION == "UNKNOWN"


def test_bootstrap_is_denied_after_runtime_has_valid_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **_kwargs: _completed(
            argv,
            stdout=_image_inspect(LEGACY_IMAGE, CANDIDATE_REVISION),
        ),
    )

    with pytest.raises(
        bootstrap.BootstrapError,
        match="BOOTSTRAP_DENIED_USE_ORDINARY_DEPLOY",
    ):
        bootstrap._inspect_legacy_image(contract, LEGACY_IMAGE)


def test_unhealthy_legacy_runtime_is_denied_before_image_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    contract.database_target = Path("/home/hermes/healbite.db")
    monkeypatch.setattr(
        bootstrap.attestation,
        "capture_pre_mutation_baseline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bootstrap.attestation.RuntimeAttestationError(
                "PRE_MUTATION_HERMES_UNHEALTHY"
            )
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "_inspect_legacy_image",
        lambda *_args: pytest.fail("image classification must not run"),
    )

    with pytest.raises(
        bootstrap.attestation.RuntimeAttestationError,
        match="PRE_MUTATION_HERMES_UNHEALTHY",
    ):
        bootstrap._capture_eligible_baseline(contract)


def test_ordinary_deploy_still_rejects_missing_revision_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **_kwargs: _completed(
            argv,
            stdout=_image_inspect(LEGACY_IMAGE, None),
        ),
    )

    with pytest.raises(
        deploy.DeploymentContractError,
        match="image-revision-label-missing",
    ):
        deploy.inspect_local_image(contract, LEGACY_IMAGE)


def test_candidate_failure_restores_exact_legacy_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = _contract()
    baseline = _baseline()
    artifact = tmp_path / "legacy-image.tar"
    artifact.write_bytes(b"private-rollback-artifact")
    plan = {
        "REPOSITORY_ROOT": str(tmp_path),
        "CANDIDATE_OCI_REVISION": CANDIDATE_REVISION,
        "CANDIDATE_IMAGE_ID": CANDIDATE_IMAGE,
        "LEGACY_IMAGE_ID": LEGACY_IMAGE,
    }
    compose_calls: list[tuple[str, str]] = []
    post_revisions: list[str | None] = []
    evidence: list[str] = []

    monkeypatch.setattr(bootstrap, "_root_required", lambda: None)
    monkeypatch.setattr(bootstrap, "_open_plan", lambda *_args: plan)
    monkeypatch.setattr(deploy, "load_contract", lambda _root: contract)
    monkeypatch.setattr(deploy, "_validate_runtime_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bootstrap.preflight, "validate_deployment_lease_owner", lambda **_kwargs: 0)
    monkeypatch.setattr(bootstrap.preflight, "acquire_deployment_lease", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap.preflight, "release_deployment_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bootstrap,
        "_validate_plan_runtime",
        lambda *_args, **_kwargs: (contract, baseline, artifact),
    )
    monkeypatch.setattr(
        deploy,
        "_compose_recreate_hermes",
        lambda _contract, *, image_id, revision: compose_calls.append((image_id, revision)),
    )
    monkeypatch.setattr(bootstrap, "_ensure_legacy_image", lambda *_args: None)
    monkeypatch.setattr(
        bootstrap.attestation,
        "rollback_log_baseline",
        lambda value: value,
    )

    def post(*_args, revision_label, **_kwargs):
        post_revisions.append(revision_label)
        if revision_label is not None:
            raise bootstrap.BootstrapError("CANDIDATE_POST_CHECK_FAILED")
        return object()

    monkeypatch.setattr(bootstrap.attestation, "post_deploy_attestation", post)
    monkeypatch.setattr(
        bootstrap,
        "_write_execution_evidence",
        lambda _path, *, status, **_kwargs: evidence.append(status),
    )

    with pytest.raises(
        bootstrap.BootstrapRolledBack,
        match="BOOTSTRAP_CANDIDATE_REJECTED_ROLLED_BACK",
    ):
        bootstrap.execute_bootstrap(
            Namespace(
                plan=str(tmp_path / "bootstrap-plan.json"),
                expected_plan_sha256="f" * 64,
                confirm=bootstrap.EXECUTE_CONFIRMATION,
            )
        )

    assert compose_calls == [
        (CANDIDATE_IMAGE, CANDIDATE_REVISION),
        (LEGACY_IMAGE, CANDIDATE_REVISION),
    ]
    assert post_revisions == [contract.image_revision_label, None]
    assert evidence == ["ROLLED_BACK"]


def test_rollback_artifact_hash_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "legacy-image.tar"
    plan = {
        "ROLLBACK_ARTIFACT_PATH": str(artifact),
        "ROLLBACK_ARTIFACT_SIZE": 10,
        "ROLLBACK_ARTIFACT_SHA256": "a" * 64,
    }
    monkeypatch.setattr(
        bootstrap,
        "_read_private_file",
        lambda *_args, **_kwargs: (None, 10, "b" * 64),
    )

    with pytest.raises(bootstrap.BootstrapError, match="ROLLBACK_ARTIFACT_DRIFT"):
        bootstrap._validate_artifact(plan)


def test_bootstrap_entrypoint_cannot_run_schema_or_feature_mutation() -> None:
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")

    assert "healbite_schema_migrate" not in source
    assert "HERMES_RUNTIME_IMAGE_IDENTITY_ONLY" in source
    assert '"SCHEMA_MIGRATION_ALLOWED": False' in source
    assert '"FEATURE_ACTIVATION_ALLOWED": False' in source
