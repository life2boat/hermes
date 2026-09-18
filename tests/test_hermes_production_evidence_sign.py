import json
import os
import pytest
import subprocess
import copy
from datetime import datetime, timezone, timedelta
import tempfile
import shutil

VALID_TARGET_SHA = 'a' * 40
DUMMY_KEY = 'dummykey'

def get_base_unsigned_bundle():
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    prov = {'isolation_level': 'docker', 'runtime_identity': 'healbite-production'}
    bundle = {
        'collector_provenance': {
            'COLLECTOR_REPOSITORY': 'life2boat/hermes',
            'COLLECTOR_HEAD_SHA': VALID_TARGET_SHA,
            'COLLECTOR_TREE_SHA': 'b' * 40,
            'COLLECTOR_WORKTREE_CLEAN': True
        }
    }
    types = {
        'secret_evidence': 'secret_scan',
        'db_evidence': 'db_ready',
        'schema_evidence': 'schema_ready',
        'rollback_evidence': 'rollback_ready'
    }
    for k, v in types.items():
        bundle[k] = {
            'schema_version': 1,
            'evidence_type': v,
            'target_sha': VALID_TARGET_SHA,
            'status': 'PASS',
            'collected_at_utc': now_str,
            'execution_provenance': prov
        }
    bundle['credential_risk_evidence'] = {
        'schema_version': 1,
        'evidence_type': 'credential_risk',
        'target_sha': VALID_TARGET_SHA,
        'status': 'PASS',
        'credential_risk_status': 'PROVEN_CLEAR',
        'collected_at_utc': now_str,
        'execution_provenance': prov
    }
    return bundle

def run_signer(bundle, target_sha=VALID_TARGET_SHA, env_overrides=None):
    env = dict(os.environ)
    if env_overrides is not None:
        env.update(env_overrides)
    if 'HERMES_PROVENANCE_KEY' not in env and env_overrides is None:
        env['HERMES_PROVENANCE_KEY'] = DUMMY_KEY
    
    # We must explicitly ensure PYTHONPATH includes the root
    env['PYTHONPATH'] = os.path.abspath('.')
        
    proc = subprocess.run(
        ['python3', 'scripts/hermes_production_evidence_sign.py', target_sha],
        input=json.dumps(bundle) if bundle else '',
        text=True,
        capture_output=True,
        env=env
    )
    return proc

def test_missing_trust_anchor():
    proc = run_signer(get_base_unsigned_bundle(), env_overrides={'HERMES_PROVENANCE_KEY': ''})
    assert proc.returncode != 0
    assert 'Missing provenance key' in proc.stdout

def test_wrong_target_sha():
    proc = run_signer(get_base_unsigned_bundle(), target_sha='b' * 40)
    assert proc.returncode != 0
    assert 'mismatch' in proc.stdout

def test_missing_required_field():
    bundle = get_base_unsigned_bundle()
    del bundle['secret_evidence']['status']
    proc = run_signer(bundle)
    assert proc.returncode != 0
    assert 'Missing required field' in proc.stdout

def test_status_not_pass():
    bundle = get_base_unsigned_bundle()
    bundle['db_evidence']['status'] = 'FAIL'
    proc = run_signer(bundle)
    assert proc.returncode != 0
    assert 'Verification failed' in proc.stdout

def test_credential_risk_not_clear():
    bundle = get_base_unsigned_bundle()
    bundle['credential_risk_evidence']['credential_risk_status'] = 'PROVEN_RISK'
    proc = run_signer(bundle)
    assert proc.returncode != 0
    assert 'not PROVEN_CLEAR' in proc.stdout

def test_stale_evidence():
    bundle = get_base_unsigned_bundle()
    past = datetime.now(timezone.utc) - timedelta(seconds=8000)
    bundle['schema_evidence']['collected_at_utc'] = past.strftime('%Y-%m-%dT%H:%M:%SZ')
    proc = run_signer(bundle)
    assert proc.returncode != 0
    assert 'Verification failed' in proc.stdout
    assert 'stale' in proc.stdout.lower() or 'verification failed' in proc.stdout.lower()

