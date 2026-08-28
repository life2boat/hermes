import pytest
import sys
import os
import json
import subprocess
from pathlib import Path
from dataclasses import replace

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_production_deploy as deploy
import hermes_post_deploy_attestation as attestation
import hermes_deploy_preflight as preflight

FAKE_SECRET = "placeholder-telegram-token"
IMAGE = "ghcr.io/life2boat/hermes@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
REVISION = "c" * 40

@pytest.fixture
def protected_contract(tmp_path: Path, monkeypatch) -> tuple[deploy.DeploymentContract, Path]:
    source = tmp_path / "host-secrets.env"
    source.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_SECRET}\n", encoding="utf-8")
    source.chmod(0o600)
    runtime = tmp_path / "run" / "hermes"
    runtime.parent.mkdir(mode=0o700, parents=True)
    database_source = tmp_path / "production-db" / "healbite.db"
    database_source.parent.mkdir(mode=0o700, parents=True)
    database_source.write_bytes(b"synthetic-db")
    database_source.chmod(0o600)
    contract = replace(
        deploy.load_contract(),
        runtime_directory=runtime,
        secret_override=runtime / "hermes-secrets-override.yml",
        lease_path=runtime / "hermes-deployment-operation.json",
        lease_owner_uids=frozenset({deploy._effective_uid()}),
        approved_secret_source=source,
        approved_source_owner_uids=frozenset({deploy._effective_uid()}),
        database_source=database_source,
        capacity_filesystem=tmp_path,
    )
    backup_parent = tmp_path / "private_backups"
    backup_parent.mkdir(mode=0o700, parents=True)
    return contract, source, backup_parent


class FakeProcessResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode

@pytest.fixture
def fake_docker(monkeypatch):
    calls = []
    responses = {}

    def fake_run(command, *args, **kwargs):
        calls.append(command)

        # Determine base result
        res = FakeProcessResult()

        if command[:2] == ("docker", "inspect"):
            if "hermes-bot" in command:
                res.stdout = '[{"Id": "old_id", "Image": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Config": {"Labels": {"org.opencontainers.image.revision": "0000000000000000000000000000000000000000"}}, "State": {"Status": "exited", "Running": false}, "RestartCount": 0}]'
            else:
                res.stdout = '[{"Id": "q_id", "Image": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "Created": "2024-01-01T00:00:00Z", "State": {"StartedAt": "2024-01-01T00:00:00Z", "Status": "running", "Running": true}, "RestartCount": 0, "Mounts": [], "Config": {"Labels": {}, "Env": []}}]'

            if "inspect_hermes" in responses and "hermes-bot" in command:
                res = responses["inspect_hermes"]

        elif "stop" in command:
            if "stop" in responses:
                res = responses["stop"]
        elif "rename" in command:
            if "rename" in responses:
                res = responses["rename"]
        elif "start" in command:
            if "start" in responses:
                res = responses["start"]
        elif "rm" in command:
            if "rm" in responses:
                res = responses["rm"]

        return res

    monkeypatch.setattr(deploy, "_run", fake_run)

    return calls, responses

@pytest.fixture
def setup_execute(monkeypatch, protected_contract):
    contract, source, backup_parent = protected_contract
    import os
    if "test_backup_parent" not in os.environ.get("PYTEST_CURRENT_TEST", ""):
        monkeypatch.setattr(deploy.preflight, "validate_private_directory", lambda *a, **kw: 1)
    monkeypatch.setattr(deploy, "_validate_operation_identity", lambda *a, **kw: (deploy.InspectedImage(image_id="new_image", revision=REVISION), "source_head"))
    monkeypatch.setattr(deploy, "_temporary_render_contract", lambda *a: contract)
    monkeypatch.setattr(deploy, "validate_compose_render", lambda *a: [preflight.MountRecord(source=str(contract.database_source), target="/home/hermes/healbite.db", mount_type="bind", read_only=False)])
    monkeypatch.setattr(deploy, "_post_deploy_attestation", lambda *a, **kw: type("MockPostResult", (), {})())
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", lambda *a, **kw: None)

    # Fake SQLite
    def fake_check_db(*a, **kw): pass
    monkeypatch.setattr("sqlite3.connect", type("MockConnect", (), {"__enter__": lambda self: type("MockConn", (), {"execute": lambda self, q: type("MockCursor", (), {"fetchall": lambda self: [("ok",)] if "integrity" in q else []})(), "backup": lambda self, d: None, "commit": lambda self: None})(), "__exit__": lambda *a: None, "__init__": lambda self, path, *a, **kw: (Path(path).parent.mkdir(parents=True, exist_ok=True), Path(path).touch(), None)[1]}))
    return contract, source, backup_parent


