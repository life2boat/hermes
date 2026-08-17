import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ai_engineering.production_collectors import (
    DockerRuntimeCollector,
    QdrantReadOnlyCollector,
    SecretSourceStructuralCollector,
    SqliteReadOnlyCollector,
    collect_production_attestation,
)
from ai_engineering.production_runtime_attestation import (
    ComparisonStatus,
    IntendedProductionState,
    compare_production_runtime,
    create_intended_state,
    serialize_attestation,
    serialize_comparison,
)


def get_git_output(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def check_source_identity(expected_sha: str) -> None:
    try:
        head_sha = get_git_output(["git", "rev-parse", "HEAD"])
        if head_sha != expected_sha:
            print(f"SOURCE_IDENTITY=FAIL: HEAD {head_sha} != expected {expected_sha}")
            sys.exit(1)

        status_output = get_git_output(["git", "status", "--porcelain=v1"])
        if status_output:
            print("SOURCE_IDENTITY=FAIL: Working tree is dirty")
            sys.exit(1)

    except subprocess.CalledProcessError:
        print("SOURCE_IDENTITY=FAIL: Git commands failed")
        sys.exit(1)


def save_evidence(data: bytes, prefix: str, sha: str) -> Path:
    timestamp = int(time.time())
    evidence_dir = Path("/home/hermes/private_backups/hermes-agent")
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    filename = f"{prefix}_{sha}_{timestamp}.json"
    evidence_path = evidence_dir / filename

    # Create-only semantics, fail if exists
    fd = os.open(str(evidence_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as f:
        f.write(data)

    return evidence_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected-source-sha", required=True, help="Expected 40-hex Git SHA"
    )
    args = parser.parse_args()

    print("Running B2 preflight checks...")

    # Source identity checks
    check_source_identity(args.expected_source_sha)

    # Preflight: Target must be WSL2 Ubuntu
    if not Path("/proc/version").exists():
        print("Preflight failed: Not running on Linux")
        sys.exit(1)

    version_str = Path("/proc/version").read_text(encoding="utf-8").lower()
    if "microsoft" not in version_str and "wsl" not in version_str:
        print("Preflight failed: Not running on WSL2")
        sys.exit(1)

    print("Preflight passed. Starting collectors...")

    # Define targets and baselines
    TARGET = "HOME_WSL2/Ubuntu"
    COMPOSE_PROJECT = "healbite-s72-family-invite-main"
    SERVICE = "hermes-bot"
    DB_PATH = "/var/lib/hermes/production-db/healbite.db"
    CONTAINER_DB_MOUNT = "/home/hermes/healbite.db"
    QDRANT_URL = "http://localhost:6333"
    QDRANT_COLLECTION = "healbite_memory_os"
    SECRET_SOURCE = "/etc/hermes/hermes-production.env"
    LEGACY_SECRET_SOURCE = "/home/hermes/.hermes/.env"

    collectors = [
        DockerRuntimeCollector("hermes-bot", expected_db_mount=CONTAINER_DB_MOUNT),
        SqliteReadOnlyCollector(DB_PATH),
        QdrantReadOnlyCollector(QDRANT_URL, QDRANT_COLLECTION),
        SecretSourceStructuralCollector(
            expected_path=SECRET_SOURCE, legacy_path=LEGACY_SECRET_SOURCE
        ),
    ]

    print("Collecting attestation...")
    attestation = collect_production_attestation(TARGET, collectors)

    evidence_path = save_evidence(
        serialize_attestation(attestation), "attestation_b2", args.expected_source_sha
    )
    print(f"Attestation collected and saved to {evidence_path}")
    print(f"Attestation ID: {attestation.attestation_id}")

    # Build intended state
    print("Building intended state...")
    expected_observations = {
        "docker_runtime": {
            "running": True,
            "compose_project": COMPOSE_PROJECT,
            "compose_service": SERVICE,
            "db_mount_matches_expected": True,
        },
        "sqlite_read_only": {
            "sqlite_open_read_only": True,
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "qdrant_read_only": {
            "reachable": True,
            "collection_exists": True,
        },
        "secret_source_structural": {
            "approved_source_exists": True,
        },
    }

    intended = create_intended_state(
        target=TARGET, expected_observations=expected_observations
    )

    print("Comparing runtime...")
    comparison = compare_production_runtime(intended, attestation)

    comp_evidence_path = save_evidence(
        serialize_comparison(comparison), "comparison_b2", args.expected_source_sha
    )
    print(f"Comparison saved to {comp_evidence_path}")

    print("\n--- RESULTS ---")
    print(f"Status: {comparison.status.value}")
    if comparison.drifted_observations:
        print(f"Drifted: {comparison.drifted_observations}")
    if comparison.missing_observations:
        print(f"Missing: {comparison.missing_observations}")

    print("\n--- POST-COLLECTION HEALTH ---")
    # Execute a lightweight second pass to prove we didn't crash it
    post_attestation = collect_production_attestation(TARGET, collectors)

    # Validate critical structural facts haven't drifted because of collection
    # e.g., restart count shouldn't increase, running must be true.
    docker_pre = attestation.collectors[0].observations
    docker_post = post_attestation.collectors[0].observations
    sqlite_post = post_attestation.collectors[1].observations

    if docker_post.get("restart_count") != docker_pre.get("restart_count"):
        print("POST-HEALTH FAIL: Restart count increased!")
        sys.exit(1)

    if not docker_post.get("running"):
        print("POST-HEALTH FAIL: Container stopped!")
        sys.exit(1)

    if (
        sqlite_post.get("integrity") != "ok"
        or sqlite_post.get("foreign_key_violations") != 0
    ):
        print("POST-HEALTH FAIL: SQLite integrity/FKs failed in post-health!")
        sys.exit(1)

    print("POST-HEALTH: PASS")


if __name__ == "__main__":
    main()
