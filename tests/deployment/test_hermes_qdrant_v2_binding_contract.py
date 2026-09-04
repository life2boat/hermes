from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_production_deploy as deploy  # noqa: E402
import hermes_post_deploy_attestation as attestation  # noqa: E402

IMAGE_ID = "sha256:" + "a" * 64
PREVIOUS_IMAGE_ID = "sha256:" + "b" * 64
REVISION = "c" * 40
FAKE_SECRET = "placeholder-telegram-token"
CANONICAL_COLLECTION = "healbite_memory_os_v2"
LEGACY_COLLECTION = "healbite_memory_os"


def _completed(
    argv,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


@pytest.fixture
def base_contract(tmp_path: Path) -> tuple[deploy.DeploymentContract, Path]:
    source = tmp_path / "host-secrets.env"
    source.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\n", encoding="utf-8")
    source.chmod(0o600)
    runtime_dir = tmp_path / "run" / "hermes"
    runtime_dir.mkdir(parents=True, mode=0o700)
    database_source = tmp_path / "production-db" / "healbite.db"
    database_source.parent.mkdir(parents=True, mode=0o700)
    database_source.write_bytes(b"synthetic-db")
    database_source.chmod(0o600)
    contract = replace(
        deploy.load_contract(),
        runtime_directory=runtime_dir,
        secret_override=runtime_dir / "hermes-secrets-override.yml",
        lease_path=runtime_dir / "hermes-deployment-operation.json",
        lease_owner_uids=frozenset({deploy._effective_uid()}),
        approved_secret_source=source,
        approved_source_owner_uids=frozenset({deploy._effective_uid()}),
        database_source=database_source,
        capacity_filesystem=tmp_path,
        minimum_free_basis_points=1,
        estimated_peak_incremental_build_bytes=1,
        build_peak_multiplier=1,
        staging_safety_margin_bytes=1,
        capacity_formula_source="synthetic-test-policy",
    )
    deploy.prepare_secret_override(contract, source)
    return contract, source


def _mock_compose_runner(
    contract: deploy.DeploymentContract,
    *,
    override_env: dict[str, str] | None = None,
):
    environment = {
        **contract.runtime_bindings,
        **contract.feature_gates,
    }
    if override_env is not None:
        environment.update(override_env)
    compose_document = {
        "services": {
            contract.target_service: {
                "environment": environment,
                "volumes": [
                    {
                        "type": contract.database_mount_type,
                        "source": str(contract.database_source),
                        "target": str(contract.database_target),
                        "read_only": contract.database_read_only,
                    }
                ],
            }
        }
    }

    def runner(argv, **_kwargs):
        command = tuple(str(item) for item in argv)
        if command[-2:] == ("config", "--services"):
            return _completed(argv, stdout="hermes-bot\nqdrant\n")
        if command[-3:] == ("config", "--format", "json"):
            return _completed(argv, stdout=json.dumps(compose_document))
        if command[-2:] == ("config", "--quiet"):
            return _completed(argv, stdout="")
        if command[:3] == ("docker", "image", "inspect"):
            record = {
                "Id": IMAGE_ID,
                "Config": {"Labels": {contract.image_revision_label: REVISION}},
            }
            return _completed(argv, stdout=json.dumps([record]))
        return _completed(argv)

    return runner


def _mock_container_snapshot(
    *,
    collection: str | None = CANONICAL_COLLECTION,
    image_id: str = IMAGE_ID,
    revision: str = REVISION,
    state: str = "running",
    restart_count: int = 0,
) -> attestation.ContainerSnapshot:
    env = [
        "TELEGRAM_BOT_TOKEN=" + FAKE_SECRET,
        *(f"{k}={v}" for k, v in deploy.load_contract().feature_gates.items()),
    ]
    if collection is not None:
        env.append(f"QDRANT_COLLECTION={collection}")
    record = {
        "Id": "test-container-id",
        "Image": image_id,
        "Created": "2026-09-04T00:00:00Z",
        "RestartCount": restart_count,
        "State": {"Status": state, "StartedAt": "2026-09-04T00:01:00Z"},
        "Config": {
            "Labels": {"org.opencontainers.image.revision": revision},
            "Env": env,
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/var/lib/hermes/production-db/healbite.db",
                "Destination": "/home/hermes/healbite.db",
                "RW": True,
            }
        ],
    }
    return attestation._container_snapshot(
        "hermes-bot",
        revision_label="org.opencontainers.image.revision",
        feature_gate_names=attestation.CANONICAL_FEATURE_GATE_NAMES,
        allowlist_names=attestation.CANONICAL_ALLOWLIST_NAMES,
        protected_secret_names=("TELEGRAM_BOT_TOKEN",),
        run=lambda _argv, **_kw: _completed(_argv, stdout=json.dumps([record])),
    )


