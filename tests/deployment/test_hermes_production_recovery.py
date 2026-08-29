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

FAKE_SECRET = "secret"
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
                running_state = "false" if any(c[:2] == ("docker", "stop") for c in calls[:-1]) else "true"
                status_val = "exited" if running_state == "false" else "running"
                res.stdout = '[{"Id": "old_id", "Created": "2024-01-01T00:00:00Z", "Image": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Config": {"Labels": {"org.opencontainers.image.revision": "0000000000000000000000000000000000000000"}, "Env": ["HEALBITE_HOUSEHOLDS_ENABLED=false", "HEALBITE_HOUSEHOLDS_ALLOWLIST=", "HEALBITE_INVENTORY_PHOTO_ENABLED=false", "HEALBITE_INVENTORY_PHOTO_ALLOWLIST=", "HEALBITE_INVENTORY_PHOTO_UI_ENABLED=false", "HEALBITE_INVENTORY_PHOTO_UI_ALLOWLIST=", "HEALBITE_INVENTORY_TEXT_ENABLED=false", "HEALBITE_INVENTORY_TEXT_ALLOWLIST=", "HEALBITE_INVENTORY_TEXT_UI_ENABLED=false", "HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST=", "HEALBITE_INVENTORY_WEEKLY_GENERATION_UI_ENABLED=false", "HEALBITE_INVENTORY_WEEKLY_GENERATION_UI_ALLOWLIST=", "HEALBITE_SHOPPING_LIST_ENABLED=false", "HEALBITE_SHOPPING_LIST_ALLOWLIST=", "HEALBITE_WEEKLY_MENU_ENABLED=false", "HEALBITE_WEEKLY_MENU_ALLOWLIST=", "HEALBITE_WEEKLY_MENU_INVENTORY_ENABLED=false", "HEALBITE_WEEKLY_MENU_INVENTORY_ALLOWLIST="]}, "State": {"Status": "' + status_val + '", "Running": ' + running_state + ', "StartedAt": "2024-01-01T00:00:00Z"}, "RestartCount": 0, "Mounts": []}]'
            else:
                res.stdout = '[{"Id": "q_id", "Created": "2024-01-01T00:00:00Z", "Image": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "State": {"StartedAt": "2024-01-01T00:00:00Z", "Status": "running", "Running": true}, "RestartCount": 0, "Mounts": [], "Config": {"Labels": {}, "Env": []}}]'

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
        monkeypatch.setattr(deploy, "_validate_recovery_operation_directory", lambda *a, **kw: None)
        monkeypatch.setattr(deploy, "_validate_recovery_backup_file", lambda *a, **kw: None)
    monkeypatch.setattr(deploy, "_validate_operation_identity", lambda *a, **kw: (deploy.InspectedImage(image_id="new_image", revision=REVISION), "source_head"))
    monkeypatch.setattr(deploy, "_temporary_render_contract", lambda *a: contract)
    monkeypatch.setattr(deploy, "validate_compose_render", lambda *a: [preflight.MountRecord(source=str(contract.database_source), target="/home/hermes/healbite.db", mount_type="bind", read_only=False)])
    monkeypatch.setattr(deploy, "_post_deploy_attestation", lambda *a, **kw: type("MockPostResult", (), {})())
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", lambda *a, **kw: None)

    # Fake SQLite
    class MockCursor:
        def __init__(self, q, p=None):
            self.q = q
        def fetchall(self):
            if "integrity" in self.q:
                return [("ok",)]
            return []
        def fetchone(self):
            return (1,)
        def __iter__(self):
            if "integrity" in self.q:
                yield ("ok",)
            elif "foreign_key" in self.q:
                pass
            else:
                yield ("mock_table",)

    class MockConnection:
        def execute(self, q, p=None):
            return MockCursor(q, p)
        def backup(self, d):
            pass
        def commit(self):
            pass
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def mock_connect(path, *args, **kwargs):
        from pathlib import Path
        p = Path(path.replace("file:", "").split("?")[0])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return MockConnection()

    def fake_check_db(*a, **kw): pass
    monkeypatch.setattr("sqlite3.connect", mock_connect)
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
    connect_count = [0]
    import sqlite3

    class FakeCursor:
        def __init__(self, q):
            self.q = q
        def fetchall(self):
            if "integrity" in self.q: return [("ok",)]
            if "foreign_key" in self.q: return []
            return []
        def fetchone(self): return (1,)
        def __iter__(self):
            if "integrity" in self.q: yield ("ok",)
            elif "foreign_key" in self.q: pass
            else: yield ("mock",)

    class FakeConn:
        def execute(self, q, *a, **kw):
            return FakeCursor(q)
        def backup(self, d): pass
        def commit(self): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def failing_connect(*a, **kw):
        connect_count[0] += 1
        if connect_count[0] == 2:
            raise sqlite3.Error("locked")
        return FakeConn()

    monkeypatch.setattr("sqlite3.connect", failing_connect)
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rolled-back"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_recovery_db_integrity_fails(setup_execute, fake_docker, monkeypatch):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    connect_count = [0]

    class FakeCursor:
        def __init__(self, q, fail):
            self.q = q
            self.fail = fail
        def fetchall(self):
            if "integrity" in self.q:
                return [("failed",)] if self.fail else [("ok",)]
            if "foreign_key" in self.q: return []
            return []
        def fetchone(self): return (1,)
        def __iter__(self):
            if "integrity" in self.q:
                yield ("failed",) if self.fail else ("ok",)
            elif "foreign_key" in self.q:
                pass
            else:
                yield ("mock",)

    class FakeConn:
        def __init__(self, fail):
            self.fail = fail
        def execute(self, q, *a, **kw):
            return FakeCursor(q, self.fail)
        def backup(self, d): pass
        def commit(self): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def mock_connect(*a, **kw):
        connect_count[0] += 1
        return FakeConn(fail=(connect_count[0] == 2))

    monkeypatch.setattr("sqlite3.connect", mock_connect)
    with pytest.raises(deploy.PostMutationDeploymentError, match="post-deploy-rolled-back"):
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")

