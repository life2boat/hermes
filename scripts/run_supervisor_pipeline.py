"""run_supervisor_pipeline.py - CLI entry point for the supervisor pipeline.

Usage:
    python scripts/run_supervisor_pipeline.py \\
        --result <path-to-worker-result.json> \\
        --evidence-root <dir> \\
        --intent <path-to-intent.json> \\
        [--output <output-path>]

Exit codes:
    0  PASS
    1  FAIL
    2  BLOCKED or error

Does NOT depend on Astra, Antigravity GUI, or any live service.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure the repo root is on the path when run as a script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_engineering.contracts import Status
from ai_engineering.task_intent import deserialize_intent
from ai_engineering.supervisor.collector import CollectorError, ResultCollector
from ai_engineering.supervisor.validator import (
    VerifiedResult,
    validate_normalized_evidence,
    canonical_serialize_verified_result,
)


def _load_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _exit_code(status: Status) -> int:
    if status == Status.PASS:
        return 0
    if status == Status.FAIL:
        return 1
    return 2  # BLOCKED, UNKNOWN, etc.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hermes Autonomous Supervisor Pipeline (Task No.1)",
    )
    parser.add_argument(
        "--result", required=True, metavar="PATH",
        help="Path to worker result JSON file",
    )
    parser.add_argument(
        "--evidence-root", required=True, metavar="DIR",
        help="Directory containing artifact files referenced by the result",
    )
    parser.add_argument(
        "--intent", required=True, metavar="PATH",
        help="Path to TaskIntent JSON file",
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help="Optional path to write VerifiedResult JSON output",
    )
    args = parser.parse_args(argv)

    # Load inputs
    try:
        result_json = _load_text(args.result)
    except OSError as exc:
        print(f"ERROR: Cannot read result file: {exc}", file=sys.stderr)
        return 2

    try:
        intent_json = _load_text(args.intent)
    except OSError as exc:
        print(f"ERROR: Cannot read intent file: {exc}", file=sys.stderr)
        return 2

    # Parse intent
    try:
        intent = deserialize_intent(intent_json)
    except Exception as exc:
        print(f"ERROR: Failed to parse intent: {exc}", file=sys.stderr)
        return 2

    # Collect
    collector = ResultCollector()
    try:
        ne = collector.collect(
            result_bundle_json=result_json,
            evidence_root=args.evidence_root,
            intent=intent,
        )
    except CollectorError as exc:
        print(f"BLOCKED: Collector rejected result: {exc.code}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: Unexpected collection error: {exc}", file=sys.stderr)
        return 2

    # Validate
    try:
        vr = validate_normalized_evidence(ne, intent)
    except Exception as exc:
        print(f"ERROR: Validation failed unexpectedly: {exc}", file=sys.stderr)
        return 2

    # Output
    vr_json = canonical_serialize_verified_result(vr)
    print(vr_json)

    if args.output:
        try:
            Path(args.output).write_text(vr_json, encoding="utf-8")
            print(f"VerifiedResult written to: {args.output}", file=sys.stderr)
        except OSError as exc:
            print(f"WARNING: Could not write output file: {exc}", file=sys.stderr)

    # Status line
    print(f"\nSTATUS={vr.status.value}", file=sys.stderr)
    if vr.blockers:
        print(f"BLOCKERS={', '.join(vr.blockers)}", file=sys.stderr)

    return _exit_code(vr.status)


if __name__ == "__main__":
    sys.exit(main())
