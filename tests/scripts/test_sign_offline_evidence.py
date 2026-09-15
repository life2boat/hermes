import json
import os
import subprocess
import tempfile
import pytest
from datetime import datetime, timedelta
import copy
from scripts.hermes_release_qualification import get_all_gates, Status
from scripts.compute_bundle_digest import compute_canonical_digest_from_dict

SIGNER_SCRIPT = 'scripts/sign_offline_evidence.py'

def get_utc_offset(seconds: int) -> str:
    return (datetime.utcnow() + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

@pytest.fixture
def base_evidence():
    return {
        'schema_version': 1,
        'evidence_type': 'production_db_path',
        'target_sha': '8b44bb146b31902dc99c53d976e7b20964eb4caa',
        'status': 'PASS',
        'validator_id': '_validate_database_source_path',
        'validator_version': '1',
        'path_classification': 'authoritative-production-path',
        'collected_at_utc': get_utc_offset(0),
        'execution_provenance': {
            'runtime_identity': 'test-runtime',
            'isolation_level': 'docker'
        }
    }

def run_signer(input_data, key='TEST_KEY_123'):
    env = os.environ.copy()
    if key is not None:
        env['HERMES_PROVENANCE_KEY'] = key
    elif 'HERMES_PROVENANCE_KEY' in env:
        del env['HERMES_PROVENANCE_KEY']
        
    with tempfile.NamedTemporaryFile('w', delete=False) as f_in, tempfile.NamedTemporaryFile('w', delete=False) as f_out:
        json.dump(input_data, f_in)
        f_in.close()
        f_out.close()
        
        proc = subprocess.run(
            ['python3', SIGNER_SCRIPT, f_in.name, f_out.name],
            env=env,
            capture_output=True,
            text=True
        )
        
        if proc.returncode == 0:
            with open(f_out.name) as f:
                output_data = json.load(f)
        else:
            output_data = None
            
        os.unlink(f_in.name)
        os.unlink(f_out.name)
        
        return proc.returncode, proc.stdout, proc.stderr, output_data

def evaluate_gate(ev, key='TEST_KEY_123'):
    bundle = {"db_evidence": ev}
    env = os.environ.copy()
    if key is not None:
        os.environ['HERMES_PROVENANCE_KEY'] = key
    elif 'HERMES_PROVENANCE_KEY' in os.environ:
        del os.environ['HERMES_PROVENANCE_KEY']
        
    gates, *_ = get_all_gates(ev.get("target_sha", "8b44bb146b31902dc99c53d976e7b20964eb4caa"), bundle)
    
    # Restore env
    os.environ.clear()
    os.environ.update(env)
    
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    return gate

def test_1_valid_untouched_evidence(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    assert rc == 0
    gate = evaluate_gate(out)
    assert gate.status == Status.PASS

def test_2_missing_key_in_signer(base_evidence):
    rc, stdout, _, _ = run_signer(base_evidence, key=None)
    assert rc != 0
    assert 'HERMES_PROVENANCE_KEY environment variable is required' in stdout

def test_3_missing_key_in_qualifier(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    assert rc == 0
    gate = evaluate_gate(out, key=None)
    assert gate.status == Status.BLOCKED
    assert "No approved trust anchor exists" in gate.message

def test_4_wrong_key_in_qualifier(base_evidence):
    rc, _, _, out = run_signer(base_evidence, key="KEY_A")
    assert rc == 0
    gate = evaluate_gate(out, key="KEY_B")
    assert gate.status == Status.FAIL
    assert "Cryptographic provenance verification failed" in gate.message

def test_5_tamper_target_sha(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    out["target_sha"] = "0000000000000000000000000000000000000000"
    out["evidence_digest"] = compute_canonical_digest_from_dict({k: v for k, v in out.items() if k != "evidence_digest"})
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "target_sha mismatch" in gate.message or "Cryptographic provenance verification failed" in gate.message

def test_6_tamper_evidence_type(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    out["evidence_type"] = "fake_type"
    out["evidence_digest"] = compute_canonical_digest_from_dict({k: v for k, v in out.items() if k != "evidence_digest"})
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "Invalid evidence_type" in gate.message or "Cryptographic provenance verification failed" in gate.message

def test_7_tamper_path_classification(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    out["path_classification"] = "fake-path"
    out["evidence_digest"] = compute_canonical_digest_from_dict({k: v for k, v in out.items() if k != "evidence_digest"})
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "Cryptographic provenance verification failed" in gate.message

def test_8_missing_required_field(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    del out["validator_id"]
    out["evidence_digest"] = compute_canonical_digest_from_dict({k: v for k, v in out.items() if k != "evidence_digest"})
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "Missing fields" in gate.message or "Cryptographic provenance verification failed" in gate.message

def test_9_extra_unknown_field(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    out["hacker_field"] = "malicious"
    out["evidence_digest"] = compute_canonical_digest_from_dict({k: v for k, v in out.items() if k != "evidence_digest"})
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "Unknown fields" in gate.message or "Cryptographic provenance verification failed" in gate.message

def test_10_invalid_timestamp_regex(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    out["collected_at_utc"] = "2026/09/15 00:00:00"
    out["evidence_digest"] = compute_canonical_digest_from_dict({k: v for k, v in out.items() if k != "evidence_digest"})
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "Invalid collected_at_utc timestamp" in gate.message or "Cryptographic provenance verification failed" in gate.message

def test_11_timestamp_future_clock_skew(base_evidence):
    base_evidence["collected_at_utc"] = get_utc_offset(600)  # +10 minutes
    rc, _, _, out = run_signer(base_evidence)
    assert rc == 0
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "collected_at_utc is too far in the future" in gate.message

def test_12_timestamp_stale_replay(base_evidence):
    base_evidence["collected_at_utc"] = get_utc_offset(-8000)  # -133 minutes
    rc, _, _, out = run_signer(base_evidence)
    assert rc == 0
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "collected_at_utc is stale" in gate.message

def test_13_missing_signature_structure(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    del out["execution_provenance"]["signature"]
    out["evidence_digest"] = compute_canonical_digest_from_dict({k: v for k, v in out.items() if k != "evidence_digest"})
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "Missing or invalid signature" in gate.message

def test_14_tamper_evidence_digest(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    out["evidence_digest"] = "bad_digest"
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "evidence_digest mismatch" in gate.message

def test_15_status_tampered(base_evidence):
    rc, _, _, out = run_signer(base_evidence)
    out["status"] = "FAIL"
    out["evidence_digest"] = compute_canonical_digest_from_dict({k: v for k, v in out.items() if k != "evidence_digest"})
    gate = evaluate_gate(out)
    assert gate.status == Status.FAIL
    assert "status not PASS" in gate.reason or "Cryptographic provenance verification failed" in gate.reason or "DB evidence explicitly failed" in gate.reason