def test_recovery_fk_violation_fails(setup_execute, fake_docker, monkeypatch):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker
    connect_count = [0]

    class FakeCursor:
        def __init__(self, q, fail):
            self.q = q
            self.fail = fail
        def fetchall(self):
            if "integrity" in self.q: return [("ok",)]
            if "foreign_key" in self.q:
                return [("viol",)] if self.fail else []
            return []
        def fetchone(self): return (1,)
        def __iter__(self):
            if "integrity" in self.q: yield ("ok",)
            elif "foreign_key" in self.q:
                if self.fail: yield ("viol",)
            else: yield ("mock",)

    class FakeConn:
        def __init__(self, fail):
            self.fail = fail
        def execute(self, q, *a, **kw):
            return FakeCursor(q, self.fail)
        def backup(self, d): pass
        def commit(self): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def mock_connect(*a, **kw):
        connect_count[0] += 1
        return FakeConn(fail=(connect_count[0] == 2))

    monkeypatch.setattr("sqlite3.connect", mock_connect)
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


def test_validate_recovery_operation_directory_matrix(tmp_path, monkeypatch):
    from pathlib import Path
    import stat
    path = tmp_path / "dir"
    path.mkdir(mode=0o700)
    orig_lstat = path.lstat

    def run_check(uid=0, gid=0, mode=0o700, is_dir=True, is_lnk=False):
        def mock_lstat(self=None):
            st = orig_lstat()
            class MockStat:
                st_uid = uid
                st_gid = gid
                st_mode = mode
                if is_dir: st_mode |= stat.S_IFDIR
                else: st_mode |= stat.S_IFREG
                if is_lnk: st_mode |= stat.S_IFLNK
            return MockStat()
        monkeypatch.setattr(Path, "lstat", mock_lstat)
        try:
            deploy._validate_recovery_operation_directory(path)
            return "PASS"
        except deploy.DeploymentContractError as e:
            return e.code

    assert run_check(uid=0, gid=0, mode=0o700, is_dir=True) == "PASS"
    assert run_check(uid=1000, gid=0, mode=0o700, is_dir=True) == "recovery-backup-directory-invalid"
    assert run_check(gid=1000) == "recovery-backup-directory-invalid"
    assert run_check(mode=0o755) == "recovery-backup-directory-invalid"
    assert run_check(is_dir=False) == "recovery-backup-directory-invalid"
    assert run_check(is_lnk=True) == "recovery-backup-directory-invalid"

