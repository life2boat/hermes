"""CLI for the Cross-Artifact Analyzer.

Usage:
    python scripts/analyze_task.py --intent INTENT.json --lineage LINEAGE.json
    python scripts/analyze_task.py --intent INTENT.json --lineage LINEAGE.json --output report.json

Exit codes:
    0 = analysis completed, no ERROR findings
    1 = analysis completed, ERROR findings detected
    2 = invalid input / contract validation failure

Properties:
    Read-only  – never mutates input artifacts.
    Offline    – zero provider calls, zero network calls.
    Deterministic – same inputs produce byte-equivalent JSON output.
"""

from __future__ import annotations

import argparse
import json
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

_MAX_FILE_BYTES = 512 * 1024  # 512 KB


class _SafeReadError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _safe_read(path: Path) -> bytes:
    """Read a file with basic safety checks."""
    if path.is_symlink():
        raise _SafeReadError(f"analyze_task: UNSAFE_PATH: {path}")
    if not path.is_file():
        raise _SafeReadError(f"analyze_task: FILE_NOT_FOUND: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _SafeReadError(f"analyze_task: FILE_UNREADABLE: {exc}") from exc
    if len(raw) > _MAX_FILE_BYTES:
        raise _SafeReadError(f"analyze_task: FILE_TOO_LARGE: {path}")
    return raw


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
""",
    )
    p.add_argument("--intent", type=Path, required=True, help="Path to TaskIntent JSON file.")
    p.add_argument("--lineage", type=Path, required=True, help="Path to TaskLineage JSON file.")
    p.add_argument(
        "--output",
        type=Path,
        help="Write canonical JSON report to this path. If omitted, prints to stdout.",
    )
    p.add_argument(
        "--human",
        action="store_true",
        help="Print a human-readable summary to stdout.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    # Load and validate inputs.
    try:
        intent_raw = _safe_read(args.intent)
        lineage_raw = _safe_read(args.lineage)
    except _SafeReadError as exc:
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
    report = analyze(intent, lineage)

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
        # Always print human-readable summary when no --output, or when --human flag set.
        d = report_to_dict(report)
        print(f"analysis_id : {report.analysis_id}")
        print(f"intent      : {report.intent_task_id}")
        print(f"base_sha    : {report.source_base_sha}")
        print(f"findings    : {d['summary']['total']} total  "
              f"({d['summary']['errors']} errors, "
              f"{d['summary']['warnings']} warnings, "
              f"{d['summary']['infos']} infos)")
        if report.findings:
            print()
            for f in report.findings:
                print(f"  [{f.severity.value}] {f.code.value}")
                print(f"    {f.message}")
                print(f"    ref: {f.primary_reference.artifact_kind}::{f.primary_reference.identity}")

    if args.output is not None and not args.human:
        # If writing to file and not asked for human output, print just the path.
        print(f"ANALYSIS_REPORT_WRITTEN={args.output}")

    return 1 if report.has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
