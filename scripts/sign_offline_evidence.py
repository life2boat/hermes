import sys
import json
import os
import hashlib
import hmac

def main():
    if len(sys.argv) < 3:
        print('Usage: python3 sign_offline_evidence.py <input_payload.json> <output.json>')
        sys.exit(1)
        
    input_file = sys.argv[1]
    output_file = sys.argv[2]
    
    key = os.environ.get('HERMES_PROVENANCE_KEY', '')
    if not key:
        print('Error: HERMES_PROVENANCE_KEY environment variable is required')
        sys.exit(1)
        
    with open(input_file, 'r', encoding='utf-8') as f:
        ev = json.load(f)
        
    payload = {
        k: v
        for k, v in ev.items()
        if k not in ('evidence_digest', 'execution_provenance')
    }
    
    prov = ev.get('execution_provenance', {})
    prov_copy = {
        k: v
        for k, v in prov.items()
        if k != 'signature'
    }
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
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(final_evidence, f, indent=2)
        f.write('\n')
        
    print(f'Signed evidence written to {output_file}')

if __name__ == '__main__':
    main()
