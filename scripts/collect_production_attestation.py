import json
import os
import sys
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

EVIDENCE_PATH = Path("/home/hermes/private_backups/hermes-agent/attestation_b2.json")
COMPARISON_PATH = Path("/home/hermes/private_backups/hermes-agent/comparison_b2.json")

def main():
    print("Running B2 preflight checks...")
    # Preflight: Target must be WSL2 Ubuntu
    if not Path("/proc/version").exists():
        print("Preflight failed: Not running on Linux")
        sys.exit(1)
        
    version_str = Path("/proc/version").read_text().lower()
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
        SecretSourceStructuralCollector(expected_path=SECRET_SOURCE, legacy_path=LEGACY_SECRET_SOURCE),
    ]

    print("Collecting attestation...")
    attestation = collect_production_attestation(TARGET, collectors)

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_bytes(serialize_attestation(attestation))
    print(f"Attestation collected and saved to {EVIDENCE_PATH}")
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

    intended = create_intended_state(target=TARGET, expected_observations=expected_observations)

    print("Comparing runtime...")
    comparison = compare_production_runtime(intended, attestation)
    
    COMPARISON_PATH.write_bytes(serialize_comparison(comparison))
    print(f"Comparison saved to {COMPARISON_PATH}")

    print("\n--- RESULTS ---")
    print(f"Status: {comparison.status.value}")
    if comparison.drifted_observations:
        print(f"Drifted: {comparison.drifted_observations}")
    if comparison.missing_observations:
        print(f"Missing: {comparison.missing_observations}")


if __name__ == "__main__":
    main()