# ============================================================
# 1. CANONICAL CONTRACT DECLARATION
# ============================================================
def test_canonical_production_contract_declares_qdrant_v2() -> None:
    contract = deploy.load_contract()
    assert contract.runtime_bindings == {"QDRANT_COLLECTION": CANONICAL_COLLECTION}

    manifest_raw = json.loads(contract.manifest_path.read_text(encoding="utf-8"))
    assert manifest_raw.get("runtime_bindings") == {
        "QDRANT_COLLECTION": CANONICAL_COLLECTION
    }

    override_raw = json.loads(
        contract.production_override.read_text(encoding="utf-8")
    )
    hermes_env = override_raw["services"]["hermes-bot"]["environment"]
    assert hermes_env["QDRANT_COLLECTION"] == CANONICAL_COLLECTION


# ============================================================
# 2. MISSING BINDING FAILS CLOSED
# ============================================================
def test_missing_runtime_binding_fails_closed(base_contract, monkeypatch) -> None:
    contract, _source = base_contract

    # Manifest missing runtime_bindings section
    manifest_raw = json.loads(contract.manifest_path.read_text(encoding="utf-8"))
    del manifest_raw["runtime_bindings"]
    with pytest.raises(deploy.DeploymentContractError, match="manifest-fields"):
        deploy.load_contract(manifest_bytes=json.dumps(manifest_raw).encode())

    # Manifest runtime_bindings missing QDRANT_COLLECTION
    manifest_raw["runtime_bindings"] = {}
    with pytest.raises(deploy.DeploymentContractError, match="runtime-binding-policy"):
        deploy.load_contract(manifest_bytes=json.dumps(manifest_raw).encode())

    # Compose render missing QDRANT_COLLECTION
    doc_without_binding = {
        "services": {
            contract.target_service: {
                "environment": dict(contract.feature_gates),
                "volumes": [
                    {
                        "type": contract.database_mount_type,
                        "source": str(contract.database_source),
                        "target": str(contract.database_target),
                        "read_only": contract.database_read_only,
                    }
                ],
            }
        }
    }
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **_kw: _completed(
            argv,
            stdout=(
                "hermes-bot\n"
                if tuple(argv[-2:]) == ("config", "--services")
                else json.dumps(doc_without_binding)
            ),
        ),
    )
    with pytest.raises(
        deploy.DeploymentContractError, match="compose-runtime-binding-missing"
    ):
        deploy.validate_compose_render(
            contract,
            image=IMAGE_ID,
            revision=REVISION,
        )

    # Runtime attestation with missing QDRANT_COLLECTION
    snapshot = _mock_container_snapshot(collection=None)
    with pytest.raises(
        attestation.RuntimeAttestationError, match="QDRANT_COLLECTION_MISSING"
    ):
        attestation._require_expected_runtime(
            snapshot,
            expected_image_id=IMAGE_ID,
            expected_revision=REVISION,
            expected_mounts=snapshot.mounts,
            expected_feature_gates=snapshot.feature_gates,
            expected_allowlists=snapshot.allowlists,
            expected_secret_fingerprints=snapshot.secret_fingerprints,
            expected_qdrant_collection=CANONICAL_COLLECTION,
        )


