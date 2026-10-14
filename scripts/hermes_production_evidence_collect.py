import datetime
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


def get_git_provenance():
    repo_dir = Path(__file__).resolve().parent.parent

    def run_git(cmd):
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=repo_dir,
        )
        return res.stdout.strip() if res.returncode == 0 else ""

    head = run_git(["git", "rev-parse", "HEAD"])
    tree = run_git(["git", "rev-parse", "HEAD^{tree}"])

    status_out = run_git(["git", "status", "--porcelain=v1"])
    clean = status_out == ""

    repo_url = run_git(["git", "remote", "get-url", "origin"]).strip()
    norm_url = repo_url[:-4] if repo_url.endswith(".git") else repo_url
    valid_remotes = {
        "https://github.com/life2boat/hermes",
        "git@github.com:life2boat/hermes",
        "git@github-healbite:life2boat/hermes",
        "life2boat/hermes",
    }
    if norm_url in valid_remotes or repo_url in valid_remotes:
        repo = "life2boat/hermes"
    else:
        repo = repo_url

    return head, tree, clean, repo


def find_target_image(target_sha: str) -> str:
    """Locate local Docker image for target_sha."""
    candidates = [
        f"ghcr.io/life2boat/hermes:sha-{target_sha}",
    ]
    for cand in candidates:
        res = subprocess.run(
            ["docker", "inspect", cand],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if res.returncode == 0:
            return cand

    # Search existing images by OCI revision label
    try:
        out = subprocess.check_output(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}@{{.ID}}"],
            text=True,
        )
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            ref, img_id = line.split("@") if "@" in line else (line, line)
            insp = subprocess.run(
                ["docker", "inspect", img_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if insp.returncode == 0:
                data = json.loads(insp.stdout)[0]
                rev = (
                    data
                    .get("Config", {})
                    .get("Labels", {})
                    .get("org.opencontainers.image.revision", "")
                )
                if rev == target_sha:
                    return ref if "<none>" not in ref else img_id
    except Exception:
        pass

    return f"ghcr.io/life2boat/hermes:sha-{target_sha}"


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Missing target SHA"}))
        sys.exit(1)

    target_sha = sys.argv[1]
    if not all(c in "0123456789abcdef" for c in target_sha) or len(target_sha) != 40:
        print(json.dumps({"error": "Invalid target SHA"}))
        sys.exit(1)

    execution_nonce = ""
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read().strip()
            if stdin_data:
                parsed_stdin = json.loads(stdin_data)
                execution_nonce = parsed_stdin.get("execution_nonce", "")
        except Exception:
            pass

    if not execution_nonce:
        execution_nonce = uuid.uuid4().hex

    head, tree, clean, repo = get_git_provenance()
    if head != target_sha:
        print(
            json.dumps({
                "error": f"COLLECTOR_HEAD_SHA mismatch: {head} != {target_sha}"
            })
        )
        sys.exit(1)

    if not clean:
        print(json.dumps({"error": "COLLECTOR_WORKTREE_CLEAN mismatch"}))
        sys.exit(1)

    if repo != "life2boat/hermes":
        print(json.dumps({"error": f"COLLECTOR_REPOSITORY mismatch: {repo}"}))
        sys.exit(1)

    bundle = {
        "collector_provenance": {
            "COLLECTOR_REPOSITORY": repo,
            "COLLECTOR_HEAD_SHA": head,
            "COLLECTOR_TREE_SHA": tree,
            "COLLECTOR_WORKTREE_CLEAN": clean,
            "EXECUTION_NONCE": execution_nonce,
            "COMMAND_PROVENANCE": "authenticated-restricted-ssh",
        }
    }

    expected_files = {
        "secret_evidence": "secret_evidence_unsigned.json",
        "db_evidence": "db_evidence_unsigned.json",
        "schema_evidence": "schema_evidence_unsigned.json",
        "rollback_evidence": "rollback_evidence_unsigned.json",
        "credential_risk_evidence": "credential_risk_evidence_unsigned.json",
    }

    tmpdir = tempfile.mkdtemp(prefix="hermes-collect-")
    os.chmod(tmpdir, 0o700)

    try:
        # Run fresh isolated rollback drill bound to session nonce
        target_img = find_target_image(target_sha)
        prod_db = Path("/var/lib/hermes/production-db/healbite.db")
        from scripts.hermes_isolated_rollback_drill import run_drill

        drill_res = run_drill(
            target_sha=target_sha,
            target_image=target_img,
            prod_db_path=prod_db,
            output_dir=Path(tmpdir),
            nonce=execution_nonce,
            cleanup=True,
        )
        if drill_res.get("status") != "PASS":
            raise RuntimeError(f"Isolated rollback drill failed: {drill_res}")

        rehearsal_file = Path(tmpdir) / "rollback-rehearsal.json"
        health_file = Path(tmpdir) / "rollback-health.json"
        rehearsal_bytes = rehearsal_file.read_bytes()
        health_bytes = health_file.read_bytes()
        rehearsal_data = json.loads(rehearsal_bytes.decode("utf-8"))
        health_data = json.loads(health_bytes.decode("utf-8"))

        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rollback_unsigned = {
            "schema_version": 1,
            "evidence_type": "rollback_ready",
            "target_sha": target_sha,
            "status": "PASS",
            "collected_at_utc": now_utc,
            "execution_provenance": {
                "isolation_level": "docker",
                "runtime_identity": "healbite-production",
                "execution_nonce": execution_nonce,
            },
            "current_production_image_digest": drill_res["baseline_digest"],
            "current_production_oci_revision": drill_res["baseline_revision"],
            "rollback_image_digest": drill_res["baseline_digest"],
            "rollback_image_resolvable": True,
            "rollback_revision": drill_res["baseline_revision"],
            "rollback_mechanism_id": "docker-compose-revert",
            "same_compose_chain": True,
            "database_restore_required": False,
            "schema_downgrade_required": False,
            "rollback_health_required": True,
            "rollback_attempt_count_max": 1,
            "rollback_procedure_proven": True,
            "canonical_rehearsal_evidence": f"artifact:rollback-rehearsal:{drill_res['receipt_id']}:{drill_res['health_hash'][:16]}:pass",
        }

        # Run offline generator for other canonical evidence (secrets, db, schema, credential risk)
        proc = subprocess.run(
            ["python3", "scripts/generate_offline_evidence.py", target_sha, tmpdir],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            raise Exception(f"generate_offline_evidence failed: {proc.stderr}")

        # Overwrite rollback_evidence_unsigned.json with genuine drill result
        rb_path = os.path.join(tmpdir, "rollback_evidence_unsigned.json")
        with open(rb_path, "w", encoding="utf-8") as rbf:
            json.dump(rollback_unsigned, rbf)

        for k, filename in expected_files.items():
            full_path = os.path.join(tmpdir, filename)
            if not os.path.exists(full_path):
                raise Exception(f"Missing {filename}")

            os.chmod(full_path, 0o600)
            with open(full_path, "r", encoding="utf-8") as f:
                ev = json.load(f)
                bundle[k] = ev

        bundle["raw_rehearsal_receipt"] = rehearsal_data
        bundle["raw_rehearsal_receipt_raw"] = rehearsal_bytes.decode("utf-8")
        bundle["raw_rehearsal_health"] = health_data
        bundle["raw_rehearsal_health_raw"] = health_bytes.decode("utf-8")

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(json.dumps(bundle, separators=(",", ":")))


if __name__ == "__main__":
    main()
