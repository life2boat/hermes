import sys
import json
import os
import hashlib
import hmac
import tempfile
import shutil
import time
from datetime import datetime, timezone
import subprocess

def compute_canonical_digest_from_dict(d: dict) -> str:
    s = json.dumps(d, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def sign_evidence(ev: dict, key: str) -> dict:
    payload = {k: v for k, v in ev.items() if k not in ('evidence_digest', 'execution_provenance')}
    prov = ev.get('execution_provenance', {})
    prov_copy = {k: v for k, v in prov.items() if k != 'signature'}
    payload['execution_provenance'] = prov_copy
    
    payload_str = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    payload_digest = hashlib.sha256(payload_str.encode('utf-8')).hexdigest()
    
    sig = hmac.new(
        key.encode('utf-8'),
        payload_digest.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    
    final_evidence = dict(ev)
    prov_out = dict(prov_copy)
    prov_out['signature'] = sig
    final_evidence['execution_provenance'] = prov_out
    
    if 'evidence_digest' in final_evidence:
        del final_evidence['evidence_digest']
        
    final_evidence['evidence_digest'] = compute_canonical_digest_from_dict(final_evidence)
    return final_evidence

def verify_evidence(ev: dict, expected_sha: str, key: str) -> bool:
    if ev.get('target_sha') != expected_sha: return False
    if ev.get('status') != 'PASS': return False
    ts_str = ev.get('collected_at_utc', '')
    try:
        dt = datetime.strptime(ts_str, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except:
        return False
    now = datetime.now(timezone.utc)
    age = (now - dt).total_seconds()
    if age > 7200 or age < -300:
        return False
        
    d_copy = dict(ev)
    supplied_digest = d_copy.pop('evidence_digest', '')
    if compute_canonical_digest_from_dict(d_copy) != supplied_digest:
        return False
        
    prov = d_copy.get('execution_provenance', {})
    supplied_sig = prov.get('signature', '')
    
    payload = {k: v for k, v in d_copy.items() if k != 'execution_provenance'}
    prov_copy = {k: v for k, v in prov.items() if k != 'signature'}
    payload['execution_provenance'] = prov_copy
    payload_str = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    payload_digest = hashlib.sha256(payload_str.encode('utf-8')).hexdigest()
    
    expected_sig = hmac.new(
        key.encode('utf-8'),
        payload_digest.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    
    if expected_sig != supplied_sig:
        return False
        
    return True

def main():
    target_sha = ''
    if len(sys.argv) > 1:
        target_sha = sys.argv[1]
    
    ssh_cmd = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    if ssh_cmd and not target_sha:
        target_sha = ssh_cmd.strip()
        
    if not target_sha or not all(c in '0123456789abcdef' for c in target_sha) or len(target_sha) != 40:
        print(json.dumps({'error': 'Invalid or missing target SHA'}))
        sys.exit(1)
        
    key = os.environ.get('HERMES_PROVENANCE_KEY', '')
    if not key:
        key = sys.stdin.read().strip()
        
    if not key:
        print(json.dumps({'error': 'Missing provenance key'}))
        sys.exit(1)
        
    tmpdir = tempfile.mkdtemp(prefix='hermes-ev-')
    os.chmod(tmpdir, 0o700)
    
    bundle = {}
    try:
        from scripts.generate_offline_evidence import generate
        import builtins
        original_open = builtins.open
        def patched_open(file, *args, **kwargs):
            if isinstance(file, str) and file.endswith('_unsigned.json') and not file.startswith('/'):
                new_path = os.path.join(tmpdir, file)
                return original_open(new_path, *args, **kwargs)
            return original_open(file, *args, **kwargs)
            
        builtins.open = patched_open
        
        # Swallow prints from generate
        import io
        sys.stdout = io.StringIO()
        generate(target_sha)
        sys.stdout = sys.__stdout__
        
        builtins.open = original_open
        
        expected_files = {
            'secret_evidence': 'secret_evidence_unsigned.json',
            'db_evidence': 'db_evidence_unsigned.json',
            'schema_evidence': 'schema_evidence_unsigned.json',
            'rollback_evidence': 'rollback_evidence_unsigned.json',
            'credential_risk_evidence': 'credential_risk_evidence_unsigned.json'
        }
        
        for k, filename in expected_files.items():
            full_path = os.path.join(tmpdir, filename)
            if not os.path.exists(full_path):
                raise Exception(f'Missing {filename}')
                
            os.chmod(full_path, 0o600)
            with open(full_path, 'r', encoding='utf-8') as f:
                ev = json.load(f)
                
            signed_ev = sign_evidence(ev, key)
            if not verify_evidence(signed_ev, target_sha, key):
                raise Exception(f'Verification failed for {k}')
                
            bundle[k] = signed_ev
            
    except Exception as e:
        sys.stdout = sys.__stdout__
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        
    print(json.dumps(bundle, separators=(',', ':')))

if __name__ == '__main__':
    main()