def test_future_evidence():
    bundle = get_base_unsigned_bundle()
    future = datetime.now(timezone.utc) + timedelta(seconds=800)
    bundle['schema_evidence']['collected_at_utc'] = future.strftime('%Y-%m-%dT%H:%M:%SZ')
    proc = run_signer(bundle)
    assert proc.returncode != 0
    assert 'Verification failed' in proc.stdout

def test_wrong_evidence_type():
    bundle = get_base_unsigned_bundle()
    bundle['rollback_evidence']['evidence_type'] = 'wrong_type'
    proc = run_signer(bundle)
    assert proc.returncode != 0
    assert 'Verification failed' in proc.stdout

def test_signer_does_not_accept_stdin_key():
    # Attempt to pass key through stdin bundle payload (which should not be accepted as trust root)
    proc = subprocess.run(
        ['python3', 'scripts/hermes_production_evidence_sign.py', VALID_TARGET_SHA],
        input=DUMMY_KEY,
        text=True,
        capture_output=True,
        env={'PYTHONPATH': os.path.abspath('.')}
    )
    assert proc.returncode != 0
    assert 'Missing provenance key' in proc.stdout

def test_collector_target_sha_mismatch():
    bundle = get_base_unsigned_bundle()
    bundle['collector_provenance']['COLLECTOR_HEAD_SHA'] = 'c' * 40
    proc = run_signer(bundle)
    assert proc.returncode != 0
    assert 'mismatch' in proc.stdout

def test_unknown_field():
    bundle = get_base_unsigned_bundle()
    bundle['db_evidence']['unknown_field'] = 'test'
    proc = run_signer(bundle)
    assert proc.returncode != 0
    assert 'Verification failed' in proc.stdout

def test_signing_key_absent_from_stdout_stderr():
    bundle = get_base_unsigned_bundle()
    proc = run_signer(bundle)
    assert proc.returncode == 0
    assert DUMMY_KEY not in proc.stdout
    assert DUMMY_KEY not in proc.stderr

def test_valid_bundle_success():
    proc = run_signer(get_base_unsigned_bundle())
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert 'secret_evidence' in out
    assert 'signature' in out['secret_evidence']['execution_provenance']

# Verifier side checks (mocked by signing first then modifying)
def test_signature_mutation_fails_verifier():
    proc = run_signer(get_base_unsigned_bundle())
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    
    # Mutate signature
    out['secret_evidence']['execution_provenance']['signature'] = '0' * 64
    
    from scripts.hermes_release_qualification import _verify_signed_provenance
    fields = ['schema_version', 'evidence_type', 'target_sha', 'status', 'collected_at_utc', 'evidence_digest', 'execution_provenance']
    err = _verify_signed_provenance(out['secret_evidence'], VALID_TARGET_SHA, 'secret_scan', fields, DUMMY_KEY)
    assert err is not None

def test_evidence_mutation_after_signing_fails_verifier():
    proc = run_signer(get_base_unsigned_bundle())
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    
    out['db_evidence']['status'] = 'FAIL'
    
    from scripts.hermes_release_qualification import _verify_signed_provenance
    fields = ['schema_version', 'evidence_type', 'target_sha', 'status', 'collected_at_utc', 'evidence_digest', 'execution_provenance']
    err = _verify_signed_provenance(out['db_evidence'], VALID_TARGET_SHA, 'db_ready', fields, DUMMY_KEY)
    assert err is not None

def test_unsigned_evidence_sent_directly_fails():
    bundle = get_base_unsigned_bundle()
    from scripts.hermes_release_qualification import _verify_signed_provenance
    fields = ['schema_version', 'evidence_type', 'target_sha', 'status', 'collected_at_utc', 'evidence_digest', 'execution_provenance']
    err = _verify_signed_provenance(bundle['db_evidence'], VALID_TARGET_SHA, 'db_ready', fields, DUMMY_KEY)
    assert err is not None
