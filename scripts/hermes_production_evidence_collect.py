import sys
import json
import os
import subprocess

def get_git_provenance():
    def run_git(cmd):
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.stdout.strip() if res.returncode == 0 else ''
        
    head = run_git(['git', 'rev-parse', 'HEAD'])
    tree = run_git(['git', 'rev-parse', 'HEAD^{tree}'])
    
    res = subprocess.run(['git', 'diff', '--quiet', 'HEAD'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    clean = (res.returncode == 0)
    
    return head, tree, clean

def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Missing target SHA'}))
        sys.exit(1)
        
    target_sha = sys.argv[1]
    if not all(c in '0123456789abcdef' for c in target_sha) or len(target_sha) != 40:
        print(json.dumps({'error': 'Invalid target SHA'}))
        sys.exit(1)
        
    head, tree, clean = get_git_provenance()
    if head != target_sha:
        print(json.dumps({'error': f'COLLECTOR_HEAD_SHA mismatch: {head} != {target_sha}'}))
        sys.exit(1)
        
    if not clean:
        print(json.dumps({'error': 'COLLECTOR_WORKTREE_CLEAN mismatch'}))
        sys.exit(1)

    bundle = {
        'collector_provenance': {
            'COLLECTOR_REPOSITORY': 'life2boat/hermes',
            'COLLECTOR_HEAD_SHA': head,
            'COLLECTOR_TREE_SHA': tree,
            'COLLECTOR_WORKTREE_CLEAN': clean
        }
    }
    
    expected_files = {
        'secret_evidence': 'secret_evidence_unsigned.json',
        'db_evidence': 'db_evidence_unsigned.json',
        'schema_evidence': 'schema_evidence_unsigned.json',
        'rollback_evidence': 'rollback_evidence_unsigned.json',
        'credential_risk_evidence': 'credential_risk_evidence_unsigned.json'
    }
    
    try:
        # Run it as a subprocess to completely avoid polluting our memory and imports
        proc = subprocess.run(['python3', 'scripts/generate_offline_evidence.py', target_sha],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise Exception(f'generate_offline_evidence failed: {proc.stderr}')
            
        for k, filename in expected_files.items():
            if not os.path.exists(filename):
                raise Exception(f'Missing {filename}')
                
            with open(filename, 'r', encoding='utf-8') as f:
                ev = json.load(f)
                bundle[k] = ev
                
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
    finally:
        for filename in expected_files.values():
            if os.path.exists(filename):
                try:
                    os.remove(filename)
                except Exception:
                    pass
                    
    print(json.dumps(bundle, separators=(',', ':')))

if __name__ == '__main__':
    main()