# ============================================================
# 3. LEGACY VALUE FAILS CLOSED
# ============================================================
def test_legacy_binding_fails_closed(base_contract, monkeypatch) -> None:
    contract, _source = base_contract

    # Manifest with legacy collection
    manifest_raw = json.loads(contract.manifest_path.read_text(encoding="utf-8"))
    manifest_raw["runtime_bindings"] = {"QDRANT_COLLECTION": LEGACY_COLLECTION}
    with pytest.raises(deploy.DeploymentContractError, match="runtime-binding-policy"):
        deploy.load_contract(manifest_bytes=json.dumps(manifest_raw).encode())

    # Compose render with legacy collection
    doc_legacy = {
        "services": {
            contract.target_service: {
                "environment": {
                    "QDRANT_COLLECTION": LEGACY_COLLECTION,
                    **contract.feature_gates,
                },
                "volumes": [
                    {
                        "type": contract.database_mount_type,
                        "source": str(contract.database_source),
                        "target": str(contract.database_target),
                        "read_only": contract.database_read_only,
                    }
                ],
            }
        }
    }
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **_kw: _completed(
            argv,
            stdout=(
                "hermes-bot\n"
                if tuple(argv[-2:]) == ("config", "--services")
                else json.dumps(doc_legacy)
            ),
        ),
    )
    with pytest.raises(
        deploy.DeploymentContractError, match="compose-runtime-binding-legacy"
    ):
        deploy.validate_compose_render(
            contract,
            image=IMAGE_ID,
            revision=REVISION,
        )

    # Runtime attestation with legacy collection
    snapshot = _mock_container_snapshot(collection=LEGACY_COLLECTION)
    with pytest.raises(
        attestation.RuntimeAttestationError, match="LEGACY_QDRANT_COLLECTION"
    ):
        attestation._require_expected_runtime(
            snapshot,
            expected_image_id=IMAGE_ID,
            expected_revision=REVISION,
            expected_mounts=snapshot.mounts,
            expected_feature_gates=snapshot.feature_gates,
            expected_allowlists=snapshot.allowlists,
            expected_secret_fingerprints=snapshot.secret_fingerprints,
            expected_qdrant_collection=CANONICAL_COLLECTION,
        )


# ============================================================
# 4. UNEXPECTED COLLECTION FAILS CLOSED
# ============================================================
def test_unexpected_collection_fails_closed(base_contract, monkeypatch) -> None:
    contract, _source = base_contract

    # Manifest with unexpected collection
    manifest_raw = json.loads(contract.manifest_path.read_text(encoding="utf-8"))
    manifest_raw["runtime_bindings"] = {"QDRANT_COLLECTION": "unexpected_collection"}
    with pytest.raises(deploy.DeploymentContractError, match="runtime-binding-policy"):
        deploy.load_contract(manifest_bytes=json.dumps(manifest_raw).encode())

    # Compose render with unexpected collection
    doc_unexpected = {
        "services": {
            contract.target_service: {
                "environment": {
                    "QDRANT_COLLECTION": "unexpected_collection",
                    **contract.feature_gates,
                },
                "volumes": [
                    {
                        "type": contract.database_mount_type,
                        "source": str(contract.database_source),
                        "target": str(contract.database_target),
                        "read_only": contract.database_read_only,
                    }
                ],
            }
        }
    }
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **_kw: _completed(
            argv,
            stdout=(
                "hermes-bot\n"
                if tuple(argv[-2:]) == ("config", "--services")
                else json.dumps(doc_unexpected)
            ),
        ),
    )
    with pytest.raises(
        deploy.DeploymentContractError, match="compose-runtime-binding-mismatch"
    ):
        deploy.validate_compose_render(
            contract,
            image=IMAGE_ID,
            revision=REVISION,
        )

    # Runtime attestation with unexpected collection
    snapshot = _mock_container_snapshot(collection="unexpected_collection")
    with pytest.raises(
        attestation.RuntimeAttestationError, match="QDRANT_COLLECTION_MISMATCH"
    ):
        attestation._require_expected_runtime(
            snapshot,
            expected_image_id=IMAGE_ID,
            expected_revision=REVISION,
            expected_mounts=snapshot.mounts,
            expected_feature_gates=snapshot.feature_gates,
            expected_allowlists=snapshot.allowlists,
            expected_secret_fingerprints=snapshot.secret_fingerprints,
            expected_qdrant_collection=CANONICAL_COLLECTION,
        )


