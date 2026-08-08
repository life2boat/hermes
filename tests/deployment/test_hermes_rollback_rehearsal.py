from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import hermes_legacy_provenance_bootstrap as bootstrap
from scripts import hermes_production_deploy as deploy


LEGACY_IMAGE = "sha256:" + "1" * 64
PLAN_SHA = "2" * 64
ARTIFACT_SHA = "3" * 64
OPERATION_ID = "4" * 32


def _completed(argv: tuple[str, ...], returncode: int = 0):
    return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")


def _plan(tmp_path: Path) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "OPERATION_ID": OPERATION_ID,
        "PLAN_SHA256": PLAN_SHA,
        "CREATED_AT": (now - timedelta(seconds=10)).isoformat(),
        "EXPIRES_AT": (now + timedelta(minutes=30)).isoformat(),
        "ROLLBACK_ARTIFACT_PATH": str(tmp_path / "legacy-image.tar"),
        "ROLLBACK_ARTIFACT_SIZE": 100,
        "ROLLBACK_ARTIFACT_SHA256": ARTIFACT_SHA,
        "ROLLBACK_REHEARSAL_PATH": str(tmp_path / "rollback-rehearsal.json"),
        "LEGACY_IMAGE_ID": LEGACY_IMAGE,
    }


def _evidence(plan: dict[str, object], **overrides: object) -> dict[str, object]:
    payload = {
        "ROLLBACK_REHEARSAL_VERSION": bootstrap.ROLLBACK_REHEARSAL_VERSION,
        "OPERATION_ID": plan["OPERATION_ID"],
        "PLAN_SHA256": PLAN_SHA,
        "ROLLBACK_ARTIFACT_SHA256": plan["ROLLBACK_ARTIFACT_SHA256"],
        "EXPECTED_LEGACY_IMAGE_ID": plan["LEGACY_IMAGE_ID"],
        "ARCHIVE_STRUCTURE_VALID": True,
        "DOCKER_LOAD_ACCEPTED": True,
        "LOADED_IMAGE_IDENTITY_VALID": True,
        "REHEARSAL_STATUS": "PASS",
        "CREATED_AT": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


def _bind_private_json(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    data = bootstrap._canonical_json(payload)
    monkeypatch.setattr(
        bootstrap,
        "_read_private_file",
        lambda *_args, **_kwargs: (data, len(data), "f" * 64),
    )


def test_open_rehearsal_accepts_exact_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    _bind_private_json(monkeypatch, _evidence(plan))

    result = bootstrap._open_rehearsal(tmp_path / "bootstrap-plan.json", PLAN_SHA, plan)

    assert result["REHEARSAL_STATUS"] == "PASS"


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("OPERATION_ID", "5" * 32),
        ("PLAN_SHA256", "6" * 64),
        ("ROLLBACK_ARTIFACT_SHA256", "7" * 64),
        ("EXPECTED_LEGACY_IMAGE_ID", "sha256:" + "8" * 64),
    ],
)
def test_rehearsal_binding_mismatch_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    wrong: str,
) -> None:
    plan = _plan(tmp_path)
    _bind_private_json(monkeypatch, _evidence(plan, **{field: wrong}))

    with pytest.raises(
        bootstrap.BootstrapError, match="ROLLBACK_REHEARSAL_BINDING_INVALID"
    ):
        bootstrap._open_rehearsal(tmp_path / "bootstrap-plan.json", PLAN_SHA, plan)


def test_missing_rehearsal_evidence_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "_read_private_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    with pytest.raises(
        bootstrap.BootstrapError, match="ROLLBACK_REHEARSAL_EVIDENCE_MISSING"
    ):
        bootstrap._open_rehearsal(tmp_path / "bootstrap-plan.json", PLAN_SHA, plan)


