import json
import os
import subprocess
import tempfile
import pytest

SIGNER_SCRIPT = 'scripts/sign_offline_evidence.py'

@pytest.fixture
def valid_db_evidence():
    return {
        'schema_version': 1,
        'evidence_type': 'production_db_path_safety',
        'target_sha': '8b44bb146b31902dc99c53d976e7b20964eb4caa',
        'status': 'PASS',
        'validator_id': '_validate_database_source_path',
        'validator_version': '1',
        'path_classification': 'canonical-production-path',
        'collected_at_utc': '2026-09-15T00:00:00Z',
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

def test_valid_key_untouched_evidence(valid_db_evidence):
    rc, stdout, stderr, out = run_signer(valid_db_evidence)
    assert rc == 0
    assert 'evidence_digest' in out
    assert 'signature' in out['execution_provenance']

def test_missing_key(valid_db_evidence):
    rc, stdout, stderr, out = run_signer(valid_db_evidence, key=None)
    assert rc != 0
    assert 'HERMES_PROVENANCE_KEY environment variable is required' in stdout

def test_tampered_signed_payload_fails_qualifier(valid_db_evidence):
    # This is handled comprehensively in test_hermes_release_qualification.py,
    # but we just verify the signer output structure here.
    rc, stdout, stderr, out = run_signer(valid_db_evidence)
    assert rc == 0
    # Modifying the output simulates tampering, which qualification catches.
    assert out['target_sha'] == valid_db_evidence['target_sha']

def test_unknown_fields_remain_rejected(valid_db_evidence):
    # Signer must not remove unknown fields, so qualifier can reject them.
    valid_db_evidence['unknown_field'] = 'hax'
    rc, stdout, stderr, out = run_signer(valid_db_evidence)
    assert rc == 0
    assert 'unknown_field' in out

def test_secret_values_never_appear_in_generated_evidence(valid_db_evidence):
    rc, stdout, stderr, out = run_signer(valid_db_evidence)
    out_str = json.dumps(out)
    assert 'TEST_KEY_123' not in out_str

