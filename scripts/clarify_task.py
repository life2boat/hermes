"""CLI for Clarification generation.

Usage:
    python scripts/clarify_task.py --intent INTENT.json [--output clarification-report.json]

Exit codes:
    0 = successfully generated, no blocking unknowns
    1 = successfully generated, blocking unknowns present
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
    generate_clarification_report,
    serialize_clarification,
    RequirementsGateError,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deterministic Clarification generator.")
    p.add_argument(
        "--intent", type=Path, required=True, help="Path to TaskIntent JSON file."
    )
    p.add_argument(
        "--output", type=Path, help="Write canonical ClarificationReport to this path."
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.output is not None:
        try:
            check_output_alias(args.output, {"--intent": args.intent}, "clarify_task")
        except OutputAliasError as exc:
            print(exc.message, file=sys.stderr)
            return 2

    try:
        intent_raw = safe_read(args.intent, "clarify_task")
    except SafeReadError as exc:
        print(exc.message, file=sys.stderr)
        return 2

    try:
        intent = deserialize_intent(intent_raw)
    except TaskIntentValidationError as exc:
        print(f"clarify_task: {exc.code}", file=sys.stderr)
        return 2

    try:
        report = generate_clarification_report(intent)
    except RequirementsGateError as exc:
        print(f"clarify_task: {exc.code}", file=sys.stderr)
        return 2

    canonical_json = serialize_clarification(report)

    if args.output is not None:
        out_path = args.output
        if out_path.is_symlink():
            print(f"clarify_task: OUTPUT_UNSAFE: {out_path}", file=sys.stderr)
            return 2
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(canonical_json + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"clarify_task: OUTPUT_WRITE_FAILED: {exc}", file=sys.stderr)
            return 2
    else:
        print(canonical_json)

    if report.blocking_question_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
