import os
import json
import subprocess
import pytest
from datetime import datetime, timedelta, timezone

from scripts.hermes_release_qualification import get_all_gates, Status
from scripts.compute_bundle_digest import compute_canonical_digest_from_dict
from tests.scripts.test_hermes_release_qualification import create_bundle

import tempfile
def run_signer(evidence_dict):
    env = os.environ.copy()
    env["HERMES_PROVENANCE_KEY"] = "TEST_KEY_123"
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        json.dump(evidence_dict, f)
        temp_in = f.name
    temp_out = temp_in + ".out"
    
    proc = subprocess.run(
        ["python3", "scripts/sign_offline_evidence.py", temp_in, temp_out],
        capture_output=True, text=True, env=env
    )
    
    output_data = None
    if proc.returncode == 0:
        try:
            with open(temp_out, "r") as out_f:
                output_data = json.load(out_f)
        except:
            pass
            
    try:
        os.remove(temp_in)
        os.remove(temp_out)
    except:
        pass
        
    return proc.returncode, proc.stdout, proc.stderr, output_data

def evaluate_gate(ev, gate_name, key='TEST_KEY_123', bundle_kwargs=None):
    if bundle_kwargs is None:
        bundle_kwargs = {}
    
    if gate_name == "DB_PATH_SAFETY":
        bundle_kwargs["db_evidence"] = ev
    elif gate_name == "SECRET_CONTRACT":
        bundle_kwargs["secret_evidence"] = ev
    elif gate_name == "SCHEMA_COMPATIBILITY":
        bundle_kwargs["schema_evidence"] = ev
    elif gate_name == "ROLLBACK_QUALIFIED":
        bundle_kwargs["rollback_evidence"] = ev

    bundle = create_bundle(bundle_kwargs)
    
    env = os.environ.copy()
    if key is not None:
        os.environ['HERMES_PROVENANCE_KEY'] = key
    elif 'HERMES_PROVENANCE_KEY' in os.environ:
        del os.environ['HERMES_PROVENANCE_KEY']

    target_sha = ev.get("target_sha", "8b44bb146b31902dc99c53d976e7b20964eb4caa") if isinstance(ev, dict) else "8b44bb146b31902dc99c53d976e7b20964eb4caa"
    gates, *_ = get_all_gates(target_sha, bundle)

    os.environ.clear()
    os.environ.update(env)

    return next(g for g in gates if g.gate_name == gate_name)

def get_utc_offset(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

@pytest.fixture
def base_db_evidence():
    return {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "8b44bb146b31902dc99c53d976e7b20964eb4caa",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": 1,
        "path_classification": "authoritative-production-path",
        "collected_at_utc": get_utc_offset(0),
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "test-runtime"
        }
    }

@pytest.fixture
def base_secret_evidence():
    return {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "8b44bb146b31902dc99c53d976e7b20964eb4caa",
        "status": "PASS",
        "source_class": "docker-secret",
        "required_secrets": [
            {"name": "TELEGRAM_BOT_TOKEN", "required": True, "present": True, "source_class": "env"},
            
        ],
        "collected_at_utc": get_utc_offset(0),
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "test-runtime"
        }
    }

@pytest.fixture
def base_schema_evidence():
    return {
        "schema_version": 1,
        "evidence_type": "production_schema_compatibility",
        "target_sha": "8b44bb146b31902dc99c53d976e7b20964eb4caa",
        "status": "PASS",
        "observed_schema": "v1.2.3",
        "digest": "abcdef123456",
        "collected_at_utc": get_utc_offset(0),
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "test-runtime"
        }
    }

@pytest.fixture
def base_rollback_evidence():
    return {
        "schema_version": 1,
        "evidence_type": "rollback_ready",
        "target_sha": "8b44bb146b31902dc99c53d976e7b20964eb4caa",
        "status": "PASS",
        "collected_at_utc": get_utc_offset(0),
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "test-runtime"
        }
    }

# ================= CRYPTO =================

def test_VALID_SIGNATURE_ACCEPTED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    assert rc == 0
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.PASS

def test_WRONG_KEY_REJECTED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", key="WRONG_KEY")
    assert gate.status == Status.FAIL
    assert "Cryptographic provenance verification failed" in gate.reason

def test_TAMPERED_PAYLOAD_REJECTED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    out["status"] = "FAIL"
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL

def test_TAMPERED_SIGNATURE_REJECTED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    out["execution_provenance"]["signature"] = "tampered"
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL

def test_TAMPERED_TIMESTAMP_REJECTED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    out["collected_at_utc"] = get_utc_offset(100)
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL

def test_STALE_EVIDENCE_REJECTED(base_db_evidence):
    base_db_evidence["collected_at_utc"] = get_utc_offset(-8000)
    rc, _, _, out = run_signer(base_db_evidence)
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL
    assert "stale" in gate.reason

def test_FUTURE_EVIDENCE_REJECTED(base_db_evidence):
    base_db_evidence["collected_at_utc"] = get_utc_offset(600)
    rc, _, _, out = run_signer(base_db_evidence)
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL
    assert "future" in gate.reason

def test_WRONG_TARGET_SHA_REJECTED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    out["target_sha"] = "wrong_sha"
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL

def test_DIGEST_MISMATCH_REJECTED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    out["evidence_digest"] = "bad"
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL

def test_UNKNOWN_OUTER_FIELD_REJECTED(base_db_evidence):
    base_db_evidence["extra_field"] = "bad"
    rc, _, _, out = run_signer(base_db_evidence)
    gate = evaluate_gate(out, "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL

def test_MISSING_TRUST_ROOT_FAILS_CLOSED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", key="")
    assert gate.status == Status.BLOCKED

# ================= SECRET_CONTRACT =================

def test_VALID_OLD_CONTRACT_RECORD_ACCEPTED(base_secret_evidence):
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.PASS

def test_MISSING_REQUIRED_SECRET_REJECTED(base_secret_evidence):
    base_secret_evidence["required_secrets"].pop(0)
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.FAIL

def test_EXTRA_SECRET_REJECTED(base_secret_evidence):
    base_secret_evidence["required_secrets"].append({"name": "EXTRA", "required": True, "present": True, "source_class": "env"})
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.FAIL
    assert "Observed secrets do not match expected mandatory secrets exactly" in gate.reason

def test_REQUIRED_FALSE_TYPE_REJECTED(base_secret_evidence):
    base_secret_evidence["required_secrets"][0]["required"] = "yes"
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.FAIL

def test_PRESENT_FALSE_TYPE_REJECTED(base_secret_evidence):
    base_secret_evidence["required_secrets"][0]["present"] = "yes"
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.FAIL

def test_REQUIRED_BUT_NOT_PRESENT_REJECTED(base_secret_evidence):
    base_secret_evidence["required_secrets"][0]["present"] = False
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.FAIL
    assert "Required secret is not present" in gate.reason

def test_EMPTY_SOURCE_CLASS_REJECTED(base_secret_evidence):
    base_secret_evidence["required_secrets"][0]["source_class"] = ""
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.FAIL

def test_UNKNOWN_SECRET_FIELD_REJECTED(base_secret_evidence):
    base_secret_evidence["required_secrets"][0]["unknown"] = True
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.FAIL

def test_PR336_PRESENCE_CONFIRMED_SHAPE_REJECTED(base_secret_evidence):
    base_secret_evidence["required_secrets"][0] = {"name": "TELEGRAM_BOT_TOKEN", "presence": "CONFIRMED"}
    rc, _, _, out = run_signer(base_secret_evidence)
    gate = evaluate_gate(out, "SECRET_CONTRACT")
    assert gate.status == Status.FAIL
    assert "missing or unknown fields" in gate.reason

# ================= SCHEMA =================

def test_VALID_SIGNED_SCHEMA_EVIDENCE_ACCEPTED(base_schema_evidence):
    rc, _, _, out = run_signer(base_schema_evidence)
    gate = evaluate_gate(out, "SCHEMA_COMPATIBILITY", bundle_kwargs={"schema_evidence": out})
    assert gate.status == Status.PASS

def test_UNSIGNED_SCHEMA_EVIDENCE_REJECTED(base_schema_evidence):
    gate = evaluate_gate(base_schema_evidence, "SCHEMA_COMPATIBILITY", bundle_kwargs={"schema_evidence": base_schema_evidence})
    assert gate.status == Status.FAIL

def test_STALE_SCHEMA_EVIDENCE_REJECTED(base_schema_evidence):
    base_schema_evidence["collected_at_utc"] = get_utc_offset(-8000)
    rc, _, _, out = run_signer(base_schema_evidence)
    gate = evaluate_gate(out, "SCHEMA_COMPATIBILITY", bundle_kwargs={"schema_evidence": out})
    assert gate.status == Status.FAIL

def test_TAMPERED_SCHEMA_EVIDENCE_REJECTED(base_schema_evidence):
    rc, _, _, out = run_signer(base_schema_evidence)
    out["observed_schema"] = "tampered"
    gate = evaluate_gate(out, "SCHEMA_COMPATIBILITY", bundle_kwargs={"schema_evidence": out})
    assert gate.status == Status.FAIL

# ================= ROLLBACK =================

def test_VALID_SIGNED_ROLLBACK_EVIDENCE_ACCEPTED(base_rollback_evidence):
    rc, _, _, out = run_signer(base_rollback_evidence)
    gate = evaluate_gate(out, "ROLLBACK_QUALIFIED", bundle_kwargs={"rollback_evidence": out})
    assert gate.status == Status.PASS

def test_UNSIGNED_ROLLBACK_EVIDENCE_REJECTED(base_rollback_evidence):
    gate = evaluate_gate(base_rollback_evidence, "ROLLBACK_QUALIFIED", bundle_kwargs={"rollback_evidence": base_rollback_evidence})
    assert gate.status == Status.FAIL

def test_STALE_ROLLBACK_EVIDENCE_REJECTED(base_rollback_evidence):
    base_rollback_evidence["collected_at_utc"] = get_utc_offset(-8000)
    rc, _, _, out = run_signer(base_rollback_evidence)
    gate = evaluate_gate(out, "ROLLBACK_QUALIFIED", bundle_kwargs={"rollback_evidence": out})
    assert gate.status == Status.FAIL

def test_TAMPERED_ROLLBACK_EVIDENCE_REJECTED(base_rollback_evidence):
    rc, _, _, out = run_signer(base_rollback_evidence)
    out["status"] = "FAIL"
    gate = evaluate_gate(out, "ROLLBACK_QUALIFIED", bundle_kwargs={"rollback_evidence": out})
    assert gate.status == Status.FAIL

# --- DB PATH SAFETY EXACTNESS TESTS ---

import copy
import pytest

@pytest.fixture
def base_db_evidence():
    return {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "8b44bb146b31902dc99c53d976e7b20964eb4caa",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": 1,
        "path_classification": "authoritative-production-path",
        "collected_at_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "test-runtime"
        }
    }

