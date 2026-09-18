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

from scripts.hermes_canonical_evidence import (
    SECRET_EVIDENCE_TYPE, SECRET_EVIDENCE_FIELDS,
    DB_EVIDENCE_TYPE, DB_EVIDENCE_FIELDS,
    SCHEMA_EVIDENCE_TYPE, SCHEMA_EVIDENCE_FIELDS,
    ROLLBACK_EVIDENCE_TYPE, ROLLBACK_EVIDENCE_FIELDS,
    CREDENTIAL_RISK_EVIDENCE_TYPE, CREDENTIAL_RISK_EVIDENCE_FIELDS
)

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
    bundle['secret_evidence'] = {k: '' for k in SECRET_EVIDENCE_FIELDS if k not in ('evidence_digest', 'execution_provenance')}
    bundle['secret_evidence'].update({
        'schema_version': 1, 'evidence_type': SECRET_EVIDENCE_TYPE, 'target_sha': VALID_TARGET_SHA,
        'status': 'PASS', 'source_class': 'explicit-protected-dotenv', 'collected_at_utc': now_str,
        'required_secrets': ['foo'], 'execution_provenance': prov
    })

    bundle['db_evidence'] = {k: '' for k in DB_EVIDENCE_FIELDS if k not in ('evidence_digest', 'execution_provenance')}
    bundle['db_evidence'].update({
        'schema_version': 1, 'evidence_type': DB_EVIDENCE_TYPE, 'target_sha': VALID_TARGET_SHA,
        'status': 'PASS', 'validator_id': 'validate_database_source_path', 'validator_version': 1,
        'path_classification': 'authoritative-production-path', 'collected_at_utc': now_str, 'execution_provenance': prov
    })

    bundle['schema_evidence'] = {k: '' for k in SCHEMA_EVIDENCE_FIELDS if k not in ('evidence_digest', 'execution_provenance')}
    bundle['schema_evidence'].update({
        'schema_version': 1, 'evidence_type': SCHEMA_EVIDENCE_TYPE, 'target_sha': VALID_TARGET_SHA,
        'status': 'PASS', 'observed_schema': 'CREATE TABLE', 'digest': '123', 'user_version': 1,
        'actual_user_version': 1, 'expected_user_version': 1, 'actual_schema_digest': '123',
        'expected_schema_digest': '123', 'schema_delta': 'none', 'migration_required': False,
        'integrity_status': 'ok', 'foreign_key_violation_count': 0, 'collected_at_utc': now_str,
        'execution_provenance': prov
    })

    bundle['rollback_evidence'] = {k: '' for k in ROLLBACK_EVIDENCE_FIELDS if k not in ('evidence_digest', 'execution_provenance')}
    bundle['rollback_evidence'].update({
        'schema_version': 1, 'evidence_type': ROLLBACK_EVIDENCE_TYPE, 'target_sha': VALID_TARGET_SHA,
        'status': 'PASS', 'current_production_image_digest': 'sha256:123',
        'current_production_oci_revision': 'rev', 'rollback_image_digest': 'sha256:123',
        'rollback_image_resolvable': True, 'rollback_revision': 'rev',
        'rollback_mechanism_id': 'docker-compose-revert', 'same_compose_chain': True,
        'database_restore_required': False, 'schema_downgrade_required': False,
        'rollback_health_required': True, 'rollback_attempt_count_max': 1,
        'rollback_procedure_proven': True, 'canonical_rehearsal_evidence': 'artifact:rollback-rehearsal:docker-compose-revert:pass',
        'collected_at_utc': now_str, 'execution_provenance': prov
    })

    bundle['credential_risk_evidence'] = {k: '' for k in CREDENTIAL_RISK_EVIDENCE_FIELDS if k not in ('evidence_digest', 'execution_provenance')}
    bundle['credential_risk_evidence'].update({
        'schema_version': 1, 'evidence_type': CREDENTIAL_RISK_EVIDENCE_TYPE, 'target_sha': VALID_TARGET_SHA,
        'status': 'PASS', 'credential_risk_status': 'PROVEN_CLEAR', 'collected_at_utc': now_str,
        'execution_provenance': prov
    })
    
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
    err = _verify_signed_provenance(out['secret_evidence'], VALID_TARGET_SHA, SECRET_EVIDENCE_TYPE, SECRET_EVIDENCE_FIELDS, DUMMY_KEY)
    assert err is not None

def test_evidence_mutation_after_signing_fails_verifier():
    proc = run_signer(get_base_unsigned_bundle())
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    
    out['db_evidence']['status'] = 'FAIL'
    
    from scripts.hermes_release_qualification import _verify_signed_provenance
    err = _verify_signed_provenance(out['db_evidence'], VALID_TARGET_SHA, DB_EVIDENCE_TYPE, DB_EVIDENCE_FIELDS, DUMMY_KEY)
    assert err is not None

def test_unsigned_evidence_sent_directly_fails():
    bundle = get_base_unsigned_bundle()
    from scripts.hermes_release_qualification import _verify_signed_provenance
    err = _verify_signed_provenance(bundle['db_evidence'], VALID_TARGET_SHA, DB_EVIDENCE_TYPE, DB_EVIDENCE_FIELDS, DUMMY_KEY)
    assert err is not None


