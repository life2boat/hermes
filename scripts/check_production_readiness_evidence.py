#!/usr/bin/env python3
"""Offline CLI for ProductionReadinessEvidenceReceipt verification.

Usage:
    python scripts/check_production_readiness_evidence.py \\
        --attestation <file> \\
        --comparison <file> \\
        --candidate-sha <sha> \\
        --observed-head-sha <sha> \\
        --runtime-evidence-source-sha <sha> \\
        --evaluated-at <UTC timestamp> \\
        --max-age-seconds <N> \\
        --post-health-status <PASS|FAIL|INSUFFICIENT_EVIDENCE> \\
        --expected-target <target> \\
        [--repository <repo>] \\
        [--canonical-remote <remote>] \\
        [--expected-runtime-evidence-source-sha <sha>] \\
        [--output <file>] \\
        [--gate-evidence-output <file>]

All inputs/outputs are file-based. No production or network access.
The CLI may accept an explicit --evaluated-at timestamp from the caller.
Core decision logic does NOT call datetime.now() internally.

EVIDENCE_EXPANDS_AUTHORITY=false
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_engineering.production_readiness_evidence import (
    PostCollectionHealthStatus,
    ProductionReadinessEvidenceError,
    deserialize_receipt,
    normalize_receipt,
    serialize_receipt,
    to_production_readiness_gate_evidence,
    verify_production_readiness,
)
from ai_engineering.production_runtime_attestation import (
    ProductionRuntimeAttestationError,
    deserialize_attestation,
    deserialize_comparison,
)
from ai_engineering.release_gate import GateName


def _load_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--attestation", required=True, help="Path to attestation JSON")
    parser.add_argument("--comparison", required=True, help="Path to comparison JSON")
    parser.add_argument("--candidate-sha", required=True, help="40-char candidate Git SHA")
    parser.add_argument(
        "--observed-head-sha", required=True, help="40-char observed HEAD Git SHA"
    )
    parser.add_argument(
        "--runtime-evidence-source-sha",
        required=True,
        help="40-char SHA of the source used to run/verify evidence collectors",
    )
    parser.add_argument(
        "--evaluated-at",
        required=True,
        help="Evaluation timestamp in UTC (YYYY-MM-DDTHH:MM:SSZ)",
    )
    parser.add_argument(
        "--max-age-seconds",
        required=True,
        type=int,
        help="Maximum evidence age in seconds (positive integer)",
    )
    parser.add_argument(
        "--post-health-status",
        required=True,
        choices=["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"],
        help="Post-collection health check result",
    )
    parser.add_argument(
        "--expected-target",
        required=True,
        help="Expected attestation target identifier",
    )
    parser.add_argument(
        "--repository",
        default="life2boat/hermes",
        help="Repository identifier (default: life2boat/hermes)",
    )
    parser.add_argument(
        "--canonical-remote",
        default="origin",
        help="Canonical remote name (default: origin)",
    )
    parser.add_argument(
        "--expected-runtime-evidence-source-sha",
        default=None,
        help="Expected runtime evidence source SHA for binding verification",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path for the ProductionReadinessEvidenceReceipt JSON",
    )
    parser.add_argument(
        "--gate-evidence-output",
        default=None,
        help="Output path for GateEvidence JSON (optional)",
    )

    args = parser.parse_args(argv)

    try:
        attestation = deserialize_attestation(_load_bytes(args.attestation))
        comparison = deserialize_comparison(_load_bytes(args.comparison))
    except ProductionRuntimeAttestationError as exc:
        print(f"ERROR: attestation/comparison validation failed: {exc.code}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"ERROR: file not found: {exc}", file=sys.stderr)
        return 1

    try:
        receipt = verify_production_readiness(
            attestation=attestation,
            comparison=comparison,
            candidate_sha=args.candidate_sha,
            observed_head_sha=args.observed_head_sha,
            runtime_evidence_source_sha=args.runtime_evidence_source_sha,
            expected_target=args.expected_target,
            repository=args.repository,
            canonical_remote=args.canonical_remote,
            evaluated_at_utc=args.evaluated_at,
            max_age_seconds=args.max_age_seconds,
            post_collection_health_status=PostCollectionHealthStatus(args.post_health_status),
            expected_runtime_evidence_source_sha=args.expected_runtime_evidence_source_sha,
        )
    except ProductionReadinessEvidenceError as exc:
        print(f"ERROR: verification failed: {exc.code}", file=sys.stderr)
        return 1

    receipt_json = serialize_receipt(receipt)

    if args.output:
        out_path = Path(args.output)
        if out_path.exists():
            print(f"ERROR: output file already exists (create-only): {out_path}", file=sys.stderr)
            return 1
        out_path.write_bytes(receipt_json)
        print(f"Receipt written to: {out_path}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(receipt_json)
        sys.stdout.buffer.write(b"\n")

    # Emit status summary to stderr
    print(
        f"FINAL_STATUS={receipt.final_status}  "
        f"REASONS={','.join(receipt.reason_codes)}",
        file=sys.stderr,
    )

    if args.gate_evidence_output:
        try:
            gate = to_production_readiness_gate_evidence(receipt, required=True)
        except ProductionReadinessEvidenceError as exc:
            print(f"ERROR: gate evidence adapter failed: {exc.code}", file=sys.stderr)
            return 1
        gate_path = Path(args.gate_evidence_output)
        if gate_path.exists():
            print(
                f"ERROR: gate evidence output file already exists (create-only): {gate_path}",
                file=sys.stderr,
            )
            return 1
        gate_json = json.dumps(
            {
                "gate_name": gate.gate_name.value,
                "required": gate.required,
                "status": gate.status.value,
                "evidence_refs": list(gate.evidence_refs),
                "reason_codes": list(gate.reason_codes),
                "evidence_digest": gate.evidence_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        gate_path.write_bytes(gate_json)
        print(f"GateEvidence written to: {gate_path}", file=sys.stderr)

    # Exit 0 for PASS, 2 for FAIL or BLOCKED, 1 for error
    if receipt.final_status == "PASS":
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
