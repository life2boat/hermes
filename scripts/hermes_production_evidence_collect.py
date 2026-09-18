import sys
import json
import os
import subprocess
import tempfile
import shutil

def get_git_provenance():
    def run_git(cmd):
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.stdout.strip() if res.returncode == 0 else ''
        
    head = run_git(['git', 'rev-parse', 'HEAD'])
    tree = run_git(['git', 'rev-parse', 'HEAD^{tree}'])
    
    status_out = run_git(['git', 'status', '--porcelain=v1'])
    clean = (status_out == '')
    
    repo_url = run_git(['git', 'remote', 'get-url', 'origin'])
    # Extract owner/repo
    if 'life2boat/hermes' in repo_url:
        repo = 'life2boat/hermes'
    else:
        repo = repo_url
        
    return head, tree, clean, repo

def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Missing target SHA'}))
        sys.exit(1)
        
    target_sha = sys.argv[1]
    if not all(c in '0123456789abcdef' for c in target_sha) or len(target_sha) != 40:
        print(json.dumps({'error': 'Invalid target SHA'}))
        sys.exit(1)
        
    head, tree, clean, repo = get_git_provenance()
    if head != target_sha:
        print(json.dumps({'error': f'COLLECTOR_HEAD_SHA mismatch: {head} != {target_sha}'}))
        sys.exit(1)
        
    if not clean:
        print(json.dumps({'error': 'COLLECTOR_WORKTREE_CLEAN mismatch'}))
        sys.exit(1)
        
    if repo != 'life2boat/hermes':
        print(json.dumps({'error': f'COLLECTOR_REPOSITORY mismatch: {repo}'}))
        sys.exit(1)

    bundle = {
        'collector_provenance': {
            'COLLECTOR_REPOSITORY': repo,
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
    
    tmpdir = tempfile.mkdtemp(prefix='hermes-collect-')
    os.chmod(tmpdir, 0o700)
    
    try:
        proc = subprocess.run(['python3', 'scripts/generate_offline_evidence.py', target_sha, tmpdir],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise Exception(f'generate_offline_evidence failed: {proc.stderr}')
            
        for k, filename in expected_files.items():
            full_path = os.path.join(tmpdir, filename)
            if not os.path.exists(full_path):
                raise Exception(f'Missing {filename}')
                
            os.chmod(full_path, 0o600)
            with open(full_path, 'r', encoding='utf-8') as f:
                ev = json.load(f)
                bundle[k] = ev
                
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
                    
    print(json.dumps(bundle, separators=(',', ':')))

if __name__ == '__main__':
    main()
