import sys
import json
import os
import hashlib
import hmac
from datetime import datetime, timezone

def _verify_signed_provenance(ev, expected_sha, evidence_type, expected_fields, key_str):
    # This checks all the exact constraints that hermes_release_qualification uses.
    from scripts.hermes_release_qualification import _verify_signed_provenance as verify_inner
    return verify_inner(ev, expected_sha, evidence_type, expected_fields, key_str)

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

def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Missing target SHA'}))
        sys.exit(1)
        
    target_sha = sys.argv[1]
    if not target_sha or not all(c in '0123456789abcdef' for c in target_sha) or len(target_sha) != 40:
        print(json.dumps({'error': 'Invalid target SHA'}))
        sys.exit(1)
    
    key = os.environ.get('HERMES_PROVENANCE_KEY', '')
    if not key:
        print(json.dumps({'error': 'Missing provenance key'}))
        sys.exit(1)
        
    try:
        input_data = sys.stdin.read().strip()
        if not input_data:
            print(json.dumps({'error': 'Missing unsigned evidence'}))
            sys.exit(1)
            
        unsigned_bundle = json.loads(input_data)
        if 'error' in unsigned_bundle:
            print(json.dumps({'error': unsigned_bundle['error']}))
            sys.exit(1)
            
        collector_provenance = unsigned_bundle.get('collector_provenance', {})
        if collector_provenance.get('COLLECTOR_HEAD_SHA') != target_sha:
            print(json.dumps({'error': 'Target SHA mismatch in collector provenance'}))
            sys.exit(1)
            
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
        
    signed_bundle = {}
    
    from scripts.hermes_canonical_evidence import TYPES_MAP, EXPECTED_FIELDS_MAP
    types_map = TYPES_MAP
    expected_fields_map = EXPECTED_FIELDS_MAP

    try:
        for key_name, ev_type in types_map.items():
            ev = unsigned_bundle.get(key_name)
            if not ev:
                raise Exception(f'Missing {key_name}')
                
            signed_ev = sign_evidence(ev, key)
            
            # Additional pre-sign validation: check required fields exist in unsigned evidence
            required_fields = expected_fields_map[key_name]
            for f in required_fields:
                if f != 'evidence_digest' and f != 'execution_provenance':
                    if f not in ev:
                        raise Exception(f'Missing required field {f} in {key_name}')
                        
            # Verify using canonical logic
            err = _verify_signed_provenance(signed_ev, target_sha, ev_type, required_fields, key)
            if err:
                raise Exception(f'Verification failed for {key_name}: {err}')
                
            if key_name == 'credential_risk_evidence':
                if signed_ev.get('credential_risk_status') != 'PROVEN_CLEAR':
                    raise Exception(f'Credential risk status is not PROVEN_CLEAR')
                    
            signed_bundle[key_name] = signed_ev
            
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
        
    print(json.dumps(signed_bundle, separators=(',', ':')))

if __name__ == '__main__':
    main()
