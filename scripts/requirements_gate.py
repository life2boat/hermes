"""CLI for the Requirements Quality Gate.

Usage:
    python scripts/requirements_gate.py --intent INTENT.json \
        --clarification CLARIFICATION.json --review REVIEW.json \
        [--output gate-report.json]

Exit codes:
    0 = gate PASS (ready for execution)
    1 = gate FAIL (blocking reasons present)
    2 = invalid input / contract failure
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repository root is on sys.path when script is executed directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from scripts._cli_utils import (
        SafeReadError,
        OutputAliasError,
        check_output_alias,
        safe_read,
    )
except ImportError:
    from _cli_utils import (
        SafeReadError,
        OutputAliasError,
        check_output_alias,
        safe_read,
    )

from ai_engineering.task_intent import deserialize_intent, TaskIntentValidationError
from ai_engineering.requirements_gate import (
    deserialize_clarification,
    deserialize_review,
    evaluate_requirements_gate,
    serialize_gate,
    RequirementsGateError,
    GateStatus,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deterministic Requirements Quality Gate.")
    p.add_argument(
        "--intent", type=Path, required=True, help="Path to TaskIntent JSON file."
    )
    p.add_argument(
        "--clarification",
        type=Path,
        required=True,
        help="Path to ClarificationReport JSON file.",
    )
    p.add_argument(
        "--review",
        type=Path,
        required=True,
        help="Path to RequirementsQualityReview JSON file.",
    )
    p.add_argument(
        "--output",
        type=Path,
        help="Write canonical RequirementsGateReport to this path.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.output is not None:
        try:
            check_output_alias(
                args.output,
                {
                    "--intent": args.intent,
                    "--clarification": args.clarification,
                    "--review": args.review,
                },
                "requirements_gate",
            )
        except OutputAliasError as exc:
            print(exc.message, file=sys.stderr)
            return 2

    try:
        intent_raw = safe_read(args.intent, "requirements_gate")
        clar_raw = safe_read(args.clarification, "requirements_gate")
        review_raw = safe_read(args.review, "requirements_gate")
    except SafeReadError as exc:
        print(exc.message, file=sys.stderr)
        return 2

    try:
        intent = deserialize_intent(intent_raw)
    except TaskIntentValidationError as exc:
        print(f"requirements_gate: {exc.code}", file=sys.stderr)
        return 2

    try:
        clarification = deserialize_clarification(clar_raw)
        review = deserialize_review(review_raw)
        report = evaluate_requirements_gate(intent, clarification, review)
    except RequirementsGateError as exc:
        print(f"requirements_gate: {exc.code}", file=sys.stderr)
        return 2

    canonical_json = serialize_gate(report)

    if args.output is not None:
        out_path = args.output
        if out_path.is_symlink():
            print(f"requirements_gate: OUTPUT_UNSAFE: {out_path}", file=sys.stderr)
            return 2
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(canonical_json + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"requirements_gate: OUTPUT_WRITE_FAILED: {exc}", file=sys.stderr)
            return 2
    else:
        print(canonical_json)

    if report.status == GateStatus.FAIL:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