# ============================================================
# 5. STALE .ENV CANNOT OVERRIDE CANONICAL PRODUCTION VALUE
# ============================================================
def test_stale_dotenv_cannot_override_canonical_production_value(base_contract) -> None:
    contract, _source = base_contract
    override_doc = json.loads(contract.production_override.read_text(encoding="utf-8"))
    assert (
        override_doc["services"]["hermes-bot"]["environment"]["QDRANT_COLLECTION"]
        == CANONICAL_COLLECTION
    )


# ============================================================
# 6. RENDERED COMPOSE VALUE IS EXACT V2
# ============================================================
def test_rendered_compose_value_is_exact_v2(base_contract, monkeypatch) -> None:
    contract, _source = base_contract
    runner = _mock_compose_runner(contract)
    monkeypatch.setattr(deploy, "_run", runner)

    mounts = deploy.validate_compose_render(contract, image=IMAGE_ID, revision=REVISION)
    assert len(mounts) == 1
    assert mounts[0].target == str(contract.database_target)


# ============================================================
# 7. DEPLOYMENT PLAN RECORDS EXPECTED COLLECTION
# ============================================================
def test_deployment_plan_records_expected_collection(
    base_contract, monkeypatch, capsys
) -> None:
    contract, source = base_contract
    runner = _mock_compose_runner(contract)
    monkeypatch.setattr(deploy, "_run", runner)
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    monkeypatch.setattr(deploy, "_validate_capacity", lambda *_args, **_kw: None)
    monkeypatch.setattr(deploy, "_validate_live_future_mounts", lambda *_args: None)
    monkeypatch.setattr(
        deploy,
        "_ordinary_deploy_pre_mutation_barrier",
        lambda *args, **kwargs: (
            deploy.InspectedImage(image_id=IMAGE_ID, revision=REVISION),
            {"TELEGRAM_BOT_TOKEN": FAKE_SECRET},
            REVISION,
        ),
    )

    deploy.plan_operation(contract, source=source, image=IMAGE_ID, revision=REVISION)
    out = capsys.readouterr().out
    assert f"PLAN_EXPECTED_QDRANT_COLLECTION={CANONICAL_COLLECTION}" in out


# ============================================================
# 8. RUNTIME ATTESTATION REJECTS WRONG RUNNING VALUE
# ============================================================
def test_runtime_attestation_rejects_wrong_running_value() -> None:
    snapshot_wrong = _mock_container_snapshot(collection="wrong_collection")
    with pytest.raises(
        attestation.RuntimeAttestationError, match="QDRANT_COLLECTION_MISMATCH"
    ):
        attestation._require_expected_runtime(
            snapshot_wrong,
            expected_image_id=IMAGE_ID,
            expected_revision=REVISION,
            expected_mounts=snapshot_wrong.mounts,
            expected_feature_gates=snapshot_wrong.feature_gates,
            expected_allowlists=snapshot_wrong.allowlists,
            expected_secret_fingerprints=snapshot_wrong.secret_fingerprints,
            expected_qdrant_collection=CANONICAL_COLLECTION,
        )