def test_AUTHORITATIVE_PRODUCTION_PATH_ACCEPTED(base_db_evidence):
    rc, _, _, out = run_signer(base_db_evidence)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.PASS

def test_APPROVED_PRODUCTION_PATH_REJECTED(base_db_evidence):
    ev = copy.deepcopy(base_db_evidence)
    ev["path_classification"] = "approved-production-path"
    rc, _, _, out = run_signer(ev)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.FAIL

def test_CANONICAL_PRODUCTION_PATH_REJECTED(base_db_evidence):
    ev = copy.deepcopy(base_db_evidence)
    ev["path_classification"] = "canonical-production-path"
    rc, _, _, out = run_signer(ev)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.FAIL

def test_ARBITRARY_PATH_CLASSIFICATION_REJECTED(base_db_evidence):
    ev = copy.deepcopy(base_db_evidence)
    ev["path_classification"] = "arbitrary-path"
    rc, _, _, out = run_signer(ev)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.FAIL

def test_MISSING_PATH_CLASSIFICATION_REJECTED(base_db_evidence):
    ev = copy.deepcopy(base_db_evidence)
    del ev["path_classification"]
    rc, _, _, out = run_signer(ev)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.FAIL

def test_NULL_PATH_CLASSIFICATION_REJECTED(base_db_evidence):
    ev = copy.deepcopy(base_db_evidence)
    ev["path_classification"] = None
    rc, _, _, out = run_signer(ev)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.FAIL

def test_WRONG_VALIDATOR_ID_REJECTED(base_db_evidence):
    ev = copy.deepcopy(base_db_evidence)
    ev["validator_id"] = "some_other_validator"
    rc, _, _, out = run_signer(ev)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.FAIL

def test_WRONG_VALIDATOR_VERSION_REJECTED(base_db_evidence):
    ev = copy.deepcopy(base_db_evidence)
    ev["validator_version"] = 2
    rc, _, _, out = run_signer(ev)
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.FAIL

def test_VALID_SIGNATURE_BUT_NON_AUTHORITATIVE_PATH_REJECTED(base_db_evidence):
    # Already inherently tested by APPROVED/CANONICAL tests, 
    # but let's test a distinct non-authoritative known path.
    ev = copy.deepcopy(base_db_evidence)
    ev["path_classification"] = "approved-production-path"
    rc, _, _, out = run_signer(ev)
    
    gate = evaluate_gate(out, "DB_PATH_SAFETY", bundle_kwargs={"db_evidence": out})
    assert gate.status == Status.FAIL
    assert gate.reason == "path_classification is not authoritative"