def test_expired_rehearsal_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    plan["EXPIRES_AT"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    _bind_private_json(monkeypatch, _evidence(plan))

    with pytest.raises(bootstrap.BootstrapError, match="ROLLBACK_REHEARSAL_EXPIRED"):
        bootstrap._open_rehearsal(tmp_path / "bootstrap-plan.json", PLAN_SHA, plan)


def test_rehearsal_evidence_symlink_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    target = tmp_path / "target.json"
    target.write_bytes(bootstrap._canonical_json(_evidence(plan)))
    (tmp_path / "rollback-rehearsal.json").symlink_to(target)
    monkeypatch.setattr(bootstrap, "_private_directory", lambda *_args: None)

    with pytest.raises(
        bootstrap.BootstrapError, match="ROLLBACK_REHEARSAL_EVIDENCE_MISSING"
    ):
        bootstrap._open_rehearsal(tmp_path / "bootstrap-plan.json", PLAN_SHA, plan)


def test_rehearsal_evidence_unsafe_mode_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    evidence_path = tmp_path / "rollback-rehearsal.json"
    evidence_path.write_bytes(bootstrap._canonical_json(_evidence(plan)))
    evidence_path.chmod(0o644)
    monkeypatch.setattr(bootstrap, "_private_directory", lambda *_args: None)

    with pytest.raises(
        bootstrap.BootstrapError, match="ROLLBACK_REHEARSAL_EVIDENCE_MISSING"
    ):
        bootstrap._open_rehearsal(tmp_path / "bootstrap-plan.json", PLAN_SHA, plan)


def _prepare_rehearse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    load_returncode: int = 0,
) -> tuple[dict[str, object], list[tuple[str, ...]], list[dict[str, object]]]:
    plan = _plan(tmp_path)
    baseline = SimpleNamespace(hermes=SimpleNamespace(image_id=LEGACY_IMAGE))
    contract = SimpleNamespace()
    artifact = Path(str(plan["ROLLBACK_ARTIFACT_PATH"]))
    calls: list[tuple[str, ...]] = []
    evidence: list[dict[str, object]] = []
    monkeypatch.setattr(bootstrap, "_root_required", lambda: None)
    monkeypatch.setattr(bootstrap, "_open_plan", lambda *_args: plan)
    monkeypatch.setattr(
        bootstrap,
        "_validate_plan_runtime",
        lambda *_args, **_kwargs: (contract, baseline, artifact),
    )
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **_kwargs: (
            calls.append(argv) or _completed(argv, returncode=load_returncode)
        ),
    )
    monkeypatch.setattr(bootstrap, "_inspect_legacy_image", lambda *_args: LEGACY_IMAGE)
    monkeypatch.setattr(
        bootstrap, "_capture_eligible_baseline", lambda *_args: baseline
    )
    monkeypatch.setattr(
        bootstrap, "_baseline_contract", lambda *_args: {"stable": True}
    )
    monkeypatch.setattr(bootstrap, "_validate_artifact", lambda *_args: artifact)
    monkeypatch.setattr(
        bootstrap,
        "verify_rollback_archive",
        lambda *_args: {"archive_structure_valid": True, "image_id": LEGACY_IMAGE},
    )
    monkeypatch.setattr(
        bootstrap,
        "_write_new_private_json",
        lambda _path, payload: evidence.append(payload) or "e" * 64,
    )
    monkeypatch.setattr(bootstrap, "_open_rehearsal", lambda *_args: {})
    monkeypatch.setattr(
        deploy,
        "_compose_recreate_hermes",
        lambda *_args, **_kwargs: pytest.fail("rehearsal must not recreate runtime"),
    )
    return plan, calls, evidence


def test_rehearse_loads_exact_archive_without_runtime_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, calls, evidence = _prepare_rehearse(monkeypatch, tmp_path)

    result = bootstrap.rehearse_rollback(
        Namespace(
            plan=str(tmp_path / "bootstrap-plan.json"), expected_plan_sha256=PLAN_SHA
        )
    )

    assert result == 0
    assert calls == [
        (
            "docker",
            "image",
            "load",
            "--input",
            str(plan["ROLLBACK_ARTIFACT_PATH"]),
        )
    ]
    assert evidence[0]["REHEARSAL_STATUS"] == "PASS"


def test_rehearse_load_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _plan_data, _calls, evidence = _prepare_rehearse(
        monkeypatch, tmp_path, load_returncode=1
    )

    with pytest.raises(
        bootstrap.BootstrapError, match="ROLLBACK_REHEARSAL_DOCKER_LOAD_FAILED"
    ):
        bootstrap.rehearse_rollback(
            Namespace(
                plan=str(tmp_path / "bootstrap-plan.json"),
                expected_plan_sha256=PLAN_SHA,
            )
        )

    assert evidence == []


def test_load_success_without_identity_proof_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare_rehearse(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "_inspect_legacy_image",
        lambda *_args: (_ for _ in ()).throw(
            bootstrap.BootstrapError("LEGACY_IMAGE_MISSING")
        ),
    )

    with pytest.raises(bootstrap.BootstrapError, match="LEGACY_IMAGE_MISSING"):
        bootstrap.rehearse_rollback(
            Namespace(
                plan=str(tmp_path / "bootstrap-plan.json"),
                expected_plan_sha256=PLAN_SHA,
            )
        )


