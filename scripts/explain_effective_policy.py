#!/usr/bin/env python3
"""Explain Effective Policy and Source Attribution for a Hermes TaskIntent.

Resolves task-level policy, invariants, and required gates against exact Git repository sources.

Exit codes:
  0 = valid COMPLETE report (all declared references resolved)
  1 = valid INCOMPLETE report (one or more declared references unresolved)
  2 = invalid input / unsafe repository source / contract failure
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repository root is on sys.path when script is executed directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_engineering.effective_policy import (
    EffectivePolicyStatus,
    EffectivePolicyValidationError,
    deserialize_effective_policy_report,
    resolve_effective_policy,
    serialize_effective_policy_report,
)
from ai_engineering.task_intent import (
    TaskIntentValidationError,
    deserialize_intent,
)
from scripts._cli_utils import (
    OutputAliasError,
    SafeReadError,
    check_output_alias,
    resolve_path,
    safe_read,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explain effective policy and source attribution for a TaskIntent."
    )
    parser.add_argument(
        "--intent",
        required=True,
        type=Path,
        help="Path to TaskIntent JSON file.",
    )
    parser.add_argument(
        "--repository-root",
        default=Path("."),
        type=Path,
        help="Path to Git repository root (default: current directory).",
    )
    parser.add_argument(
        "--subject-sha",
        default=None,
        type=str,
        help="Exact 40-hex commit SHA to read policy sources from (default: intent.source_base_sha).",
    )
    parser.add_argument(
        "--output",
        default=None,
        type=Path,
        help="Optional path to write the EffectivePolicyReport JSON.",
    )

    args = parser.parse_args()

    tool_name = "explain_effective_policy.py"

    # 1. Check output alias
    if args.output is not None:
        try:
            check_output_alias(
                args.output,
                {"--intent": args.intent},
                tool_name,
            )
        except OutputAliasError as exc:
            sys.stderr.write(f"Error: {exc.message}\n")
            return 2

    # 2. Read and deserialize TaskIntent
    try:
        raw_intent = safe_read(args.intent, tool_name)
    except SafeReadError as exc:
        sys.stderr.write(f"Error: {exc.message}\n")
        return 2

    try:
        intent = deserialize_intent(raw_intent)
    except TaskIntentValidationError as exc:
        sys.stderr.write(f"Error: INTENT_VALIDATION_ERROR: {exc.code}\n")
        return 2

    # 3. Resolve effective policy
    try:
        report = resolve_effective_policy(
            intent=intent,
            repository_root=args.repository_root,
            subject_sha=args.subject_sha,
        )
    except EffectivePolicyValidationError as exc:
        sys.stderr.write(f"Error: POLICY_RESOLUTION_ERROR: {exc.code}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"Error: UNEXPECTED_ERROR: {exc}\n")
        return 2

    # 4. Serialize report
    try:
        report_json = serialize_effective_policy_report(report)
    except EffectivePolicyValidationError as exc:
        sys.stderr.write(f"Error: REPORT_SERIALIZATION_ERROR: {exc.code}\n")
        return 2

    # 5. Write output or stdout
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report_json, encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"Error: OUTPUT_WRITE_FAILED: {exc}\n")
            return 2
    else:
        sys.stdout.write(report_json + "\n")

    if report.status == EffectivePolicyStatus.COMPLETE:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
