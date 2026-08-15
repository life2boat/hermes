#!/usr/bin/env python3
"""CLI script to verify evidence bundle artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add root path to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_engineering.convergence import deserialize_evidence_bundle
from ai_engineering.evidence_verifier import (
    serialize_verification_result,
    verify_evidence_bundle_artifacts,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify artifacts in an evidence bundle")
    parser.add_argument("--bundle", required=True, help="Path to evidence bundle JSON")
    parser.add_argument("--evidence-root", required=True, help="Root directory containing artifacts")
    parser.add_argument("--output", help="Optional output JSON path")
    args = parser.parse_args()

    try:
        bundle_path = Path(args.bundle)
        if not bundle_path.is_file():
            print(f"Error: Bundle file not found: {args.bundle}", file=sys.stderr)
            sys.exit(1)

        bundle = deserialize_evidence_bundle(bundle_path.read_text(encoding="utf-8"))
        result = verify_evidence_bundle_artifacts(bundle, args.evidence_root)
        serialized = serialize_verification_result(result)

        if args.output:
            Path(args.output).write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        else:
            print(json.dumps(serialized, indent=2))

        if result.overall != "PASS":
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