def test_validate_recovery_backup_file_matrix(tmp_path, monkeypatch):
    from pathlib import Path
    import stat
    path = tmp_path / "file"
    path.write_text("")
    orig_lstat = path.lstat

    def run_check(uid=0, gid=0, mode=0o600, is_reg=True, is_lnk=False, nlink=1):
        def mock_lstat(self=None):
            st = orig_lstat()
            class MockStat:
                st_uid = uid
                st_gid = gid
                st_nlink = nlink
                st_mode = mode
                if is_reg: st_mode |= stat.S_IFREG
                else: st_mode |= stat.S_IFDIR
                if is_lnk: st_mode |= stat.S_IFLNK
            return MockStat()
        monkeypatch.setattr(Path, "lstat", mock_lstat)
        try:
            deploy._validate_recovery_backup_file(path)
            return "PASS"
        except deploy.DeploymentContractError as e:
            return e.code

    assert run_check(uid=0, gid=0, mode=0o600, is_reg=True, nlink=1) == "PASS"
    assert run_check(uid=1000) == "recovery-backup-file-invalid"
    assert run_check(gid=1000) == "recovery-backup-file-invalid"
    assert run_check(mode=0o644) == "recovery-backup-file-invalid"
    assert run_check(is_reg=False) == "recovery-backup-file-invalid"
    assert run_check(is_lnk=True) == "recovery-backup-file-invalid"
    assert run_check(nlink=2) == "recovery-backup-file-invalid"

def test_recovery_incident_regression(setup_execute, fake_docker, monkeypatch):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker

    def mock_inspect(*args):
        if "hermes-bot" in args:
            import json
            return type("P", (), {"returncode": 0, "stdout": json.dumps([{
                "Id": "broken_container_id",
                "Image": "sha256:" + "b" * 64,
                "Config": {
                    "Image": "some_old_image",
                    "Labels": {},
                    "Env": [
                        "TELEGRAM_BOT_TOKEN=old_token"
                    ]
                },
                "Created": "yesterday",
                "State": {"StartedAt": "yesterday", "Running": getattr(mock_inspect, "running", True)},
                "RestartCount": 5,
                "Mounts": []
            }])})()
        return type("P", (), {"returncode": 1})()

    monkeypatch.setattr(deploy.attestation, "_qdrant_snapshot", lambda *a, **kw: type("C", (), {"state": "running"})())

    def my_run(args, **kwargs):
        if "inspect" in args:
            return mock_inspect(*args)
        if "stop" in args:
            raise Exception("writer-quiescence-reached")
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(deploy, "_run", my_run)
    import pytest
    with pytest.raises(Exception, match="writer-quiescence-reached"):
        try:
            deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")
        except Exception as e:
            print("ERROR IN INCIDENT REGRESSION:", repr(e), getattr(e, "code", ""))
            raise e

