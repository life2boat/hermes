import sys
import json
import subprocess

def run(cmd):
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error running {cmd}: {res.stderr}", file=sys.stderr)
        sys.exit(1)
    return res.stdout.strip()

def main():
    target_sha = sys.argv[1]
    
    pr_json = run(f"gh api repos/life2boat/hermes/commits/{target_sha}/pulls")
    prs = json.loads(pr_json)
    if not prs:
        print(f"No PR found for commit {target_sha}", file=sys.stderr)
        sys.exit(1)
        
    pr = prs[0]
    evidence = {
        "pr_number": pr['number'],
        "pr_head_sha": pr['head']['sha'],
        "pr_merge_sha": pr['merge_commit_sha'],
        "pr_merged_at": pr['merged_at'],
    }
    
    final_main_tree_sha = run(f"git rev-parse {target_sha}^{{tree}}")
    run(f"git fetch origin pull/{pr['number']}/head")
    pr_head_tree_sha = run(f"git rev-parse {pr['head']['sha']}^{{tree}}")
    
    evidence["final_main_tree_sha"] = final_main_tree_sha
    evidence["pr_head_tree_sha"] = pr_head_tree_sha
    
    runs_json = run(f"gh api repos/life2boat/hermes/actions/runs?head_sha={pr['head']['sha']}&per_page=100")
    runs_data = json.loads(runs_json)
    
    evidence["workflow_runs"] = []
    for r in runs_data.get("workflow_runs", []):
        evidence["workflow_runs"].append({
            "workflow_name": r["name"],
            "workflow_id": r["workflow_id"],
            "run_id": r["id"],
            "head_sha": r["head_sha"],
            "status": r["status"],
            "conclusion": r["conclusion"],
            "created_at": r["created_at"],
            "completed_at": r["updated_at"]
        })
        
    with open("structured_ci_evidence.json", "w", encoding="utf-8") as f:
        json.dump(evidence, f)
        
if __name__ == "__main__":
    main()