def test_archive_drift_after_load_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare_rehearse(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "_validate_artifact",
        lambda *_args: (_ for _ in ()).throw(
            bootstrap.BootstrapError("ROLLBACK_ARTIFACT_DRIFT")
        ),
    )

    with pytest.raises(bootstrap.BootstrapError, match="ROLLBACK_ARTIFACT_DRIFT"):
        bootstrap.rehearse_rollback(
            Namespace(
                plan=str(tmp_path / "bootstrap-plan.json"),
                expected_plan_sha256=PLAN_SHA,
            )
        )


def test_validate_bootstrap_requires_bound_rehearsal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(bootstrap, "_root_required", lambda: None)
    monkeypatch.setattr(bootstrap, "_open_plan", lambda *_args: plan)

    def validate(_plan: dict[str, object], **kwargs: object):
        captured.update(kwargs)
        raise bootstrap.BootstrapError("ROLLBACK_REHEARSAL_EVIDENCE_MISSING")

    monkeypatch.setattr(bootstrap, "_validate_plan_runtime", validate)

    with pytest.raises(
        bootstrap.BootstrapError, match="ROLLBACK_REHEARSAL_EVIDENCE_MISSING"
    ):
        bootstrap.validate_bootstrap(
            Namespace(
                plan=str(tmp_path / "bootstrap-plan.json"),
                expected_plan_sha256=PLAN_SHA,
            )
        )

    assert captured["plan_sha256"] == PLAN_SHA
    assert captured["plan_path"] == tmp_path / "bootstrap-plan.json"


def test_emergency_path_revalidates_same_rehearsed_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    artifact = Path(str(plan["ROLLBACK_ARTIFACT_PATH"]))
    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "_validate_artifact", lambda *_args: artifact)
    monkeypatch.setattr(
        bootstrap,
        "_open_rehearsal",
        lambda *_args, **kwargs: calls.append(
            f"evidence:{kwargs['require_unexpired']}"
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "verify_rollback_archive",
        lambda path, image: (
            calls.append(f"{path}:{image}")
            or {"archive_structure_valid": True, "image_id": image}
        ),
    )
    monkeypatch.setattr(bootstrap, "_inspect_legacy_image", lambda *_args: LEGACY_IMAGE)
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("existing exact image needs no reload"),
    )

    bootstrap._ensure_legacy_image(
        SimpleNamespace(),
        LEGACY_IMAGE,
        artifact,
        plan,
        tmp_path / "bootstrap-plan.json",
        PLAN_SHA,
    )

    assert calls == ["evidence:False", f"{artifact}:{LEGACY_IMAGE}"]


def test_execute_refuses_missing_rehearsal_before_runtime_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    plan.update({
        "REPOSITORY_ROOT": str(tmp_path),
        "CANDIDATE_OCI_REVISION": "a" * 40,
        "CANDIDATE_IMAGE_ID": "sha256:" + "9" * 64,
    })
    contract = SimpleNamespace(
        lease_owner_uids=frozenset({0}),
        lease_path=tmp_path / "lease",
        lease_timeout_seconds=60,
        canonical_repository="https://github.com/life2boat/hermes.git",
    )
    monkeypatch.setattr(bootstrap, "_root_required", lambda: None)
    monkeypatch.setattr(bootstrap, "_open_plan", lambda *_args: plan)
    monkeypatch.setattr(deploy, "load_contract", lambda *_args: contract)
    monkeypatch.setattr(
        deploy, "_validate_runtime_directory", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bootstrap.preflight, "validate_deployment_lease_owner", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        bootstrap.preflight, "acquire_deployment_lease", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        bootstrap.preflight, "release_deployment_lease", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bootstrap,
        "_validate_plan_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bootstrap.BootstrapError("ROLLBACK_REHEARSAL_EVIDENCE_MISSING")
        ),
    )
    monkeypatch.setattr(
        deploy,
        "_compose_recreate_hermes",
        lambda *_args, **_kwargs: pytest.fail("runtime mutation must not start"),
    )

    with pytest.raises(
        bootstrap.BootstrapError, match="ROLLBACK_REHEARSAL_EVIDENCE_MISSING"
    ):
        bootstrap.execute_bootstrap(
            Namespace(
                plan=str(tmp_path / "bootstrap-plan.json"),
                expected_plan_sha256=PLAN_SHA,
                confirm=bootstrap.EXECUTE_CONFIRMATION,
            )
        )


def test_parser_exposes_required_rehearsal_lifecycle() -> None:
    args = bootstrap.build_parser().parse_args([
        "rehearse-rollback",
        "--plan",
        "/private/op/bootstrap-plan.json",
        "--expected-plan-sha256",
        PLAN_SHA,
    ])

    assert args.command == "rehearse-rollback"
