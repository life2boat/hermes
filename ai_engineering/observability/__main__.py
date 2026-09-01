"""Bounded read-only operator CLI (PR-12).

The CLI operates only on operator snapshot JSON supplied explicitly via
``--input`` or stdin. It never connects to a repository, network,
provider, or production runtime, and never constructs control-plane
state.

Usage::

    python -m ai_engineering.observability --input snapshot.json [--json]
"""

from __future__ import annotations

import argparse
import sys

from ai_engineering.observability.rendering import (
    ObservabilitySchemaError,
    canonical_json,
    human_summary,
    load_operator_snapshot_dict,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_engineering.observability",
        description="Read-only Hermes operator observability renderer.",
    )
    parser.add_argument(
        "--input",
        "-i",
        default=None,
        help="Path to an operator snapshot JSON file (default: stdin).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit canonical redacted JSON (default: human-readable summary).",
    )
    args = parser.parse_args(argv)

    if args.input is not None:
        try:
            with open(args.input, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            print(f"OBSERVABILITY_INPUT_UNREADABLE: {exc}", file=sys.stderr)
            return 2
    else:
        raw = sys.stdin.read()
        if not raw.strip():
            print("OBSERVABILITY_INPUT_EMPTY", file=sys.stderr)
            return 2

    try:
        snapshot_dict = load_operator_snapshot_dict(raw)
    except ObservabilitySchemaError as exc:
        print(exc.code, file=sys.stderr)
        return 2

    if args.json:
        print(canonical_json(snapshot_dict))
    else:
        print(human_summary(snapshot_dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
