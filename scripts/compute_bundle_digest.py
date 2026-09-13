import json
import sys
import hashlib

def compute_canonical_digest_from_dict(bundle: dict) -> str:
    bundle_copy = dict(bundle)
    if 'bundle_digest' in bundle_copy:
        del bundle_copy['bundle_digest']
        
    serialized = json.dumps(bundle_copy, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

def compute_canonical_digest(filepath: str) -> str:
    with open(filepath, 'r', encoding='utf-8') as f:
        bundle = json.load(f)
    return compute_canonical_digest_from_dict(bundle)

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('Usage: compute_bundle_digest.py <path_to_json>')
        sys.exit(1)
    print(compute_canonical_digest(sys.argv[1]))
