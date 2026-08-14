"""CLI for the Cross-Artifact Analyzer.

Usage:
    python scripts/analyze_task.py --intent INTENT.json --lineage LINEAGE.json
    python scripts/analyze_task.py --intent INTENT.json --lineage LINEAGE.json \
        --expected-sha <40-hex-chars>
    python scripts/analyze_task.py --intent INTENT.json --lineage LINEAGE.json \
        --output report.json

Exit codes:
    0 = analysis completed, no ERROR findings
    1 = analysis completed, ERROR findings detected
    2 = invalid input / contract validation failure

Properties:
    Read-only  – never mutates input artifacts.
    Offline    – zero provider calls, zero network calls.
    Deterministic – same inputs produce byte-equivalent JSON output.

Output aliasing protection:
    --output must not resolve to the same path as --intent or --lineage.
    Aliasing is detected via Path.resolve() before any write occurs.
    Violation returns exit code 2 with SAFE_WRITE error.

Deferred rules (not implemented in schema v1):
    MUTATION_OUTSIDE_ALLOWED_SCOPE   DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE
    MUTATION_IN_FORBIDDEN_SCOPE      DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE
    REQUIRED_GATE_COVERAGE           DEFERRED_DUE_TO_MISSING_CANONICAL_MAPPING
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ai_engineering.task_analysis import (
    AnalysisInputError,
    analyze,
    load_intent_from_bytes,
    load_lineage_from_bytes,
    report_to_dict,
    serialize_report,
)

try:
    from scripts._cli_utils import (
        SafeReadError,
        check_output_alias,
        resolve_path,
        safe_read,
    )
except ImportError:
    from _cli_utils import SafeReadError, check_output_alias, resolve_path, safe_read

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Deterministic read-only Cross-Artifact Analyzer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0 = no ERROR findings
  1 = ERROR findings detected
  2 = invalid input / contract validation failure

The analyzer is READ ONLY and OFFLINE. It never mutates input artifacts
and never makes provider or network calls.

SOURCE_IDENTITY_MISMATCH is only checked when --expected-sha is supplied.
Without an independent anchor, source-SHA verification cannot be deterministic.

--output must not alias --intent or --lineage (fail-closed, exit 2).

Deferred rules (not implemented in schema v1):
  MUTATION_OUTSIDE_ALLOWED_SCOPE   DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE
  MUTATION_IN_FORBIDDEN_SCOPE      DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE
  REQUIRED_GATE_COVERAGE           DEFERRED_DUE_TO_MISSING_CANONICAL_MAPPING
""",
    )
    p.add_argument(
        "--intent", type=Path, required=True, help="Path to TaskIntent JSON file."
    )
    p.add_argument(
        "--lineage", type=Path, required=True, help="Path to TaskLineage JSON file."
    )
    p.add_argument(
        "--expected-sha",
        dest="expected_sha",
        type=str,
        default=None,
        help=(
            "Independent canonical base SHA (40 hex chars) to verify against "
            "intent.source_base_sha. When omitted, SOURCE_IDENTITY_MISMATCH "
            "is not checked."
        ),
    )
    p.add_argument(
        "--output",
        type=Path,
        help=(
            "Write canonical JSON report to this path. Must not resolve to "
            "the same file as --intent or --lineage. If omitted, prints to stdout."
        ),
    )
    p.add_argument(
        "--human",
        action="store_true",
        help="Print a human-readable summary to stdout.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    # Validate --expected-sha format.
    expected_sha: str | None = None
    if args.expected_sha is not None:
        if _SHA_RE.fullmatch(args.expected_sha) is None:
            print(
                "analyze_task: EXPECTED_SHA_INVALID: must be 40 lowercase hex chars",
                file=sys.stderr,
            )
            return 2
        expected_sha = args.expected_sha

    # Output aliasing protection — resolve before reading input.
    if args.output is not None:
        from scripts._cli_utils import OutputAliasError

        try:
            check_output_alias(
                args.output,
                {"--intent": args.intent, "--lineage": args.lineage},
                "analyze_task",
            )
        except OutputAliasError as exc:
            print(exc.message, file=sys.stderr)
            return 2

    # Load and validate inputs.
    try:
        intent_raw = safe_read(args.intent, "analyze_task")
        lineage_raw = safe_read(args.lineage, "analyze_task")
    except SafeReadError as exc:
        print(exc.message, file=sys.stderr)
        return 2

    try:
        intent = load_intent_from_bytes(intent_raw)
    except AnalysisInputError as exc:
        print(f"analyze_task: {exc.code}", file=sys.stderr)
        return 2

    try:
        lineage = load_lineage_from_bytes(lineage_raw)
    except AnalysisInputError as exc:
        print(f"analyze_task: {exc.code}", file=sys.stderr)
        return 2

    # Run analysis (read-only, offline, deterministic).
    try:
        report = analyze(intent, lineage, expected_base_sha=expected_sha)
    except AnalysisInputError as exc:
        print(f"analyze_task: {exc.code}", file=sys.stderr)
        return 2

    # Output.
    canonical_json = serialize_report(report)

    if args.output is not None:
        output_path = args.output
        if output_path.is_symlink():
            print(f"analyze_task: OUTPUT_UNSAFE: {output_path}", file=sys.stderr)
            return 2
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(canonical_json + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"analyze_task: OUTPUT_WRITE_FAILED: {exc}", file=sys.stderr)
            return 2

    if args.human or args.output is None:
        d = report_to_dict(report)
        print(f"analysis_id  : {report.analysis_id}")
        print(f"lineage_dgst : {report.lineage_digest}")
        print(f"intent       : {report.intent_task_id}")
        print(f"base_sha     : {report.source_base_sha}")
        print(
            f"findings     : {d['summary']['total']} total  "
            f"({d['summary']['errors']} errors, "
            f"{d['summary']['warnings']} warnings, "
            f"{d['summary']['infos']} infos)"
        )
        if report.findings:
            print()
            for f in report.findings:
                print(f"  [{f.severity.value}] {f.code.value}")
                print(f"    {f.message}")
                print(
                    f"    ref: {f.primary_reference.artifact_kind}::{f.primary_reference.identity}"
                )

    if args.output is not None and not args.human:
        print(f"ANALYSIS_REPORT_WRITTEN={args.output}")

    return 1 if report.has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