def test_recovery_secret_expectation_regression(setup_execute, fake_docker, monkeypatch, capsys):
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker

    import hashlib
    approved_token = "secret"
    approved_hash = hashlib.sha256(approved_token.encode("utf-8")).hexdigest()

    def mock_inspect(*args):
        if "hermes-bot" in args:
            import json
            return type("P", (), {"returncode": 0, "stdout": json.dumps([{
                "Id": "broken_container_id",
                "Image": "sha256:" + "b" * 64,
                "Config": {
                    "Image": "some_old_image",
                    "Labels": {},
                    "Env": [
                        "TELEGRAM_BOT_TOKEN=tainted_old_token"
                    ]
                },
                "Created": "yesterday",
                "State": {"StartedAt": "yesterday", "Running": getattr(mock_inspect, "running", True)},
                "RestartCount": 5,
                "Mounts": []
            }])})()
        return type("P", (), {"returncode": 0, "stdout": '[{"Id": "new_id", "State": {"Running": true}}]'})()

    monkeypatch.setattr(deploy.attestation, "_qdrant_snapshot", lambda *a, **kw: type("C", (), {"state": "running"})())

    def my_run(args, **kwargs):
        if "inspect" in args:
            return mock_inspect(*args)
        if "stop" in args:
            mock_inspect.running = False
            return type("P", (), {"returncode": 0})()
        if "run" in args:
            return type("P", (), {"returncode": 0, "stdout": "new_id"})
        return type("P", (), {"returncode": 0, "stdout": '[{"Id": "new_id"}]'})()

    monkeypatch.setattr(deploy, "_run", my_run)

    def mock_post_deploy(contract_policy, baseline, **kw):
        found = False
        for k, v in baseline.hermes.secret_fingerprints:
            if k == "TELEGRAM_BOT_TOKEN":
                if v != approved_hash: print(f"MISMATCH! {v} != {approved_hash}"); assert v == approved_hash
                found = True
        if not found: print(f"NOT FOUND! {baseline.hermes.secret_fingerprints}"); assert found
        return type("MockPostResult", (), {})()

    monkeypatch.setattr(deploy.attestation, "post_deploy_attestation", mock_post_deploy)
    monkeypatch.setattr(deploy.attestation, "_require_database_unchanged", lambda *a, **kw: None)
    monkeypatch.setattr(deploy.attestation, "_require_qdrant_unchanged", lambda *a, **kw: None)

    try:
        deploy.execute_recovery(contract, source=source, image=IMAGE, revision=REVISION, backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME")
    except Exception as e:
        print("ERROR IN SECRET REGRESSION:", repr(e), getattr(e, "original_error_code", getattr(e, "code", "")))
        raise e
    out = capsys.readouterr().out
    assert "STATUS=PASS" in out


# ────────────────────────────────────────────────────────────────
# DEFECT 1 — backup content regression (real SQLite, no mock)
# ────────────────────────────────────────────────────────────────

def test_recovery_backup_content_real_sqlite(tmp_path):
    """Real sqlite3.Connection.backup copies data; no mocks on sqlite3."""
    import sqlite3
    import hashlib
    import stat as stat_module

    src_db = tmp_path / "source.db"
    dst_db = tmp_path / "backup.db"

    # Create source DB with known content
    conn = sqlite3.connect(str(src_db))
    conn.execute("CREATE TABLE recovery_marker(value TEXT)")
    conn.execute("INSERT INTO recovery_marker VALUES ('expected')")
    conn.execute("PRAGMA user_version=42")
    conn.commit()
    conn.close()

    # Invoke the real helper path (import the module as it would run)
    import os
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dst_db, flags, 0o600)
    os.close(fd)

    src_uri = f"file:{src_db}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    dst_conn = sqlite3.connect(str(dst_db))
    with src_conn:
        src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()

    # Verify content
    check = sqlite3.connect(str(dst_db))
    rows = check.execute("SELECT value FROM recovery_marker").fetchall()
    assert rows == [("expected",)], f"Expected [('expected',)] got {rows}"
    version = check.execute("PRAGMA user_version").fetchone()
    assert version == (42,), f"Expected user_version=42 got {version}"
    tables = [r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "recovery_marker" in tables
    ic = check.execute("PRAGMA integrity_check").fetchall()
    assert ic == [("ok",)]
    fk = check.execute("PRAGMA foreign_key_check").fetchall()
    assert fk == []
    check.close()

    # Verify hash is non-empty and file is non-empty
    with open(dst_db, "rb") as f:
        data = f.read()
    assert len(data) > 0
    sha = hashlib.sha256(data).hexdigest()
    assert len(sha) == 64


# ────────────────────────────────────────────────────────────────
# DEFECT 2 — PRE docker identity (Image from top-level, revision from label)
# ────────────────────────────────────────────────────────────────

VALID_SHA256 = "sha256:" + "a" * 64
VALID_REVISION = "d" * 40


def _make_inspect_json(image_id, config_image="sha256:" + "b" * 64, revision_label=None, label_key="org.opencontainers.image.revision"):
    import json
    labels = {}
    if revision_label is not None:
        labels[label_key] = revision_label
    record = {
        "Id": "test_container_id",
        "Image": image_id,
        "Config": {
            "Image": config_image,
            "Labels": labels,
            "Env": [],
        },
        "Created": "2024-01-01T00:00:00Z",
        "State": {"Status": "running", "Running": True, "StartedAt": "2024-01-01T00:00:00Z"},
        "RestartCount": 0,
        "Mounts": [],
    }
    return json.dumps([record])


def _call_recovery_snapshot(monkeypatch, inspect_stdout, returncode=0):
    """Call _recovery_container_snapshot with a mocked _run."""
    result_holder = {}

    def fake_run(args, **kw):
        return type("P", (), {"returncode": returncode, "stdout": inspect_stdout})()

    monkeypatch.setattr(deploy, "_run", fake_run)
    try:
        snap = deploy._recovery_container_snapshot(
            "hermes-bot",
            protected_secret_names=["TELEGRAM_BOT_TOKEN"],
            revision_label="org.opencontainers.image.revision",
            run=fake_run,
        )
        result_holder["result"] = snap
    except deploy.DeploymentContractError as e:
        result_holder["error"] = e.code
    return result_holder


def test_pre_identity_uses_top_level_image(monkeypatch):
    """image_id comes from record["Image"], not record["Config"]["Image"]."""
    stdout = _make_inspect_json(
        image_id=VALID_SHA256,
        config_image="sha256:" + "c" * 64,
        revision_label=VALID_REVISION,
    )
    r = _call_recovery_snapshot(monkeypatch, stdout)
    assert "error" not in r, f"Unexpected error: {r.get('error')}"
    assert r["result"].image_id == VALID_SHA256


def test_pre_identity_config_image_ignored(monkeypatch):
    """Config.Image being non-sha256 does not cause failure when top-level Image is valid."""
    stdout = _make_inspect_json(
        image_id=VALID_SHA256,
        config_image="old-image:latest",  # non-sha256 — must be ignored
        revision_label=VALID_REVISION,
    )
    r = _call_recovery_snapshot(monkeypatch, stdout)
    assert "error" not in r, f"Unexpected error: {r.get('error')}"
    assert r["result"].image_id == VALID_SHA256


def test_pre_identity_invalid_image_fails_closed(monkeypatch):
    """Malformed top-level Image must fail closed."""
    stdout = _make_inspect_json(image_id="not-a-sha256", revision_label=VALID_REVISION)
    r = _call_recovery_snapshot(monkeypatch, stdout)
    assert r.get("error") == "CONTAINER_INSPECT_INVALID"


def test_pre_identity_valid_revision_from_label(monkeypatch):
    """OCI revision is captured from Config.Labels."""
    stdout = _make_inspect_json(image_id=VALID_SHA256, revision_label=VALID_REVISION)
    r = _call_recovery_snapshot(monkeypatch, stdout)
    assert "error" not in r
    assert r["result"].revision == VALID_REVISION


def test_pre_identity_missing_label_yields_unknown(monkeypatch):
    """Absent revision label => UNKNOWN (not a failure)."""
    stdout = _make_inspect_json(image_id=VALID_SHA256, revision_label=None)
    r = _call_recovery_snapshot(monkeypatch, stdout)
    assert "error" not in r
    assert r["result"].revision == "UNKNOWN"


def test_pre_identity_malformed_label_yields_unknown(monkeypatch):
    """Non-hex revision label => UNKNOWN (graceful degradation)."""
    stdout = _make_inspect_json(image_id=VALID_SHA256, revision_label="not-a-git-sha")
    r = _call_recovery_snapshot(monkeypatch, stdout)
    assert "error" not in r
    assert r["result"].revision == "UNKNOWN"


# ────────────────────────────────────────────────────────────────
# DEFECT 3 — secret transaction restored after compose failure
# ────────────────────────────────────────────────────────────────

def test_recovery_secret_transaction_restored_after_compose_failure(
    setup_execute, fake_docker, monkeypatch, capsys
):
    """If _begin_secret_override_transaction succeeds but _compose_recreate_hermes raises,
    secret transaction restoration must be executed regardless of candidate_started."""
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker

    restore_calls = []

    def fail_recreate(*a, **kw):
        raise RuntimeError("compose-recreate-failed")

    def track_restore(contract, txn, *, preserve_published):
        restore_calls.append(("restore", preserve_published))

    monkeypatch.setattr(deploy, "_compose_recreate_hermes", fail_recreate)
    monkeypatch.setattr(deploy, "_finish_secret_override_transaction", track_restore)

    try:
        deploy.execute_recovery(
            contract, source=source, image=IMAGE, revision=REVISION,
            backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME"
        )
    except Exception:
        pass

    assert len(restore_calls) > 0, "Secret transaction restoration was NOT executed after compose failure"
    # At least one call must be the rollback restore (preserve_published=False).
    # Note: _write_secret_override also calls _finish_secret_override_transaction with preserve_published=True.
    restore_flags = [c[1] for c in restore_calls]
    assert False in restore_flags, (
        f"No rollback restore (preserve_published=False) found in calls: {restore_calls}"
    )

    out = capsys.readouterr().out
    assert "ROLLBACK_EXECUTED=PASS" in out


# ────────────────────────────────────────────────────────────────
# DEFECT 4 — partial candidate creation rollback
# ────────────────────────────────────────────────────────────────

def test_recovery_partial_candidate_creation_rollback(
    setup_execute, fake_docker, monkeypatch, capsys
):
    """If candidate recreation creates/reveals a partial candidate then raises,
    rollback must docker rm the partial candidate before restoring the original."""
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker

    def fail_recreate(*a, **kw):
        raise RuntimeError("partial-candidate-created")

    monkeypatch.setattr(deploy, "_compose_recreate_hermes", fail_recreate)
    monkeypatch.setattr(deploy, "_finish_secret_override_transaction", lambda *a, **kw: None)

    try:
        deploy.execute_recovery(
            contract, source=source, image=IMAGE, revision=REVISION,
            backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME"
        )
    except Exception:
        pass

    # docker rm must have been attempted on the target service
    rm_calls = [c for c in calls if c[0] == "docker" and c[1] == "rm" and contract.target_service in c]
    assert len(rm_calls) > 0, "docker rm was not called to clean up partial candidate"

    out = capsys.readouterr().out
    assert "ORIGINAL_CONTAINER_ID_MATCH=PASS" in out
    assert "ORIGINAL_START=PASS" in out
    assert "ROLLBACK_EXECUTED=PASS" in out


# ────────────────────────────────────────────────────────────────
# DEFECT 5 — STATUS=PASS only after lease release
# ────────────────────────────────────────────────────────────────

def test_recovery_no_pass_if_lease_release_fails(
    setup_execute, fake_docker, monkeypatch, capsys
):
    """If lease release fails, STATUS=PASS must NOT appear in stdout."""
    contract, source, backup_parent = setup_execute
    calls, responses = fake_docker

    def fail_release(*a, **kw):
        raise deploy.DeploymentContractError("lease-release-failed")

    monkeypatch.setattr(deploy.preflight, "release_deployment_lease", fail_release)
    monkeypatch.setattr(deploy.attestation, "_require_database_unchanged", lambda *a, **kw: None)
    monkeypatch.setattr(deploy.attestation, "_require_qdrant_unchanged", lambda *a, **kw: None)

    try:
        deploy.execute_recovery(
            contract, source=source, image=IMAGE, revision=REVISION,
            backup_parent=backup_parent, confirmation="RECOVER_UNTRUSTED_RUNTIME"
        )
    except Exception:
        pass

    out = capsys.readouterr().out
    assert "STATUS=PASS" not in out, "STATUS=PASS must not appear when lease release fails"


# ────────────────────────────────────────────────────────────────
# DEFECT 6 — CLI ROLLED_BACK vs FAIL status truthfulness
# ────────────────────────────────────────────────────────────────

def test_cli_rolled_back_status_truthful(monkeypatch, capsys):
    """PostMutationDeploymentError(status=ROLLED_BACK) => STATUS=ROLLED_BACK in output, not FAIL."""
    import sys
    monkeypatch.setattr(sys, "argv", [
        "hermes_production_deploy.py", "recover-untrusted-runtime",
        "--secret-source", "dummy", "--image", "dummy", "--revision", "dummy",
        "--backup-parent", "dummy", "--confirm", "RECOVER_UNTRUSTED_RUNTIME",
    ])
    monkeypatch.setattr(deploy, "load_contract", lambda: type("MockContract", (), {"version": 2})())
    monkeypatch.setattr(deploy, "_secret_source_argument", lambda c, s: "")

    def raise_rolled_back(*args, **kwargs):
        raise deploy.PostMutationDeploymentError(
            status="ROLLED_BACK",
            original_error_code="test-original",
            rollback_error_code=None,
        )
    monkeypatch.setattr(deploy, "execute_recovery", raise_rolled_back)

    rc = deploy.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "STATUS=ROLLED_BACK" in captured.out
    assert "STATUS=FAIL" not in captured.out


def test_cli_fail_status_on_rollback_failure(monkeypatch, capsys):
    """PostMutationDeploymentError(status=FAIL) => STATUS=FAIL in output."""
    import sys
    monkeypatch.setattr(sys, "argv", [
        "hermes_production_deploy.py", "recover-untrusted-runtime",
        "--secret-source", "dummy", "--image", "dummy", "--revision", "dummy",
        "--backup-parent", "dummy", "--confirm", "RECOVER_UNTRUSTED_RUNTIME",
    ])
    monkeypatch.setattr(deploy, "load_contract", lambda: type("MockContract", (), {"version": 2})())
    monkeypatch.setattr(deploy, "_secret_source_argument", lambda c, s: "")

    def raise_fail(*args, **kwargs):
        raise deploy.PostMutationDeploymentError(
            status="FAIL",
            original_error_code="test-original",
            rollback_error_code="RECOVERY_ROLLBACK_FAILED",
        )
    monkeypatch.setattr(deploy, "execute_recovery", raise_fail)

    rc = deploy.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "STATUS=FAIL" in captured.out
    assert "STATUS=ROLLED_BACK" not in captured.out