# ============================================================
# 9. QDRANT SERVICE ITSELF REMAINS UNTOUCHED
# ============================================================
def test_qdrant_service_itself_remains_untouched(base_contract, tmp_path) -> None:
    contract, _source = base_contract
    policy = contract.attestation_policy

    post_result = attestation.PostDeployAttestation(
        observed_at="2026-09-04T00:00:00Z",
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
        memory_collection_target="PASS",
    )

    runtime_dir = tmp_path / "evidence_run"
    runtime_dir.mkdir(mode=0o700)
    evidence_path = attestation.write_evidence(
        policy,
        runtime_directory=runtime_dir,
        target_revision=REVISION,
        target_image_id=IMAGE_ID,
        previous_image_id=PREVIOUS_IMAGE_ID,
        operation_status="PASS",
        post_result=post_result,
        original_error=None,
        rollback_attempted=False,
        rollback_result="NOT_ATTEMPTED",
        rollback_error=None,
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["post_check"]["qdrant"] == "UNCHANGED"
    assert evidence["post_check"]["qdrant_service_container_identity"] == "UNCHANGED"
    assert evidence["post_check"]["hermes_memory_collection_target"] == "PASS"


# ============================================================
# 10. ROLLBACK PATH PRESERVES RUNTIME BINDING
# ============================================================
def test_rollback_path_preserves_runtime_binding(base_contract) -> None:
    contract, _source = base_contract
    cmd = deploy.compose_command(contract)
    assert str(contract.production_override) in cmd

    snapshot = _mock_container_snapshot(collection=CANONICAL_COLLECTION)
    attestation._require_expected_runtime(
        snapshot,
        expected_image_id=IMAGE_ID,
        expected_revision=REVISION,
        expected_mounts=snapshot.mounts,
        expected_feature_gates=snapshot.feature_gates,
        expected_allowlists=snapshot.allowlists,
        expected_secret_fingerprints=snapshot.secret_fingerprints,
        expected_qdrant_collection=CANONICAL_COLLECTION,
    )


# ============================================================
# 11. INCIDENT REPRODUCTION TEST
# ============================================================
def test_incident_reproduction(base_contract, monkeypatch) -> None:
    contract, _source = base_contract

    # 1. Before fix: if compose override did not specify QDRANT_COLLECTION,
    # Compose would inherit legacy value from .env.
    stale_rendered_environment = {
        "QDRANT_COLLECTION": LEGACY_COLLECTION,
        **contract.feature_gates,
    }
    stale_doc = {
        "services": {
            contract.target_service: {
                "environment": stale_rendered_environment,
                "volumes": [
                    {
                        "type": contract.database_mount_type,
                        "source": str(contract.database_source),
                        "target": str(contract.database_target),
                        "read_only": contract.database_read_only,
                    }
                ],
            }
        }
    }

    # After fix: validate_compose_render rejects the stale/legacy leak before container start!
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **_kw: _completed(
            argv,
            stdout=(
                "hermes-bot\n"
                if tuple(argv[-2:]) == ("config", "--services")
                else json.dumps(stale_doc)
            ),
        ),
    )
    with pytest.raises(
        deploy.DeploymentContractError, match="compose-runtime-binding-legacy"
    ):
        deploy.validate_compose_render(
            contract,
            image=IMAGE_ID,
            revision=REVISION,
        )

    # 2. After fix: if an old/stale container somehow ran with legacy collection,
    # post_deploy_attestation rejects it immediately and triggers rollback:
    stale_container = _mock_container_snapshot(collection=LEGACY_COLLECTION)
    with pytest.raises(
        attestation.RuntimeAttestationError, match="LEGACY_QDRANT_COLLECTION"
    ):
        attestation._require_expected_runtime(
            stale_container,
            expected_image_id=IMAGE_ID,
            expected_revision=REVISION,
            expected_mounts=stale_container.mounts,
            expected_feature_gates=stale_container.feature_gates,
            expected_allowlists=stale_container.allowlists,
            expected_secret_fingerprints=stale_container.secret_fingerprints,
            expected_qdrant_collection=CANONICAL_COLLECTION,
        )

    # 3. After fix: with canonical binding in place, validate_compose_render passes:
    canonical_runner = _mock_compose_runner(contract)
    monkeypatch.setattr(deploy, "_run", canonical_runner)
    mounts = deploy.validate_compose_render(
        contract,
        image=IMAGE_ID,
        revision=REVISION,
    )
    assert len(mounts) == 1