def test_producer_to_signer_compatibility(monkeypatch, tmp_path):
    import sys
    import json
    
    manifest = {
        'secrets': {
            'source_type': 'explicit-protected-dotenv',
            'required': ['TEST_SECRET'],
            'approved_source_path': '/fake/.env'
        },
        'database_mount': {
            'source': '/fake/path/db.sqlite3'
        },
        'rollback': {},
        'attestation': {}
    }
    
    deploy_dir = tmp_path / 'deploy'
    deploy_dir.mkdir()
    manifest_path = deploy_dir / 'hermes-production.json'
    manifest_path.write_text(json.dumps(manifest))
    
    import scripts.generate_offline_evidence as gen
    from datetime import datetime, timezone
    
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    prov = {'isolation_level': 'docker', 'runtime_identity': 'healbite-production'}
    
    def mock_secret(*args, **kwargs):
        return "PASS", ["TEST_SECRET"], "PROVEN_CLEAR"
        
    def mock_db(*args, **kwargs):
        return "PASS", "authoritative-production-path", 1
        
    def mock_schema(*args, **kwargs):
        return {
            "status": "PASS",
            "observed_schema": "CREATE TABLE mock(id INTEGER);",
            "digest": "123",
            "user_version": 1,
            "actual_user_version": 1,
            "expected_user_version": 1,
            "actual_schema_digest": "123",
            "expected_schema_digest": "123",
            "schema_delta": "none",
            "migration_required": False,
            "integrity_status": "ok",
            "foreign_key_violation_count": 0,
        }
        
    def mock_rollback(target_sha, now, manifest=None):
        return {
            "schema_version": 1,
            "evidence_type": "rollback_ready",
            "target_sha": target_sha,
            "status": "PASS",
            "collected_at_utc": now,
            "execution_provenance": prov,
            "current_production_image_digest": "sha256:123",
            "current_production_oci_revision": "rev",
            "rollback_image_digest": "sha256:123",
            "rollback_image_resolvable": True,
            "rollback_revision": "rev",
            "rollback_mechanism_id": "docker-compose-revert",
            "same_compose_chain": True,
            "database_restore_required": False,
            "schema_downgrade_required": False,
            "rollback_health_required": True,
            "rollback_attempt_count_max": 1,
            "rollback_procedure_proven": True,
            "canonical_rehearsal_evidence": "artifact:rollback-rehearsal:docker-compose-revert:pass"
        }
    
    monkeypatch.setattr(gen, "check_secret_presence", mock_secret)
    monkeypatch.setattr(gen, "check_db_path", mock_db)
    monkeypatch.setattr(gen, "check_schema_compatibility", mock_schema)
    monkeypatch.setattr(gen, "get_real_rollback_evidence", mock_rollback)
    
    monkeypatch.chdir(tmp_path)
    # Important: add the root dir to path so it can import canonical schema contract
    sys.path.insert(0, os.path.abspath(os.path.join(str(tmp_path), '..', '..')))
    
    try:
        gen.generate(VALID_TARGET_SHA, output_dir=str(tmp_path))
    finally:
        sys.path.pop(0)
    
    expected_files = {
        'secret_evidence': 'secret_evidence_unsigned.json',
        'db_evidence': 'db_evidence_unsigned.json',
        'schema_evidence': 'schema_evidence_unsigned.json',
        'rollback_evidence': 'rollback_evidence_unsigned.json',
        'credential_risk_evidence': 'credential_risk_evidence_unsigned.json'
    }
    
    bundle = {
        'collector_provenance': {
            'COLLECTOR_REPOSITORY': 'life2boat/hermes',
            'COLLECTOR_HEAD_SHA': VALID_TARGET_SHA,
            'COLLECTOR_TREE_SHA': 'b' * 40,
            'COLLECTOR_WORKTREE_CLEAN': True
        }
    }
    
    for k, filename in expected_files.items():
        with open(tmp_path / filename, 'r') as f:
            ev = json.load(f)
            # NO MUTATION OF BLOCKED TO PASS HERE
            bundle[k] = ev
            
    monkeypatch.undo()
    proc = run_signer(bundle)
    assert proc.returncode == 0, f"Signer failed: {proc.stderr} {proc.stdout}"
    
    signed_bundle = json.loads(proc.stdout)
    from scripts.hermes_release_qualification import _verify_signed_provenance
    
    for k, expected_type, expected_fields in [
        ('secret_evidence', SECRET_EVIDENCE_TYPE, SECRET_EVIDENCE_FIELDS),
        ('db_evidence', DB_EVIDENCE_TYPE, DB_EVIDENCE_FIELDS),
        ('schema_evidence', SCHEMA_EVIDENCE_TYPE, SCHEMA_EVIDENCE_FIELDS),
        ('rollback_evidence', ROLLBACK_EVIDENCE_TYPE, ROLLBACK_EVIDENCE_FIELDS),
        ('credential_risk_evidence', CREDENTIAL_RISK_EVIDENCE_TYPE, CREDENTIAL_RISK_EVIDENCE_FIELDS),
    ]:
        err = _verify_signed_provenance(signed_bundle[k], VALID_TARGET_SHA, expected_type, expected_fields, DUMMY_KEY)
        assert err is None, f"Verifier rejected {k}: {err}"
