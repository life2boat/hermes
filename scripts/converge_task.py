"""CLI entry point for PR-4 Evidence-Bound Convergence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repository root is on sys.path when script is executed directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from scripts._cli_utils import (
        OutputAliasError,
        SafeReadError,
        check_output_alias,
        safe_read,
    )
except ImportError:
    from _cli_utils import (
        OutputAliasError,
        SafeReadError,
        check_output_alias,
        safe_read,
    )

from ai_engineering.task_intent import deserialize_intent, validate_lineage
from ai_engineering.requirements_gate import (
    deserialize_clarification,
    deserialize_review,
)
from ai_engineering.convergence import (
    deserialize_evidence_bundle,
    evaluate_convergence,
    serialize_convergence_report,
    ConvergenceError,
    ConvergenceStatus,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate evidence-bound convergence.")
    parser.add_argument(
        "--intent", type=Path, required=True, help="TaskIntent JSON file"
    )
    parser.add_argument(
        "--clarification",
        type=Path,
        required=True,
        help="ClarificationReport JSON file",
    )
    parser.add_argument(
        "--review", type=Path, required=True, help="RequirementsQualityReview JSON file"
    )
    parser.add_argument(
        "--lineage", type=Path, required=True, help="TaskLineage JSON file"
    )
    parser.add_argument(
        "--evidence", type=Path, required=True, help="EvidenceBundle JSON file"
    )
    parser.add_argument(
        "--expected-base-sha",
        type=str,
        required=True,
        help="Expected base SHA (40 hex)",
    )
    parser.add_argument(
        "--subject-sha", type=str, required=True, help="Subject SHA (40 hex)"
    )
    parser.add_argument(
        "--output", type=Path, help="Output ConvergenceReport JSON file"
    )

    args = parser.parse_args()

    inputs = {
        "--intent": args.intent,
        "--clarification": args.clarification,
        "--review": args.review,
        "--lineage": args.lineage,
        "--evidence": args.evidence,
    }

    if args.output:
        try:
            check_output_alias(args.output, inputs, "converge_task")
        except OutputAliasError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        raw_intent = safe_read(args.intent, "converge_task")
        raw_clarification = safe_read(args.clarification, "converge_task")
        raw_review = safe_read(args.review, "converge_task")
        raw_lineage = safe_read(args.lineage, "converge_task")
        raw_evidence = safe_read(args.evidence, "converge_task")
    except SafeReadError as exc:
        print(f"converge_task: READ_ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        intent = deserialize_intent(raw_intent)
        clarification = deserialize_clarification(raw_clarification)
        review = deserialize_review(raw_review)
        try:
            lineage_data = json.loads(raw_lineage)
        except Exception:
            print("converge_task: CONTRACT_ERROR: JSON_INVALID", file=sys.stderr)
            return 2
        lineage = validate_lineage(lineage_data)
        bundle = deserialize_evidence_bundle(raw_evidence)

        report = evaluate_convergence(
            intent=intent,
            clarification=clarification,
            quality_review=review,
            lineage=lineage,
            bundle=bundle,
            expected_base_sha=args.expected_base_sha,
            subject_sha=args.subject_sha,
        )
    except (ValueError, ConvergenceError) as exc:
        code = getattr(exc, "code", str(exc))
        print(f"converge_task: CONTRACT_ERROR: {code}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"converge_task: INTERNAL_ERROR: {exc}", file=sys.stderr)
        return 2

    out_payload = serialize_convergence_report(report)
    out_json = json.dumps(out_payload, indent=2, sort_keys=True)

    if args.output:
        try:
            args.output.write_text(out_json, encoding="utf-8")
        except OSError as exc:
            print(f"converge_task: WRITE_ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        print(out_json)

    if report.status == ConvergenceStatus.CONVERGED:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