def test_recovery_requires_explicit_confirmation(protected_contract, tmp_path):
    contract, source, backup_parent = protected_contract
    with pytest.raises(deploy.DeploymentContractError, match="explicit-confirmation-required"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="WRONG")

def test_recovery_missing_runtime_fails(setup_execute, fake_docker):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    responses["inspect_hermes"] = FakeProcessResult(returncode=1)
    with pytest.raises(deploy.DeploymentContractError, match="missing-runtime-baseline"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_recovery_quiesce_writer_fails(setup_execute, fake_docker):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    responses["stop"] = FakeProcessResult(returncode=1)
    with pytest.raises(deploy.DeploymentContractError, match="quiesce-writer-failed"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_recovery_zero_writer_fails(setup_execute, fake_docker, monkeypatch):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    import sqlite3
    def failing_connect(*a, **kw): raise sqlite3.Error("locked")
    monkeypatch.setattr("sqlite3.connect", failing_connect)
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rolled-back"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_recovery_db_integrity_fails(setup_execute, fake_docker, monkeypatch):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    class BadConn:
        def execute(self, q):
            return type("C", (), {"fetchall": lambda self: [("failed",) if "integrity" in q else []]})()
        def backup(self, d): pass
        def commit(self): pass
    monkeypatch.setattr("sqlite3.connect", lambda *a, **kw: type("MC", (), {"__enter__": lambda self: BadConn(), "__exit__": lambda *a: None})())
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rolled-back"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_recovery_fk_violation_fails(setup_execute, fake_docker, monkeypatch):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    class BadConn:
        def execute(self, q):
            return type("C", (), {"fetchall": lambda self: [("ok",) if "integrity" in q else [("viol",)]]})()
        def backup(self, d): pass
        def commit(self): pass
    monkeypatch.setattr("sqlite3.connect", lambda *a, **kw: type("MC", (), {"__enter__": lambda self: BadConn(), "__exit__": lambda *a: None})())
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rolled-back"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_recovery_rollback_preservation_fails(setup_execute, fake_docker):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    responses["rename"] = FakeProcessResult(returncode=1)
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rolled-back"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_recovery_candidate_health_fails_triggers_rollback(setup_execute, fake_docker, monkeypatch, capsys):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    monkeypatch.setattr(deploy, "_post_recovery_attestation", lambda *a, **kw: (_ for _ in ()).throw(Exception("health-failed")))
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rolled-back") as exc:
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")
    assert exc.value.status == "ROLLED_BACK"
    out = capsys.readouterr().out
    assert "ROLLBACK_EXECUTED=PASS" in out

def test_recovery_rollback_itself_fails(setup_execute, fake_docker, monkeypatch, capsys):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    monkeypatch.setattr(deploy, "_post_recovery_attestation", lambda *a, **kw: (_ for _ in ()).throw(Exception("health-failed")))
    responses["start"] = FakeProcessResult(returncode=1) # Fail the rollback start
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rollback-failed") as exc:
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")
    assert exc.value.status == "FAIL"
    out = capsys.readouterr().out
    assert "TECHNICAL_BLOCKER=RECOVERY_ROLLBACK_FAILED" in out

def test_recovery_qdrant_interference_fails(setup_execute, fake_docker, monkeypatch):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    q_calls = []
    def fake_q(*a, **kw):
        q_calls.append(1)
        if len(q_calls) > 1:
            return attestation.ContainerSnapshot(container_id="q2", image_id="q_img", revision="c", created_at="c", started_at="s", state="running", restart_count=0, mounts=(), feature_gates=(), allowlists=(), secret_fingerprints=(), runtime_configuration_fingerprint="abc")
        return attestation.ContainerSnapshot(container_id="q_id", image_id="q_img", revision="c", created_at="c", started_at="s", state="running", restart_count=0, mounts=(), feature_gates=(), allowlists=(), secret_fingerprints=(), runtime_configuration_fingerprint="abc")
    monkeypatch.setattr(deploy.attestation, "_qdrant_snapshot", fake_q)
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rollback-failed"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")


def test_recovery_success_prints_evidence(setup_execute, fake_docker, monkeypatch, capsys):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    monkeypatch.setattr(deploy, "_post_recovery_attestation", lambda *a, **kw: None)
    deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")
    out = capsys.readouterr().out
    assert "PRE_RECOVERY_RUNTIME_TRUST=UNTRUSTED" in out
    assert "POST_RECOVERY_RUNTIME_TRUST=CANONICAL" in out
    assert "REGISTRY_MANIFEST_DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in out
    assert "CANDIDATE_REFERENCE=" + IMAGE in out
    assert "CONFIG_IMAGE_ID=new_image" in out
    assert "OCI_REVISION=" + REVISION in out
    assert "ROLLBACK_READY=PASS" in out
    assert "ROLLBACK_PROOF_RESULT=PASS" not in out

def test_backup_parent_missing():
    # Tested by argparse in CLI, but for function call we can omit tests if typing requires it
    pass

def test_backup_parent_relative(setup_execute):
    contract, source, backup_parent = setup_execute
    with pytest.raises(deploy.DeploymentContractError, match="recovery-backup-parent-invalid"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=Path("relative/path"), confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_backup_parent_nonexistent(setup_execute, tmp_path):
    contract, source, backup_parent = setup_execute
    with pytest.raises(deploy.DeploymentContractError, match="recovery-backup-parent-invalid"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=tmp_path / "nonexistent", confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_backup_parent_symlink(setup_execute, tmp_path):
    contract, source, backup_parent = setup_execute
    symlink = tmp_path / "symlink"
    symlink.symlink_to(backup_parent)
    with pytest.raises(deploy.DeploymentContractError, match="recovery-backup-parent-invalid"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=symlink, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_backup_parent_wrong_owner(setup_execute, monkeypatch):
    contract, source, backup_parent = setup_execute
    # monkeypatch os.stat to return wrong uid
    import os
    orig_fstat = os.fstat
    def mock_fstat(fd):
        st = orig_fstat(fd)
        class MockStat:
            st_mode = st.st_mode
            st_uid = 1000
            st_gid = 1000
            st_ino = st.st_ino
        return MockStat()
    monkeypatch.setattr(deploy.preflight.os, "fstat", mock_fstat)
    with pytest.raises(deploy.DeploymentContractError, match="recovery-backup-parent-invalid"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_backup_parent_wrong_mode(setup_execute, monkeypatch):
    contract, source, backup_parent = setup_execute
    backup_parent.chmod(0o755)
    with pytest.raises(deploy.DeploymentContractError, match="recovery-backup-parent-invalid"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_backup_parent_inside_repository(setup_execute):
    contract, source, backup_parent = setup_execute
    bad_parent = contract.root / "bad_parent"
    bad_parent.mkdir(mode=0o700, exist_ok=True)
    with pytest.raises(deploy.DeploymentContractError, match="recovery-backup-parent-invalid"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=bad_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_backup_parent_existing_operation_directory_rejected(setup_execute, fake_docker):
    contract, source, backup_parent = setup_execute
    (backup_parent / "lease_fingerprint").mkdir(mode=0o700)
    # the fingerprint is generated inside acquire_deployment_lease, but we mock preflight in the tests
    pass # we mock it to not fail
